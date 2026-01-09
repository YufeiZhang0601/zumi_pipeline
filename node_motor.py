import json
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from queue import Full
from typing import Optional

from zumi_core import NodeHTTPService
from zumi_config import HTTP_CONF, MOTOR_CONF, NodeStatus, PREVIEW_CONF, STORAGE_CONF, get_default_gripper_id
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


# -----------------------------------------------------------------------------
# Preview Process (runs in separate process to avoid GIL interference)
# -----------------------------------------------------------------------------
class MotorPreviewProcess(mp.Process):
    """Real-time motor data visualization using OpenCV."""

    def __init__(self, data_queue: mp.Queue, gripper_id: str, stop_event: mp.Event):
        super().__init__(daemon=True)
        self.data_queue = data_queue
        self.gripper_id = gripper_id
        self.stop_event = stop_event

    def run(self):
        import cv2
        import numpy as np

        cv2.setNumThreads(1)

        # Canvas size
        width, height = 800, 600
        plot_height = height // 3  # 3 plots stacked vertically
        margin_left, margin_right = 60, 20
        margin_top, margin_bottom = 25, 20
        plot_width = width - margin_left - margin_right

        # Data buffers
        buffer_size = PREVIEW_CONF.MOTOR_BUFFER_SIZE
        pos_buffer = deque(maxlen=buffer_size)
        vel_buffer = deque(maxlen=buffer_size)
        tau_buffer = deque(maxlen=buffer_size)

        # Y-axis ranges
        ranges = [(-1.0, 1.0), (-5.0, 5.0), (-2.0, 2.0)]
        labels = ["Position (rad)", "Velocity (rad/s)", "Torque (Nm)"]
        colors = [(255, 100, 100), (100, 255, 100), (100, 100, 255)]  # BGR

        window_name = f"Motor Preview: {self.gripper_id}"
        interval_ms = int(1000 / PREVIEW_CONF.MOTOR_PREVIEW_FPS)

        def draw_plot(canvas, data, y_range, color, y_offset, label):
            """Draw a single plot on the canvas."""
            y_min, y_max = y_range
            plot_y_start = y_offset + margin_top
            plot_y_end = y_offset + plot_height - margin_bottom
            plot_h = plot_y_end - plot_y_start

            # Draw background and border
            cv2.rectangle(canvas, (margin_left, plot_y_start),
                         (margin_left + plot_width, plot_y_end), (40, 40, 40), -1)
            cv2.rectangle(canvas, (margin_left, plot_y_start),
                         (margin_left + plot_width, plot_y_end), (80, 80, 80), 1)

            # Draw zero line
            zero_y = int(plot_y_start + plot_h * (y_max / (y_max - y_min)))
            if plot_y_start < zero_y < plot_y_end:
                cv2.line(canvas, (margin_left, zero_y),
                        (margin_left + plot_width, zero_y), (60, 60, 60), 1)

            # Draw label
            cv2.putText(canvas, label, (5, y_offset + plot_height // 2),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

            # Draw y-axis ticks
            for val in [y_min, 0, y_max]:
                y_pos = int(plot_y_start + plot_h * (1 - (val - y_min) / (y_max - y_min)))
                if plot_y_start <= y_pos <= plot_y_end:
                    cv2.putText(canvas, f"{val:.1f}", (margin_left - 35, y_pos + 4),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.35, (150, 150, 150), 1)

            # Draw data - use actual data length for x scaling
            n = len(data)
            if n >= 2:
                points = []
                for i, val in enumerate(data):
                    # Scale x to fill the plot width based on actual data
                    x = margin_left + int(i * plot_width / max(n - 1, 1))
                    y = plot_y_start + int(plot_h * (1 - (val - y_min) / (y_max - y_min)))
                    y = max(plot_y_start, min(plot_y_end, y))
                    points.append((x, y))
                cv2.polylines(canvas, [np.array(points, dtype=np.int32)],
                             False, color, 2, cv2.LINE_AA)

        # Create window and set position (offset from UVC window)
        cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(window_name, 850, 50)  # Right side of screen

        while not self.stop_event.is_set():
            # Drain queue
            while True:
                try:
                    data = self.data_queue.get_nowait()
                    pos_buffer.append(data["pos"])
                    vel_buffer.append(data["vel"])
                    tau_buffer.append(data["tau"])
                except Exception:
                    break

            # Create canvas
            canvas = np.zeros((height, width, 3), dtype=np.uint8)

            # Draw title
            cv2.putText(canvas, f"Motor: {self.gripper_id}", (width // 2 - 60, 18),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

            # Draw plots
            buffers = [pos_buffer, vel_buffer, tau_buffer]
            for i, (buf, rng, lbl, clr) in enumerate(zip(buffers, ranges, labels, colors)):
                draw_plot(canvas, buf, rng, clr, i * plot_height, lbl)

            cv2.imshow(window_name, canvas)
            key = cv2.waitKey(interval_ms) & 0xFF
            if key == ord('q') or key == 27:  # q or ESC
                self.stop_event.set()
                break

        try:
            cv2.destroyWindow(window_name)
        except Exception:
            pass


class MotorNode(NodeHTTPService):
    # Motor uses smaller backoff due to high frequency communication
    RECOVERY_BACKOFF_BASE = 1.0
    RECOVERY_BACKOFF_MAX = 10.0

    def __init__(self, gripper_id: str = None, port: int = None, preview: bool = False):
        self.gripper_id = gripper_id or get_default_gripper_id()
        self.driver = None
        self.target_freq = MOTOR_CONF.TARGET_FREQ
        self.batch_size = max(10, int(self.target_freq * 0.25))  # ~250ms worth of records
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

        # Preview state (separate process)
        self.preview_enabled = preview
        self.preview_queue: Optional[mp.Queue] = None
        self.preview_process: Optional[MotorPreviewProcess] = None
        self.preview_stop_event: Optional[mp.Event] = None

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
    # Preview Process (real-time visualization)
    # -------------------------------------------------------------------------
    def _start_preview(self):
        """Start the preview process if display is available."""
        if os.environ.get("DISPLAY") is None:
            logger.warning("[Preview] No display available, skipping preview")
            return

        self.preview_queue = mp.Queue(maxsize=PREVIEW_CONF.MOTOR_QUEUE_SIZE)
        self.preview_stop_event = mp.Event()
        self.preview_process = MotorPreviewProcess(
            self.preview_queue, self.gripper_id, self.preview_stop_event
        )
        self.preview_process.start()
        logger.info("[Preview] Motor preview process started")

    def _stop_preview(self):
        """Stop the preview process."""
        if self.preview_stop_event:
            self.preview_stop_event.set()
        if self.preview_process and self.preview_process.is_alive():
            self.preview_process.join(timeout=2.0)
            if self.preview_process.is_alive():
                self.preview_process.terminate()
        self.preview_process = None
        self.preview_queue = None
        self.preview_stop_event = None

    # -------------------------------------------------------------------------
    # Lifecycle hooks
    # -------------------------------------------------------------------------
    def on_init(self):
        # DMMotorDriver.__init__ already calls enable() and set_zero()
        self.driver = _build_driver(auto_set_zero=True)
        self._start_writer_thread()
        if self.preview_enabled:
            self._start_preview()
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

    def on_discard_run(self, run_id, episode=None):
        path = self._episode_path(run_id, episode)

        # Always cleanup writer state (base class already set is_recording=False)
        self.local_buffer = []
        if self.write_queue:
            self.write_queue.put(("DISCARD", {"path": path}))
        self.current_path = None

    def on_shutdown(self):
        self._stop_preview()
        self._stop_writer_thread()
        if self.driver:
            try:
                self.driver.shutdown()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------
    def main_loop(self):
        """
        Main control loop at configured frequency (MOTOR_CONF.TARGET_FREQ).
        - Always sends command() to maintain CAN communication (watchdog)
        - Locks gripper position when not recording or during first 0.5s of recording
        - Collects data and sends to writer thread via Queue
        - Sends decimated data to preview process (if enabled)
        - Detects communication failures and triggers recovery
        """
        rate = RateLimiter(self.target_freq)
        get_time = time.time
        consecutive_failures = 0
        max_failures = 3  # Fail fast on communication errors
        stale_threshold = 0.1  # 100ms without updates considered stale
        last_logged_stale_time = 0.0
        consecutive_stale = 0
        max_consecutive_stale = int(2.0 / stale_threshold)  # ~2s timeout
        preview_iter = 0  # Counter for preview decimation

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

            # Read state if recording OR preview is active
            should_read_state = self.is_recording or self.preview_queue is not None
            if should_read_state:
                try:
                    state = self.driver.get_state()
                    now = get_time()
                    data_age = now - state.last_update_time

                    # Handle stale data detection (only during recording)
                    if self.is_recording and data_age > stale_threshold:
                        consecutive_stale += 1
                        if now - last_logged_stale_time > 1.0:
                            logger.warning(
                                f"[Record] Motor data stale! Age: {data_age:.3f}s "
                                f"({consecutive_stale}/{max_consecutive_stale})"
                            )
                            last_logged_stale_time = now
                        if consecutive_stale >= max_consecutive_stale:
                            self._discard_current_recording(
                                f"Stale data too long: {consecutive_stale} consecutive stale samples"
                            )
                        rate.sleep()
                        continue

                    # Data is fresh, reset stale counter
                    consecutive_stale = 0

                    # Send to preview (decimated, non-blocking)
                    if self.preview_queue is not None:
                        preview_iter += 1
                        if preview_iter % PREVIEW_CONF.MOTOR_DECIMATION == 0:
                            try:
                                self.preview_queue.put_nowait({
                                    "ts": now,
                                    "pos": state.position,
                                    "vel": state.velocity,
                                    "tau": state.torque,
                                })
                            except Full:
                                pass  # Drop frame, don't block main loop

                    # Record data (only during recording)
                    if self.is_recording:
                        record = {
                            "ts": now,
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

        # Check sample rate
        if first_ts and last_ts and last_ts > first_ts:
            duration = last_ts - first_ts
            expected_samples = duration * MOTOR_CONF.TARGET_FREQ
            sample_ratio = count / expected_samples if expected_samples > 0 else 0

            # If less than 50% of expected samples, data quality is too low
            if sample_ratio < 0.5 and count > 10:  # Only check if we have reasonable data
                return ValidationResult(
                    False,
                    "motor_sample_rate_low",
                    f"Sample rate too low: {count} samples in {duration:.1f}s "
                    f"(expected ~{int(expected_samples)}, ratio={sample_ratio:.1%})",
                )

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
    parser.add_argument("--preview", action="store_true", help="Enable real-time preview")
    args = parser.parse_args()

    node = MotorNode(gripper_id=args.gripper_id, port=args.port, preview=args.preview)
    node.start()
