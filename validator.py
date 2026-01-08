import importlib
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from zumi_config import STORAGE_CONF

logging.basicConfig(level=logging.INFO, format="[VALIDATOR] %(message)s")
logger = logging.getLogger("Validator")


# -----------------------------------------------------------------------------
# Result type
# -----------------------------------------------------------------------------


@dataclass
class ValidationResult:
    success: bool
    error: Optional[str] = None  # "video_missing", "motor_missing", "video_corrupt", etc.
    message: Optional[str] = None


# -----------------------------------------------------------------------------
# Utility helpers (shared by node validators)
# -----------------------------------------------------------------------------


def check_video_decoding(video_path: Path, seconds: int = 5) -> bool:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-t",
        str(seconds),
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg decoding failed: {e.stderr.decode()}")
        return False


def get_video_duration(video_path: Path) -> Optional[float]:
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        out = subprocess.check_output(cmd).decode().strip()
        return float(out)
    except Exception as exc:
        logger.warning(f"Could not read duration for {video_path}: {exc}")
        return None


def get_video_creation_time(file_path: Path) -> Optional[float]:
    """
    Get creation_time from video metadata via ffprobe (UTC timestamp).
    """
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream_tags=creation_time",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        output = subprocess.check_output(cmd).decode().strip()
        if output:
            dt = datetime.strptime(output, "%Y-%m-%dT%H:%M:%S.%fZ")
            return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception as exc:
        logger.warning(f"Could not get video creation time: {exc}")
    return None


def extract_imu(video_path: Path, json_path: Path) -> bool:
    video_path = Path(video_path).resolve()
    json_path = Path(json_path).resolve()

    try:
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{video_path.parent}:/data",
            "chicheng/openicc",
            "node",
            "/OpenImuCameraCalibrator/javascript/extract_metadata_single.js",
            f"/data/{video_path.name}",
            f"/data/{json_path.name}",
        ]

        subprocess.run(docker_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        logger.error("Docker extraction failed.")
        return False
    except FileNotFoundError:
        logger.error("Docker not found.")
        return False
    except Exception as exc:
        logger.error(f"Error preparing docker command: {exc}")
        return False


def get_imu_start_time(json_path: Path) -> Optional[float]:
    try:
        with open(json_path, "r") as f:
            data = json.load(f)

        for key, val in data.items():
            if isinstance(val, dict) and "streams" in val:
                streams = val["streams"]
                if "ACCL" in streams and "samples" in streams["ACCL"]:
                    samples = streams["ACCL"]["samples"]
                    if samples:
                        date_str = samples[0].get("date")
                        if date_str:
                            if date_str.endswith("Z"):
                                date_str = date_str.replace("Z", "+00:00")
                            return datetime.fromisoformat(date_str).timestamp()

        if "start_time" in data:
            return float(data["start_time"])

    except Exception as exc:
        logger.warning(f"Failed to parse IMU JSON: {exc}")
    return None


# -----------------------------------------------------------------------------
# Runner
# -----------------------------------------------------------------------------


ValidatorFn = Callable[[str, Optional[int]], ValidationResult]
DEFAULT_VALIDATORS = ["node_gopro", "node_motor", "node_uvc"]


def _load_validators(modules: List[str]) -> List[Tuple[str, ValidatorFn]]:
    validators: List[Tuple[str, ValidatorFn]] = []
    for mod_name in modules:
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            logger.info(f"Validator module not found: {mod_name}")
            continue
        except Exception as exc:
            logger.error(f"Failed to import validator {mod_name}: {exc}")
            continue

        fn = getattr(mod, "validate", None)
        if not callable(fn):
            logger.warning(f"Validator {mod_name} missing callable validate()")
            continue
        validators.append((mod_name, fn))
    return validators


def validate(run_id: str, episode: Optional[int] = None) -> ValidationResult:
    validators = _load_validators(DEFAULT_VALIDATORS)
    if not validators:
        return ValidationResult(False, "validator_missing", "No validators registered")

    for name, fn in validators:
        try:
            result = fn(run_id, episode)
        except Exception as exc:
            logger.error(f"{name} validator crashed: {exc}")
            return ValidationResult(False, "validation_error", f"{name} error: {exc}")

        if not isinstance(result, ValidationResult):
            logger.warning(f"{name} returned unexpected result, skipping")
            continue

        if not result.success:
            # Preserve error/message but annotate source
            if result.message:
                msg = f"{name}: {result.message}"
            else:
                msg = f"{name} validation failed"
            return ValidationResult(False, result.error or "validation_failed", msg)

    return ValidationResult(True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        run_id_arg = sys.argv[1]
        ep_arg = None
        if len(sys.argv) > 2:
            try:
                ep_arg = int(sys.argv[2])
            except ValueError:
                ep_arg = None
        result = validate(run_id_arg, ep_arg)
        print(result)
    else:
        print("Usage: python validator.py <run_id> [episode]")
