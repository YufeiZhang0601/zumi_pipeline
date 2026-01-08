import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from zumi_core import NodeHTTPService
from zumi_config import HTTP_CONF, MOTOR_CONF, NodeStatus, STORAGE_CONF, get_default_gripper_id, get_gripper_mapping
from zumi_util import RateLimiter
from motor_mock import MockMotorDriver

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
logger = logging.getLogger("Motor")

try:
    from motor_dm import DMMotorDriver
except Exception:  # noqa: BLE001
    DMMotorDriver = None


def _build_driver():
    if MOTOR_CONF.DRIVER == "dm" and DMMotorDriver is not None:
        try:
            return DMMotorDriver(
                serial_port=MOTOR_CONF.SERIAL_PORT,
                slave_id=MOTOR_CONF.SLAVE_ID,
                master_id=MOTOR_CONF.MASTER_ID,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"DM driver init failed, falling back to mock: {exc}")
    logger.info("Using mock motor driver.")
    return MockMotorDriver()


class MotorRecorder:
    def __init__(self, driver, hz: float = 200.0):
        self.driver = driver
        self.hz = hz
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.file_path: Optional[Path] = None
        self.iter_idx = 0

    def start(self, path: Path):
        self.stop()
        self.stop_event.clear()
        self.file_path = path
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None

    def _loop(self):
        if not self.file_path:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        limiter = RateLimiter(self.hz)
        with self.file_path.open("w") as fh:
            while not self.stop_event.is_set():
                ts = time.time()
                state = self.driver.get_state()
                record = {
                    "ts": ts,
                    "pos": [state.position],
                    "vel": [state.velocity],
                    "tau": [state.torque],
                    "iter": self.iter_idx,
                }
                fh.write(json.dumps(record) + "\n")
                self.iter_idx += 1
                limiter.sleep()


class MotorNode(NodeHTTPService):
    def __init__(self, gripper_id: str = None):
        self.gripper_id = gripper_id or get_default_gripper_id()
        self.driver = None
        self.recorder = None
        self.current_path: Optional[Path] = None
        super().__init__(name=f"motor_{self.gripper_id}", host=HTTP_CONF.MOTOR_HOST, port=HTTP_CONF.MOTOR_PORT)

    def on_init(self):
        self.driver = _build_driver()
        self.driver.enable()
        self.driver.set_zero()
        self.recorder = MotorRecorder(self.driver)
        logger.info("Motor node initialized.")

    def on_prepare(self, run_id, episode=None):
        try:
            _ = self.driver.get_state()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Prepare failed: {exc}")
            return False

    def _episode_path(self, run_id: str, episode: Optional[int]) -> Path:
        ep_val = episode if episode is not None else 1
        ep_tag = f"ep{int(ep_val):03d}"
        run_dir = STORAGE_CONF.DATA_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"{run_id}_{ep_tag}_{self.gripper_id}_motor.jsonl"

    def on_start_recording(self, run_id, episode=None, start_time=None):
        path = self._episode_path(run_id, episode)
        self.current_path = path
        logger.info(f"[Record] START motor -> {path.name}")
        self.recorder.iter_idx = 0
        self.recorder.start(path)
        self.publish_status()

    def on_stop_recording(self):
        logger.info("[Record] STOP motor")
        self.status = NodeStatus.SAVING
        self.publish_status()
        self.recorder.stop()
        self.status = NodeStatus.IDLE
        self.publish_status()
        self.current_path = None

    def on_discard_run(self, run_id, episode=None):
        if self.is_recording and self.recorder:
            self.recorder.stop()
        path = self._episode_path(run_id, episode)
        if path.exists():
            try:
                path.unlink()
                logger.info(f"[Discard] Removed {path}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[Discard] Failed to remove {path}: {exc}")

    def on_shutdown(self):
        if self.recorder:
            self.recorder.stop()
        if self.driver:
            try:
                self.driver.shutdown()
            except Exception:
                pass

    def main_loop(self):
        while self.is_running:
            time.sleep(0.1)

    def check_hardware_health(self):
        _ = self.driver.get_state()

    def extra_status(self):
        return {"motor_file": str(self.current_path) if self.current_path else None}


def validate(run_id: str, episode: Optional[int], gripper_id: str = None):
    from validator import ValidationResult

    gripper_id = gripper_id or get_default_gripper_id()
    ep_val = episode if episode is not None else 1
    ep_tag = f"ep{int(ep_val):03d}"
    run_dir = STORAGE_CONF.DATA_DIR / run_id
    path = run_dir / f"{run_id}_{ep_tag}_{gripper_id}_motor.jsonl"

    if not path.exists():
        return ValidationResult(False, "motor_missing", f"Motor data missing: {path.name}")
    if path.stat().st_size < 10:
        return ValidationResult(False, "motor_empty", "Motor data empty")

    try:
        count = 0
        zero_or_nan = 0
        first_ts = None
        last_ts = None
        start_positions = []
        with path.open() as fh:
            for line in fh:
                record = json.loads(line)
                ts = float(record.get("ts"))
                pos = record.get("pos", [])
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
                if pos:
                    start_positions.append(float(pos[0]))
                    if abs(pos[0]) <= 1e-6:
                        zero_or_nan += 1
                count += 1
        if count == 0:
            return ValidationResult(False, "motor_empty", "Motor data empty")
        if zero_or_nan / max(count, 1) > 0.9:
            return ValidationResult(False, "motor_flat", "Motor data mostly zero")
        if start_positions:
            mean_start = sum(start_positions[: min(len(start_positions), 10)]) / min(len(start_positions), 10)
            if abs(mean_start) > 0.1:
                return ValidationResult(False, "motor_start_nonzero", f"Start pos not zero: {mean_start:.3f}")
        if first_ts and last_ts and last_ts <= first_ts:
            return ValidationResult(False, "motor_invalid_ts", "Motor timestamps not increasing")
    except Exception as exc:  # noqa: BLE001
        return ValidationResult(False, "motor_corrupt", f"Motor data invalid: {exc}")

    return ValidationResult(True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gripper-id", default=None, help="Gripper ID (e.g., gp00)")
    parser.add_argument("--port", type=int, default=HTTP_CONF.MOTOR_PORT, help="HTTP port")
    args = parser.parse_args()

    node = MotorNode(gripper_id=args.gripper_id)
    node.run(port=args.port)
