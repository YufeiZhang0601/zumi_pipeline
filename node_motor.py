import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

from zumi_core import NodeHTTPService
from zumi_config import HTTP_CONF, MOTOR_CONF, NodeStatus, STORAGE_CONF, get_default_gripper_id
from zumi_util import RateLimiter
from motor_dm import DMMotorDriver

logger = logging.getLogger("Motor")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
logger.addHandler(_handler)
logger.propagate = False


def _build_driver(auto_set_zero: bool = True):
    return DMMotorDriver(
        serial_port=MOTOR_CONF.SERIAL_PORT,
        slave_id=MOTOR_CONF.SLAVE_ID,
        master_id=MOTOR_CONF.MASTER_ID,
        auto_set_zero=auto_set_zero,
    )


class MotorNode(NodeHTTPService):
    # Motor uses smaller backoff due to high frequency communication
    RECOVERY_BACKOFF_BASE = 1.0
    RECOVERY_BACKOFF_MAX = 10.0

    def __init__(self, gripper_id: str = None, port: int = None):
        self.gripper_id = gripper_id or get_default_gripper_id()
        self.driver = None
        self.target_freq = MOTOR_CONF.TARGET_FREQ
        self.batch_size = 50  # Send to writer every 50 records (~250ms at 200Hz)
        self.lock_duration = MOTOR_CONF.LOCK_DURATION  # Lock gripper position (0 = no lock)

        # Writer thread state
        self.write_queue: queue.Queue = None
        self.writer_thread: Optional[threading.Thread] = None
        self._writer_file = None
        self._writer_path: Optional[Path] = None

        # Recording state (main_loop side)
        self.local_buffer = []
        self.recording_start_time: float = 0.0
        self.iter_idx: int = 0
        self.current_path: Optional[Path] = None

        super().__init__(
            name=f"motor_{self.gripper_id}",
            host=HTTP_CONF.MOTOR_HOST,
            port=port or HTTP_CONF.MOTOR_PORT,
        )

    # -------------------------------------------------------------------------
    # Writer Thread (handles all disk IO)
    # -------------------------------------------------------------------------
    def _start_writer_thread(self):
        """Start the writer thread."""
        self.write_queue = queue.Queue()
        self.writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.writer_thread.start()

    def _stop_writer_thread(self):
        """Stop the writer thread."""
        if self.write_queue:
            self.write_queue.put(None)  # Exit signal
        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=2.0)
        self.writer_thread = None
        self.write_queue = None

    def _writer_loop(self):
        """Writer thread: handles all disk IO."""
        while True:
            try:
                item = self.write_queue.get()
            except Exception:
                break

            if item is None:  # Exit signal
                break

            try:
                msg_type, payload = item
            except (TypeError, ValueError):
                continue

            try:
                if msg_type == "START":
                    path = payload["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._writer_path = path
                    self._writer_file = path.open("w")
                    logger.info(f"[Writer] Started: {path.name}")

                elif msg_type == "DATA":
                    if self._writer_file:
                        for record in payload:
                            self._writer_file.write(json.dumps(record) + "\n")
                        self._writer_file.flush()

                elif msg_type == "DISCARD":
                    logger.warning("[Writer] DISCARDING current recording")
                    if self._writer_file:
                        self._writer_file.close()
                        self._writer_file = None
                    # Use path from payload for consistent cleanup
                    path_to_remove = payload.get("path") if payload else self._writer_path
                    if path_to_remove and path_to_remove.exists():
                        try:
                            path_to_remove.unlink()
                            logger.info(f"[Writer] Removed partial file: {path_to_remove.name}")
                        except Exception:
                            pass
                    self._writer_path = None

                elif msg_type == "STOP":
                    if self._writer_file:
                        self._writer_file.close()
                        self._writer_file = None
                        logger.info(f"[Writer] Saved: {self._writer_path.name}")
                    self._writer_path = None

            except Exception as exc:
                logger.error(f"[Writer] Error: {exc}")

    # -------------------------------------------------------------------------
    # Lifecycle hooks
    # -------------------------------------------------------------------------
    def on_init(self):
        # DMMotorDriver.__init__ already calls enable() and set_zero()
        self.driver = _build_driver(auto_set_zero=True)
        self._start_writer_thread()
        logger.info(f"Motor node initialized (gripper_id={self.gripper_id}).")

    def on_prepare(self, run_id, episode=None):
        """Check gripper state and set zero before recording.

        Retries up to 3 times with 0.5s delay between attempts to handle
        intermittent communication failures.
        """
        max_retries = 3
        retry_delay = 0.5

        for attempt in range(max_retries):
            try:
                # Send command to ensure CAN communication is working
                self.driver.command(0, 0, 0, 0, 0)
                state = self.driver.get_state()

                # Check gripper position (warn if not closed, but don't block)
                if state.position >= 0.1:
                    logger.warning(
                        f"Gripper position ({state.position:.3f}) >= 0.1, "
                        "starting with gripper open (non-closed initial state)"
                    )

                # Set zero position for this recording session
                self.driver.set_zero()
                logger.info("Gripper zero position set.")
                return True

            except Exception as exc:  # noqa: BLE001
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Prepare attempt {attempt + 1}/{max_retries} failed: {exc}, retrying..."
                    )
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Prepare failed after {max_retries} attempts: {exc}")
                    return False
        return False

    def _episode_path(self, run_id: str, episode: Optional[int]) -> Path:
        ep_val = episode if episode is not None else 1
        ep_tag = f"ep{int(ep_val):03d}"
        run_dir = STORAGE_CONF.DATA_DIR / run_id
        return run_dir / f"{run_id}_{ep_tag}_{self.gripper_id}_motor.jsonl"

    def on_start_recording(self, run_id, episode=None, start_time=None):
        # Re-zero immediately before recording to ensure first sample is at zero
        self.driver.set_zero()

        path = self._episode_path(run_id, episode)
        self.current_path = path
        self.recording_start_time = time.time()
        self.iter_idx = 0
        self.local_buffer = []

        # Tell writer to start new file
        self.write_queue.put(("START", {"path": path}))
        logger.info(f"[Record] START motor -> {path.name}")
        self.publish_status()

    def on_stop_recording(self):
        logger.info("[Record] STOP motor")
        self.status = NodeStatus.SAVING
        self.publish_status()

        # Flush remaining buffer
        if self.local_buffer:
            self.write_queue.put(("DATA", self.local_buffer))
            self.local_buffer = []

        # Tell writer to close file
        self.write_queue.put(("STOP", None))

        self.status = NodeStatus.IDLE
        self.publish_status()
        self.current_path = None

    def _discard_current_recording(self, reason: str):
        """Discard current recording data due to error."""
        logger.error("=" * 60)
        logger.error("!!! RECORDING ABORTED - DATA DISCARDED !!!")
        logger.error(f"Run: {self.run_id}, Episode: {self.episode}")
        logger.error(f"Reason: {reason}")
        logger.error("=" * 60)

        # Clear local buffer
        self.local_buffer = []

        # Tell writer to discard, passing path for consistent cleanup
        if self.current_path:
            self.write_queue.put(("DISCARD", {"path": self.current_path}))

        self.is_recording = False
        self.current_path = None

    def on_discard_run(self, run_id, episode=None):
        path = self._episode_path(run_id, episode)

        # Stop any active recording first
        if self.is_recording:
            self.local_buffer = []
            self.write_queue.put(("DISCARD", {"path": path}))
            self.is_recording = False
            self.current_path = None
        else:
            # Not recording, directly remove file if exists
            if path.exists():
                try:
                    path.unlink()
                    logger.info(f"[Discard] Removed {path}")
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[Discard] Failed to remove {path}: {exc}")

    def on_shutdown(self):
        self._stop_writer_thread()
        if self.driver:
            try:
                self.driver.shutdown()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Main loop (200Hz)
    # -------------------------------------------------------------------------
    def main_loop(self):
        """
        Main control loop at 200Hz.
        - Always sends command() to maintain CAN communication (watchdog)
        - Locks gripper position when not recording or during first 0.5s of recording
        - Collects data and sends to writer thread via Queue
        - Detects communication failures and triggers recovery
        """
        rate = RateLimiter(self.target_freq)
        get_time = time.time
        consecutive_failures = 0
        max_failures = 10  # ~50ms at 200Hz

        while self.is_running:
            # Decide whether to lock gripper position
            should_lock = not self.is_recording or (
                get_time() - self.recording_start_time < self.lock_duration
            )

            # Send command (triggers CAN communication)
            try:
                if should_lock:
                    # Position lock: kp=0.8, kd=0.05
                    self.driver.command(0.0, 0.0, 0.0, 0.8, 0.05)
                else:
                    # Zero torque: free movement
                    self.driver.command(0.0, 0.0, 0.0, 0.0, 0.0)
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    logger.warning(f"Motor command failed ({consecutive_failures}/{max_failures}): {exc}")
                    # Small delay to let hardware recover before next retry
                    time.sleep(0.01)
                if consecutive_failures >= max_failures:
                    if self.is_recording:
                        self._discard_current_recording(f"Motor communication lost: {exc}")
                    raise RuntimeError(
                        f"Motor communication lost after {consecutive_failures} consecutive failures"
                    ) from exc

            # Collect data during recording
            if self.is_recording:
                try:
                    state = self.driver.get_state()
                    record = {
                        "ts": get_time(),
                        "pos": [state.position],
                        "vel": [state.velocity],
                        "tau": [state.torque],
                        "iter": self.iter_idx,
                    }
                    self.local_buffer.append(record)
                    self.iter_idx += 1

                    # Batch send to writer thread
                    if len(self.local_buffer) >= self.batch_size:
                        self.write_queue.put(("DATA", self.local_buffer))
                        self.local_buffer = []

                except Exception as exc:
                    logger.error(f"State read failed: {exc}")

            rate.sleep()

        # Cleanup on exit
        if self.is_recording:
            self.on_stop_recording()

    # -------------------------------------------------------------------------
    # Health check
    # -------------------------------------------------------------------------
    def check_hardware_health(self):
        """Check motor communication by sending a zero command.

        Must use command() instead of get_state() because:
        - command() triggers real CAN communication (send + recv)
        - get_state() only reads cached values, won't detect disconnection
        """
        self.driver.command(0.0, 0.0, 0.0, 0.0, 0.0)

    # -------------------------------------------------------------------------
    # Recovery support
    # -------------------------------------------------------------------------
    def can_recover(self, exc: Exception) -> bool:
        """Only recover from communication-related errors."""
        return isinstance(exc, (TimeoutError, RuntimeError, OSError))

    def _cleanup_for_recovery(self):
        """Clean up before recovery attempt."""
        # Discard any in-progress recording
        if self.is_recording:
            self.local_buffer = []
            if self.current_path:
                self.write_queue.put(("DISCARD", {"path": self.current_path}))
            self.is_recording = False
            self.current_path = None

        # Shutdown old driver
        if self.driver:
            try:
                self.driver.shutdown()
            except Exception:
                pass
            self.driver = None

    def on_recover(self):
        """Reinitialize driver after recovery.

        Note: Does NOT set_zero() to preserve current position.
        DMMotorDriver.__init__ already calls enable().
        """
        self.driver = _build_driver(auto_set_zero=False)
        # Send a command to verify communication is restored
        self.driver.command(0.0, 0.0, 0.0, 0.8, 0.05)
        logger.info("Motor driver recovered.")

    def after_recover(self):
        """Reset recording state after recovery."""
        self.iter_idx = 0
        self.local_buffer = []
        self.current_path = None

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------
    def extra_status(self):
        return {"motor_file": str(self.current_path) if self.current_path else None}


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def validate(run_id: str, episode: Optional[int], gripper_id: str = None):
    from validator import ValidationResult

    gripper_id = gripper_id or get_default_gripper_id()
    ep_val = episode if episode is not None else 1
    ep_tag = f"ep{int(ep_val):03d}"
    run_dir = STORAGE_CONF.DATA_DIR / run_id

    # Find motor file: {run_id}_{ep_tag}_{gripper_id}_motor.jsonl
    # or with video ID: {run_id}_{ep_tag}_{gripper_id}_*_motor.jsonl
    candidates = sorted(run_dir.glob(f"{run_id}_{ep_tag}_{gripper_id}*_motor.jsonl"))

    if not candidates:
        return ValidationResult(
            False,
            "motor_missing",
            f"Motor data missing: {run_id}_{ep_tag}_{gripper_id}_motor.jsonl",
        )

    path = candidates[0]

    if path.stat().st_size < 10:
        return ValidationResult(False, "motor_empty", "Motor data empty")

    try:
        count = 0
        first_ts = None
        last_ts = None
        all_positions = []

        with path.open() as fh:
            for line in fh:
                record = json.loads(line)
                ts = float(record.get("ts"))
                pos = record.get("pos", [])

                if first_ts is None:
                    first_ts = ts
                last_ts = ts

                if pos:
                    all_positions.append(float(pos[0]))
                count += 1

        if count == 0:
            return ValidationResult(False, "motor_empty", "Motor data empty")

        # Check if data is flat (not changing) - use all data
        if all_positions:
            unique_positions = len(set(round(p, 6) for p in all_positions))
            if unique_positions < 5:
                return ValidationResult(
                    False, "motor_flat", f"Motor data not changing (only {unique_positions} unique values)"
                )

        # Check start position is near zero
        if all_positions:
            mean_start = sum(all_positions[:10]) / min(len(all_positions), 10)
            if abs(mean_start) > 0.1:
                return ValidationResult(
                    False, "motor_start_nonzero", f"Start pos not zero: {mean_start:.3f}"
                )

        # Check timestamps are increasing
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

    node = MotorNode(gripper_id=args.gripper_id, port=args.port)
    node.start()
