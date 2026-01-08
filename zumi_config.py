import os
from enum import Enum
from pathlib import Path
from dataclasses import dataclass


class NodeStatus(str, Enum):
    INIT = "INIT"
    IDLE = "IDLE"
    READY = "READY"
    RECORDING = "RECORDING"
    SAVING = "SAVING"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"
    RECOVERING = "RECOVERING"


@dataclass
class StorageConfig:
    DATA_DIR: Path = Path("data")


@dataclass
class ZMQConfig:
    ORCHESTRATOR_IP: str = "127.0.0.1"
    STATUS_PORT: int = 5556


@dataclass
class HttpNodeConfig:
    GOPRO_URL: str = "http://127.0.0.1:8001"
    MOTOR_URL: str = "http://127.0.0.1:8002"
    UVC_URL: str = "http://127.0.0.1:8003"
    GOPRO_HOST: str = "0.0.0.0"
    GOPRO_PORT: int = 8001
    MOTOR_HOST: str = "0.0.0.0"
    MOTOR_PORT: int = 8002
    UVC_HOST: str = "0.0.0.0"
    UVC_PORT: int = 8003


@dataclass
class MotorConfig:
    DRIVER: str = "dm"  # "dm" or "mock"
    SLAVE_ID: int = 0x16
    MASTER_ID: int = 0x26
    SERIAL_PORT: str = "/dev/dm_can0"


@dataclass
class GoProConfig:
    SN: str = None  # Serial number (optional, for IP derivation)
    IP: str = None  # Direct IP (optional, auto-discover if None)


@dataclass
class UvcConfig:
    # Recommend stable path e.g. /dev/v4l/by-id/usb-icSpring_icspring_camera_20240307110322-video-index0
    DEVICE: str = "/dev/v4l/by-id/usb-DCX-250107-ZW_DECXIN-video-index0"
    RESOLUTION: tuple = (640, 480)
    FPS: int = 60
    FOURCC: str = "MJPG"  # "MJPG" for 60fps, "YUYV" for lower fps
    EXPOSURE: float = 10.0  # Manual exposure value
    BACKEND: str = "local"  # "local" or "remote" placeholder
    # V4L2 capture buffer size. Some drivers limit FPS when BUFFERSIZE=1.
    # Default 4 balances latency (~66ms @ 60fps) and full frame rate.
    CAP_BUFFER_SIZE: int = 4
    # Frame rate regulation: when enabled, aligns output to FPS target.
    # - If actual FPS > target: downsample (skip frames)
    # - If actual FPS < target: repeat frames to fill time slots
    PUT_RATE_REGULATE: bool = True


STORAGE_CONF = StorageConfig()
HTTP_CONF = HttpNodeConfig()
ZMQ_CONF = ZMQConfig()
MOTOR_CONF = MotorConfig()
GOPRO_CONF = GoProConfig()
UVC_CONF = UvcConfig()

STORAGE_CONF.DATA_DIR.mkdir(exist_ok=True, parents=True)
