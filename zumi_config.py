import os
import platform
from enum import Enum
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Optional

_IS_MAC = platform.system() == "Darwin"


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
    DRIVER: str = "dm"
    SLAVE_ID: int = 0x16
    MASTER_ID: int = 0x26
    # Mac: /dev/tty.usbserial-XXXX  Linux: /dev/dm_can0
    # Override with env var ZUMI_SERIAL_PORT
    SERIAL_PORT: str = os.environ.get(
        "ZUMI_SERIAL_PORT",
        "/dev/tty.usbserial-0001" if _IS_MAC else "/dev/dm_can0"
    )
    TARGET_FREQ: float = 150.0  # Motor control loop frequency (Hz)
    LOCK_DURATION: float = 0  # Lock gripper position for first N seconds (0 = no lock)


@dataclass
class GoProConfig:
    SN: str = None  # Serial number (optional, for IP derivation)
    IP: str = None  # Direct IP (optional, auto-discover if None)


@dataclass
class GripperMapping:
    """Gripper 与设备的映射关系"""
    GRIPPER_ID: str = "gp00"           # gripper 标识符
    MOTOR_SLAVE_ID: int = 0x16         # 对应的电机从地址
    GOPRO_SN: str = None               # 对应的 GoPro 序列号（可选）
    GOPRO_IP: str = None               # 对应的 GoPro IP（可选）
    UVC_DEVICE: str = None             # 对应的 UVC 设备（可选）


# 单臂配置（当前使用）
GRIPPER_MAPPINGS: Dict[str, GripperMapping] = {
    "gp00": GripperMapping(
        GRIPPER_ID="gp00",
        MOTOR_SLAVE_ID=0x16,
        UVC_DEVICE="/dev/v4l/by-id/usb-DCX-250107-ZW_DECXIN-video-index0"
    )
}


def get_default_gripper_id() -> str:
    """获取默认 gripper ID（第一个配置的）"""
    return next(iter(GRIPPER_MAPPINGS.keys()), "gp00")


def get_gripper_mapping(gripper_id: str) -> Optional[GripperMapping]:
    """根据 gripper_id 获取映射配置"""
    return GRIPPER_MAPPINGS.get(gripper_id)


@dataclass
class PreviewConfig:
    """Real-time preview settings for motor and UVC nodes."""
    MOTOR_PREVIEW_FPS: int = 30        # Preview update rate
    MOTOR_DECIMATION: int = 5          # Send every N samples (150Hz/5 = 30Hz)
    MOTOR_BUFFER_SIZE: int = 300       # Plot history (~10s at 30fps)
    MOTOR_QUEUE_SIZE: int = 100        # IPC queue depth
    UVC_PREVIEW_FPS: int = 30          # UVC display framerate


@dataclass
class UvcConfig:
    # Linux: /dev/v4l/by-id/usb-XXX-video-index0
    # Mac:   camera index as string, e.g. "0" "1" "2"
    # Override with env var ZUMI_UVC_DEVICE
    DEVICE: str = os.environ.get(
        "ZUMI_UVC_DEVICE",
        "0" if _IS_MAC else "/dev/v4l/by-id/usb-DCX-250107-ZW_DECXIN-video-index0"
    )
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
PREVIEW_CONF = PreviewConfig()

STORAGE_CONF.DATA_DIR.mkdir(exist_ok=True, parents=True)
