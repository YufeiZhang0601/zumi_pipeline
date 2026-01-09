import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty
from typing import Optional, Tuple

from multiprocessing.managers import SharedMemoryManager

import cv2

from umi.real_world.uvc_camera import UvcCamera
from umi.real_world.video_recorder import VideoRecorder
from zumi_config import HTTP_CONF, NodeStatus, PREVIEW_CONF, STORAGE_CONF, UVC_CONF, get_default_gripper_id
from zumi_core import NodeHTTPService

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
logger = logging.getLogger("UVC")


# -----------------------------------------------------------------------------
# Backend Selection
# -----------------------------------------------------------------------------

def _is_pi() -> bool:
    try:
        model = Path("/proc/device-tree/model").read_text().lower()
        return "raspberry" in model
    except Exception:
        return False


def _has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def _build_video_recorder(fps: int) -> VideoRecorder:
    """
    Select a recorder based on platform, favoring hardware encoders when present.
    Keep arguments minimal to avoid PyAV option incompatibilities.
    # TODO: plug in a NetworkStreamer backend for remote encoding.
    """
    if _has_nvidia():
        logger.info("Using NVENC (hevc_nvenc) backend.")
        return VideoRecorder(
            fps=fps,
            codec="hevc_nvenc",
            input_pix_fmt="bgr24",
            bit_rate=8_000_000,
            pix_fmt="yuv420p",
        )

    if _is_pi():
        logger.info("Using V4L2M2M (h264_v4l2m2m) backend.")
        return VideoRecorder(
            fps=fps,
            codec="h264_v4l2m2m",
            input_pix_fmt="bgr24",
            bit_rate=6_000_000,
            pix_fmt="yuv420p",
        )

    logger.info("Using software H.264 backend.")
    return VideoRecorder(
        fps=fps,
        codec="h264",
        input_pix_fmt="bgr24",
        bit_rate=6_000_000,
        pix_fmt="yuv420p",
    )


# -----------------------------------------------------------------------------
# Sidecar logging
# -----------------------------------------------------------------------------

@dataclass
class SidecarWriter:
    path: Path
    ring_buffer: any
    stop_event: threading.Event
    last_count: int = 0

    def start(self) -> threading.Thread:
        self.last_count = self.ring_buffer.count
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        return thread

    def _loop(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w") as fh:
            while not self.stop_event.is_set():
                try:
                    current_count = self.ring_buffer.count
                    new_frames = current_count - self.last_count

                    if new_frames <= 0:
                        time.sleep(0.01)
                        continue

                    # Check if falling behind (would lose frames)
                    max_k = self.ring_buffer.get_max_k
                    if new_frames > max_k:
                        logger.warning(f"[Sidecar] Falling behind: {new_frames} new frames, can only read {max_k}. Dropping {new_frames - max_k} frames.")
                        new_frames = max_k

                    batch = self.ring_buffer.get_last_k(new_frames)
                    n = len(batch["timestamp"])

                    for i in range(n):
                        record = {
                            "frame_idx": int(batch.get("step_idx", [i])[i]),
                            "timestamp": float(batch["timestamp"][i]),
                            "capture_timestamp": float(batch.get("camera_capture_timestamp", batch["timestamp"])[i]),
                            "receive_timestamp": float(batch.get("camera_receive_timestamp", batch["timestamp"])[i]),
                        }
                        fh.write(json.dumps(record) + "\n")

                    self.last_count = current_count

                except Empty:
                    time.sleep(0.01)
                except Exception as exc:
                    logger.error(f"[Sidecar] ring buffer error: {exc}")
                    time.sleep(0.05)
            fh.flush()


# -----------------------------------------------------------------------------
# Preview Thread (real-time visualization)
# -----------------------------------------------------------------------------
class UvcPreviewThread(threading.Thread):
    """Real-time UVC camera preview in a background thread."""

    def __init__(self, camera: UvcCamera, gripper_id: str, fps: int = 30):
        super().__init__(daemon=True)
        self.camera = camera
        self.gripper_id = gripper_id
        self.fps = fps
        self.stop_event = threading.Event()
        self.window_name = f"UVC Preview: {gripper_id}"

    def run(self):
        cv2.setNumThreads(1)  # Limit OpenCV internal threads
        vis_data = None
        interval_ms = int(1000 / self.fps)

        # Create window and set position (left side of screen)
        cv2.namedWindow(self.window_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(self.window_name, 50, 50)

        while not self.stop_event.is_set():
            try:
                # Check if camera is still alive
                if not self.camera.is_alive():
                    logger.warning("[Preview] Camera process died, stopping preview")
                    break

                vis_data = self.camera.get_vis(out=vis_data)
                frame = vis_data.get("color")
                if frame is None:
                    time.sleep(0.1)
                    continue

                # Handle multi-camera array shape (N, H, W, C)
                if len(frame.shape) == 4:
                    frame = frame[0]

                # RGB to BGR for OpenCV display
                cv2.imshow(self.window_name, frame[:, :, ::-1])
                key = cv2.waitKey(interval_ms) & 0xFF
                if key == ord('q') or key == 27:  # q or ESC
                    break
            except Exception as exc:
                logger.warning(f"[Preview] Frame error: {exc}")
                time.sleep(0.1)

        try:
            cv2.destroyWindow(self.window_name)
        except Exception:
            pass

    def stop(self):
        self.stop_event.set()


# -----------------------------------------------------------------------------
# Node implementation
# -----------------------------------------------------------------------------

class UvcNode(NodeHTTPService):
    # Enable auto-recovery for camera failures
    AUTO_RECOVERY_ENABLED = True
    RECOVERY_BACKOFF_BASE = 2.0
    RECOVERY_BACKOFF_MAX = 30.0

    def __init__(self, gripper_id: str = None, port: int = None, preview: bool = False):
        self.gripper_id = gripper_id or get_default_gripper_id()
        self.shm_manager: Optional[SharedMemoryManager] = None
        self.camera: Optional[UvcCamera] = None
        self.sidecar_thread: Optional[threading.Thread] = None
        self.sidecar_stop: Optional[threading.Event] = None
        self.current_video: Optional[Path] = None
        self.current_meta: Optional[Path] = None
        # Preview state
        self.preview_enabled = preview
        self.preview_thread: Optional[UvcPreviewThread] = None
        super().__init__(name=f"uvc_{self.gripper_id}", host=HTTP_CONF.UVC_HOST, port=port or HTTP_CONF.UVC_PORT)

    # Lifecycle ---------------------------------------------------------------
    def on_init(self):
        self.shm_manager = SharedMemoryManager()
        self.shm_manager.start()

        recorder = _build_video_recorder(UVC_CONF.FPS)

        # Drop frame payload from ring buffer to keep sidecar light.
        def meta_transform(data):
            return {
                "camera_receive_timestamp": data["camera_receive_timestamp"],
                "camera_capture_timestamp": data["camera_capture_timestamp"],
                # timestamp/step_idx will be overwritten in run loop
                "timestamp": data.get("timestamp", 0.0),
                "step_idx": data.get("step_idx", 0),
            }

        self.camera = UvcCamera(
            shm_manager=self.shm_manager,
            dev_video_path=UVC_CONF.DEVICE,
            resolution=tuple(UVC_CONF.RESOLUTION),
            capture_fps=UVC_CONF.FPS,
            exposure=UVC_CONF.EXPOSURE,
            fourcc=UVC_CONF.FOURCC,
            cap_buffer_size=UVC_CONF.CAP_BUFFER_SIZE,
            put_fps=UVC_CONF.FPS,
            put_downsample=UVC_CONF.PUT_RATE_REGULATE,
            recording_transform=None,
            transform=meta_transform,
            video_recorder=recorder,
            verbose=False,
        )
        self.camera.start(wait=True)
        logger.info("UVC camera started.")
        if self.preview_enabled:
            self._start_preview()

    def on_shutdown(self):
        self._stop_preview()
        self._stop_sidecar()
        if self.camera:
            try:
                self.camera.stop(wait=True)
            except Exception as exc:
                logger.error(f"Camera stop failed: {exc}")
        if self.shm_manager:
            try:
                self.shm_manager.shutdown()
            except Exception:
                pass

    # Preview ----------------------------------------------------------------
    def _start_preview(self):
        """Start the preview thread if display is available and camera is ready."""
        if os.environ.get("DISPLAY") is None:
            logger.warning("[Preview] No display available, skipping preview")
            return

        # Check camera is alive and ready
        if not self.camera or not self.camera.is_alive():
            logger.warning("[Preview] Camera not available, skipping preview")
            return

        self._stop_preview()  # Stop any existing preview
        self.preview_thread = UvcPreviewThread(
            camera=self.camera,
            gripper_id=self.gripper_id,
            fps=PREVIEW_CONF.UVC_PREVIEW_FPS,
        )
        self.preview_thread.start()
        logger.info("[Preview] UVC preview thread started")

    def _stop_preview(self):
        """Stop the preview thread."""
        if self.preview_thread:
            self.preview_thread.stop()
            self.preview_thread.join(timeout=2.0)
        self.preview_thread = None

    # Recovery ----------------------------------------------------------------
    def can_recover(self, exc: Exception) -> bool:
        """
        Determine if recovery should be attempted for this exception.

        Recoverable:
        - RuntimeError: Camera process crash, ring buffer errors
        - OSError: USB disconnect, device not found
        - TimeoutError: Ring buffer rate limit (136Hz burst issue)

        Not recoverable:
        - KeyboardInterrupt, SystemExit: User-initiated termination
        """
        recoverable_types = (RuntimeError, OSError, TimeoutError)
        return isinstance(exc, recoverable_types)

    def _cleanup_for_recovery(self):
        """Pre-recovery cleanup: stop sidecar and camera process."""
        logger.info("[Recovery] Cleaning up old resources...")

        # 1. Stop sidecar thread
        self._stop_sidecar()

        # 2. Stop camera process (including VideoRecorder)
        if self.camera:
            try:
                # Stop recording if active
                if self.camera.video_recorder and self.camera.video_recorder.is_recording():
                    self.camera.stop_recording()

                # Signal camera to stop (non-blocking)
                self.camera.stop(wait=False)
            except Exception as exc:
                logger.warning(f"[Recovery] Camera cleanup warning: {exc}")

    def _reinitialize_camera(self):
        """
        Fully reinitialize camera system to clear accumulated state.

        This terminates all camera/recorder processes and creates fresh instances,
        ensuring no shared memory corruption can persist across recording cycles.
        """
        logger.info("[Reinit] Stopping camera system...")

        # 1. Stop preview thread (before camera stops)
        self._stop_preview()

        # 2. Stop sidecar thread
        self._stop_sidecar()

        # 2. Stop camera process (this also stops VideoRecorder)
        if self.camera:
            try:
                # Stop recording if active
                if self.camera.video_recorder and self.camera.video_recorder.is_recording():
                    self.camera.stop_recording()

                self.camera.stop(wait=False)
                if self.camera.is_alive():
                    self.camera.join(timeout=3.0)
                    if self.camera.is_alive():
                        logger.warning("[Reinit] Camera process didn't exit cleanly, terminating...")
                        self.camera.terminate()
                        self.camera.join(timeout=1.0)
            except Exception as exc:
                logger.warning(f"[Reinit] Camera stop warning: {exc}")

        # 3. Shutdown old SharedMemoryManager
        if self.shm_manager:
            try:
                self.shm_manager.shutdown()
            except Exception:
                pass

        # 4. Create fresh SharedMemoryManager
        self.shm_manager = SharedMemoryManager()
        self.shm_manager.start()

        # 5. Create fresh VideoRecorder
        recorder = _build_video_recorder(UVC_CONF.FPS)

        # 6. Create fresh UvcCamera with new recorder
        def meta_transform(data):
            return {
                "camera_receive_timestamp": data["camera_receive_timestamp"],
                "camera_capture_timestamp": data["camera_capture_timestamp"],
                "timestamp": data.get("timestamp", 0.0),
                "step_idx": data.get("step_idx", 0),
            }

        self.camera = UvcCamera(
            shm_manager=self.shm_manager,
            dev_video_path=UVC_CONF.DEVICE,
            resolution=tuple(UVC_CONF.RESOLUTION),
            capture_fps=UVC_CONF.FPS,
            exposure=UVC_CONF.EXPOSURE,
            fourcc=UVC_CONF.FOURCC,
            cap_buffer_size=UVC_CONF.CAP_BUFFER_SIZE,
            put_fps=UVC_CONF.FPS,
            put_downsample=UVC_CONF.PUT_RATE_REGULATE,
            recording_transform=None,
            transform=meta_transform,
            video_recorder=recorder,
            verbose=False,
        )

        # 7. Start fresh camera
        self.camera.start(wait=True)
        logger.info("[Reinit] Camera system reinitialized")

        # 8. Reset operational state
        self.current_video = None
        self.current_meta = None

        # 9. Restart preview if enabled
        if self.preview_enabled:
            self._start_preview()

    def on_recover(self):
        """Reinitialize the entire camera system."""
        logger.info("[Recovery] Reinitializing camera system...")
        self._reinitialize_camera()
        logger.info("[Recovery] Camera system reinitialized successfully")

    def after_recover(self):
        """Post-recovery state reset (state already reset by _reinitialize_camera)."""
        # Note: is_recording, run_id, episode are reset by base class in _attempt_recovery
        pass

    def main_loop(self):
        while self.is_running:
            time.sleep(0.1)

    # Recording ---------------------------------------------------------------
    def _episode_paths(self, run_id: str, episode: Optional[int]) -> Tuple[Path, Path]:
        ep_val = episode if episode is not None else 1
        ep_tag = f"ep{int(ep_val):03d}"
        run_dir = STORAGE_CONF.DATA_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        video_path = run_dir / f"{run_id}_{ep_tag}_{self.gripper_id}_uvc.MP4"
        meta_path = run_dir / f"{run_id}_{ep_tag}_{self.gripper_id}_uvc.jsonl"
        return video_path, meta_path

    def _start_sidecar(self, meta_path):
        self._stop_sidecar()
        self.sidecar_stop = threading.Event()
        try:
            # Drop pre-record frames so sidecar aligns with the new recording.
            self.camera.ring_buffer.clear()
        except Exception:
            pass
        writer = SidecarWriter(path=meta_path, ring_buffer=self.camera.ring_buffer, stop_event=self.sidecar_stop)
        self.sidecar_thread = writer.start()

    def _stop_sidecar(self):
        if self.sidecar_stop:
            self.sidecar_stop.set()
        if self.sidecar_thread:
            self.sidecar_thread.join(timeout=2.0)
        self.sidecar_stop = None
        self.sidecar_thread = None

    def on_prepare(self, run_id, episode=None):
        if not self.camera or not self.camera.is_alive():
            logger.error("Camera process not running.")
            return False
        # Check VideoRecorder health before accepting prepare
        if self.camera.video_recorder and not self.camera.video_recorder.is_alive():
            logger.error("VideoRecorder process not running.")
            return False
        # Wait briefly for camera to report ready
        for _ in range(20):
            if self.camera.is_ready:
                break
            time.sleep(0.05)
        if not self.camera.is_ready:
            logger.error("Camera not ready yet.")
            return False
        # Prepare VideoRecorder for new recording (clears stop_writing_event)
        if self.camera.video_recorder:
            try:
                self.camera.video_recorder.prepare_recording()
            except Exception as exc:
                logger.error(f"Prepare recording failed: {exc}")
                return False
        # Pre-create directories
        video_path, meta_path = self._episode_paths(run_id, episode)
        try:
            video_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error(f"Prepare failed to create directory: {exc}")
            return False
        return True

    def on_start_recording(self, run_id, episode=None, start_time=None):
        # Check VideoRecorder health before starting
        if self.camera and self.camera.video_recorder:
            vr = self.camera.video_recorder
            if not vr.is_alive():
                logger.error("[Record] VideoRecorder process died! Cannot start recording.")
                raise RuntimeError("VideoRecorder process died")

            # Wait for VideoRecorder to be ready (not recording)
            for _ in range(30):  # Max 3 seconds
                if vr.is_ready() and not vr.is_recording():
                    break
                time.sleep(0.1)
            else:
                logger.warning("[Record] VideoRecorder not ready, proceeding anyway")

        video_path, meta_path = self._episode_paths(run_id, episode)
        self.current_video = video_path
        self.current_meta = meta_path
        logger.info(f"[Record] START {video_path.name}")
        self._start_sidecar(meta_path)
        # Pass start_time straight through; UvcCamera uses -1 for immediate
        st = start_time if start_time is not None else -1
        self.camera.start_recording(str(video_path), start_time=st)
        self.publish_status()

    def on_stop_recording(self):
        logger.info("[Record] STOP requested.")
        self.status = NodeStatus.SAVING
        self.publish_status()
        try:
            self.camera.stop_recording()
            # Wait for VideoRecorder to fully enter idle state
            if self.camera and self.camera.video_recorder:
                if not self.camera.video_recorder.wait_idle(timeout=3.0):
                    logger.warning("[Record] VideoRecorder did not reach idle state")
        finally:
            self._stop_sidecar()
            self.status = NodeStatus.IDLE
            self.publish_status()
            self.current_video = None
            self.current_meta = None
            # Reinitialize camera to prevent SIGSEGV from shared memory corruption
            self._reinitialize_camera()

    def on_discard_run(self, run_id, episode=None):
        if self.is_recording:
            try:
                self.camera.stop_recording()
            except Exception:
                pass
            self._stop_sidecar()

        # Delete episode files
        ep_tag = f"ep{int(episode):03d}" if episode is not None else None
        patterns = []
        if ep_tag:
            patterns = [f"{run_id}_{ep_tag}_{self.gripper_id}_uvc.MP4", f"{run_id}_{ep_tag}_{self.gripper_id}_uvc.jsonl"]
        else:
            patterns = [f"{run_id}_*_{self.gripper_id}_uvc.MP4", f"{run_id}_*_{self.gripper_id}_uvc.jsonl"]

        run_dir = STORAGE_CONF.DATA_DIR / run_id
        for pattern in patterns:
            for path in run_dir.glob(pattern):
                try:
                    path.unlink()
                    logger.info(f"[Discard] Removed {path}")
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.error(f"[Discard] Failed to delete {path}: {exc}")

        # Reinitialize camera system to prevent SIGSEGV on next recording
        # This clears all shared memory state that may have become corrupted
        self._reinitialize_camera()

    # Health/status -----------------------------------------------------------
    def check_hardware_health(self):
        if not self.camera or not self.camera.is_alive():
            raise RuntimeError("Camera process stopped.")
        if not self.camera.is_ready:
            raise RuntimeError("Camera not ready.")
        # Check VideoRecorder health - triggers recovery if dead
        if self.camera.video_recorder and not self.camera.video_recorder.is_alive():
            raise RuntimeError("VideoRecorder process stopped.")

    def extra_status(self):
        return {
            "video_path": str(self.current_video) if self.current_video else None,
        }


def validate(run_id: str, episode: int, gripper_id: str = None):
    """
    Basic UVC validator: check video + sidecar presence and rough duration match.
    """
    from validator import ValidationResult  # Local import to avoid cycles
    from validator import check_video_decoding, get_video_duration

    gripper_id = gripper_id or get_default_gripper_id()
    ep_tag = f"ep{int(episode):03d}"
    run_dir = STORAGE_CONF.DATA_DIR / run_id
    video_path = run_dir / f"{run_id}_{ep_tag}_{gripper_id}_uvc.MP4"
    sidecar_path = run_dir / f"{run_id}_{ep_tag}_{gripper_id}_uvc.jsonl"

    if not video_path.exists():
        return ValidationResult(False, "video_missing", f"UVC video missing: {video_path.name}")
    if not sidecar_path.exists():
        return ValidationResult(False, "meta_missing", f"UVC sidecar missing: {sidecar_path.name}")

    if not check_video_decoding(video_path):
        return ValidationResult(False, "video_corrupt", "UVC video decode failed")

    duration = get_video_duration(video_path)
    if duration is None or duration <= 0:
        return ValidationResult(False, "video_invalid", "Cannot read UVC video duration")

    try:
        with sidecar_path.open() as fh:
            timestamps = [json.loads(line).get("timestamp") for line in fh]
    except Exception as exc:
        return ValidationResult(False, "meta_corrupt", f"UVC sidecar invalid: {exc}")

    timestamps = [t for t in timestamps if t is not None]
    if not timestamps:
        return ValidationResult(False, "meta_empty", "UVC sidecar empty")

    ts_span = max(timestamps) - min(timestamps)
    # Allow generous slack for encoder start/stop drift
    if abs(ts_span - duration) > 1.0:
        return ValidationResult(False, "sync_error", f"Video duration {duration:.2f}s vs meta span {ts_span:.2f}s")

    return ValidationResult(True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gripper-id", default=None, help="Gripper ID (e.g., gp00)")
    parser.add_argument("--port", type=int, default=HTTP_CONF.UVC_PORT, help="HTTP port")
    parser.add_argument("--preview", action="store_true", help="Enable real-time preview")
    args = parser.parse_args()

    node = UvcNode(gripper_id=args.gripper_id, port=args.port, preview=args.preview)
    node.start()
