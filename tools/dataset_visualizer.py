#!/usr/bin/env python3
"""
Dataset Visualizer - 使用 Rerun 的数据集可视化工具

同时显示两个相机视频、IMU/电机数据曲线、AprilTag 检测结果和 Gripper Width

用法:
    python dataset_visualizer.py /path/to/demo_folder
    python dataset_visualizer.py --demo /path/to/demo_folder
    python dataset_visualizer.py --demo /path/to/demo --block  # 保持进程运行
"""

import argparse
import json
import logging
import pickle
import time
from pathlib import Path

import cv2
import numpy as np
import rerun as rr
import tqdm

LOGGER = logging.getLogger(__name__)


def load_imu_data(imu_path: Path) -> dict:
    """加载 IMU 数据，提取 ACCL 和 GYRO"""
    with open(imu_path, 'r') as f:
        data = json.load(f)

    streams = data.get('1', {}).get('streams', {})
    result = {'accl': [], 'gyro': [], 'accl_t': [], 'gyro_t': []}

    # 提取 ACCL 数据
    if 'ACCL' in streams:
        samples = streams['ACCL'].get('samples', [])
        for s in samples:
            result['accl'].append(s['value'])
            result['accl_t'].append(s['cts'] / 1000.0)  # cts 毫秒 -> 秒

    # 提取 GYRO 数据
    if 'GYRO' in streams:
        samples = streams['GYRO'].get('samples', [])
        for s in samples:
            result['gyro'].append(s['value'])
            result['gyro_t'].append(s['cts'] / 1000.0)

    result['accl'] = np.array(result['accl']) if result['accl'] else np.zeros((0, 3))
    result['gyro'] = np.array(result['gyro']) if result['gyro'] else np.zeros((0, 3))
    result['accl_t'] = np.array(result['accl_t'])
    result['gyro_t'] = np.array(result['gyro_t'])

    return result


def load_motor_data(motor_path: Path) -> dict:
    """加载电机数据"""
    result = {'pos': [], 'vel': [], 'tau': [], 't': []}

    with open(motor_path, 'r') as f:
        first_ts = None
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if first_ts is None:
                first_ts = d['ts']
            result['t'].append(d['ts'] - first_ts)
            result['pos'].append(d['pos'][0] if d['pos'] else 0)
            result['vel'].append(d['vel'][0] if d['vel'] else 0)
            result['tau'].append(d['tau'][0] if d['tau'] else 0)

    for key in result:
        result[key] = np.array(result[key])

    return result


def load_tag_detections(tag_path: Path) -> list:
    """加载 AprilTag 检测数据"""
    if not tag_path.exists():
        return []

    with open(tag_path, 'rb') as f:
        data = pickle.load(f)

    return data if isinstance(data, list) else []


def compute_gripper_width(tag_dict: dict, left_id: int = 0, right_id: int = 1,
                          nominal_z: float = 0.072, z_tolerance: float = 0.008) -> float | None:
    """
    计算 gripper width (基于两个 tag 的 tvec)

    参考: umi/common/cv_util.py:get_gripper_width()
    """
    zmax = nominal_z + z_tolerance
    zmin = nominal_z - z_tolerance

    left_x = None
    if left_id in tag_dict:
        tvec = tag_dict[left_id]['tvec']
        # 检查深度是否合理 (过滤异常值)
        if zmin < tvec[-1] < zmax:
            left_x = tvec[0]

    right_x = None
    if right_id in tag_dict:
        tvec = tag_dict[right_id]['tvec']
        if zmin < tvec[-1] < zmax:
            right_x = tvec[0]

    width = None
    if (left_x is not None) and (right_x is not None):
        width = right_x - left_x
    elif left_x is not None:
        width = abs(left_x) * 2
    elif right_x is not None:
        width = abs(right_x) * 2
    return width


def visualize_demo(demo_path: Path, block: bool = True) -> None:
    """可视化单个 demo"""

    raw_video_path = demo_path / "raw_video.mp4"
    uvc_video_path = demo_path / "uvc_video.mp4"
    imu_path = demo_path / "imu_data.json"
    motor_path = demo_path / "motor_data.jsonl"
    tag_path = demo_path / "tag_detection.pkl"

    # 检查文件存在
    for p in [raw_video_path, uvc_video_path, imu_path, motor_path]:
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {p}")

    demo_name = demo_path.name

    # 加载数据
    LOGGER.info("加载 IMU 数据...")
    imu_data = load_imu_data(imu_path)
    LOGGER.info(f"  ACCL: {len(imu_data['accl'])} 样本, GYRO: {len(imu_data['gyro'])} 样本")

    LOGGER.info("加载电机数据...")
    motor_data = load_motor_data(motor_path)
    LOGGER.info(f"  Motor: {len(motor_data['t'])} 样本")

    LOGGER.info("加载 AprilTag 检测数据...")
    tag_detections = load_tag_detections(tag_path)
    if tag_detections:
        tag_ids = set()
        for det in tag_detections:
            tag_ids.update(det['tag_dict'].keys())
        LOGGER.info(f"  Tags: {len(tag_detections)} 帧, IDs: {sorted(tag_ids)}")
    else:
        LOGGER.info("  未找到 tag_detection.pkl")

    # 创建 tag 检测的帧索引映射
    tag_by_frame = {}
    for det in tag_detections:
        tag_by_frame[det['frame_idx']] = det['tag_dict']

    # 打开视频
    LOGGER.info("打开视频...")
    raw_cap = cv2.VideoCapture(str(raw_video_path))
    uvc_cap = cv2.VideoCapture(str(uvc_video_path))

    fps = uvc_cap.get(cv2.CAP_PROP_FPS) or 60
    total_frames = int(uvc_cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps

    LOGGER.info(f"视频: {total_frames} 帧, {duration:.2f} 秒, {fps:.2f} fps")

    # 初始化 Rerun
    LOGGER.info("启动 Rerun...")
    rr.init(f"zumi/{demo_name}", spawn=True)

    # 记录视频帧和传感器数据
    LOGGER.info("记录数据到 Rerun...")

    # 预计算 IMU 和电机数据的索引映射
    imu_accl_idx = 0
    imu_gyro_idx = 0
    motor_idx = 0

    for frame_idx in tqdm.tqdm(range(total_frames), desc="处理帧"):
        t = frame_idx / fps
        rr.set_time_seconds("timestamp", t)
        rr.set_time_sequence("frame", frame_idx)

        # 读取 GoPro 帧
        ret1, raw_frame = raw_cap.read()
        if ret1:
            # 缩放大尺寸视频
            h, w = raw_frame.shape[:2]
            if w > 960:
                scale = 960 / w
                raw_frame = cv2.resize(raw_frame, (int(w * scale), int(h * scale)))
            raw_frame_rgb = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2RGB)
            rr.log("camera/gopro", rr.Image(raw_frame_rgb))

        # 读取 UVC 帧
        ret2, uvc_frame = uvc_cap.read()
        if ret2:
            uvc_frame_rgb = cv2.cvtColor(uvc_frame, cv2.COLOR_BGR2RGB)
            rr.log("camera/uvc", rr.Image(uvc_frame_rgb))

        # 记录 ACCL 数据 (找到最接近当前时间的样本)
        while imu_accl_idx < len(imu_data['accl_t']) - 1 and imu_data['accl_t'][imu_accl_idx + 1] <= t:
            imu_accl_idx += 1
        if imu_accl_idx < len(imu_data['accl']):
            accl = imu_data['accl'][imu_accl_idx]
            rr.log("imu/accl_x", rr.Scalar(accl[0]))
            rr.log("imu/accl_y", rr.Scalar(accl[1]))
            rr.log("imu/accl_z", rr.Scalar(accl[2]))

        # 记录 GYRO 数据
        while imu_gyro_idx < len(imu_data['gyro_t']) - 1 and imu_data['gyro_t'][imu_gyro_idx + 1] <= t:
            imu_gyro_idx += 1
        if imu_gyro_idx < len(imu_data['gyro']):
            gyro = imu_data['gyro'][imu_gyro_idx]
            rr.log("imu/gyro_x", rr.Scalar(gyro[0]))
            rr.log("imu/gyro_y", rr.Scalar(gyro[1]))
            rr.log("imu/gyro_z", rr.Scalar(gyro[2]))

        # 记录电机数据
        while motor_idx < len(motor_data['t']) - 1 and motor_data['t'][motor_idx + 1] <= t:
            motor_idx += 1
        if motor_idx < len(motor_data['t']):
            rr.log("motor/pos", rr.Scalar(motor_data['pos'][motor_idx]))
            rr.log("motor/vel", rr.Scalar(motor_data['vel'][motor_idx]))
            rr.log("motor/tau", rr.Scalar(motor_data['tau'][motor_idx]))

        # 记录 AprilTag 检测数据
        if frame_idx in tag_by_frame:
            tag_dict = tag_by_frame[frame_idx]

            # 计算 gripper width (使用更宽松的 z 范围)
            gripper_width = compute_gripper_width(tag_dict, left_id=0, right_id=1,
                                                   nominal_z=0.05, z_tolerance=0.05)
            if gripper_width is not None:
                rr.log("gripper/width", rr.Scalar(gripper_width))

            for tag_id, tag_data in tag_dict.items():
                # 记录 tag 的 3D 位置 (tvec)
                tvec = tag_data['tvec']
                rr.log(f"tags/tag_{tag_id}/position", rr.Points3D([tvec], radii=0.01))

                # 记录 tag 的旋转向量 (rvec) 作为标量
                rvec = tag_data['rvec']
                rr.log(f"tags/tag_{tag_id}/rvec_x", rr.Scalar(rvec[0]))
                rr.log(f"tags/tag_{tag_id}/rvec_y", rr.Scalar(rvec[1]))
                rr.log(f"tags/tag_{tag_id}/rvec_z", rr.Scalar(rvec[2]))

    raw_cap.release()
    uvc_cap.release()

    LOGGER.info("数据记录完成!")

    if block:
        LOGGER.info("按 Ctrl+C 退出...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            LOGGER.info("退出")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="数据集可视化工具 (Rerun)")
    parser.add_argument("demo_path", nargs="?", help="Demo 文件夹路径")
    parser.add_argument("--demo", help="Demo 文件夹路径 (替代位置参数)")
    parser.add_argument(
        "--block",
        action="store_true",
        help="记录完成后保持进程运行 (默认立即返回)",
    )

    args = parser.parse_args()

    demo_path = args.demo or args.demo_path
    if not demo_path:
        parser.error("请指定 demo 文件夹路径")

    demo_path = Path(demo_path)
    if not demo_path.exists():
        LOGGER.error(f"路径不存在: {demo_path}")
        return

    try:
        visualize_demo(demo_path, block=args.block)
    except FileNotFoundError as e:
        LOGGER.error(str(e))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
