#!/usr/bin/env python3
"""
实时可视化 ArUco tag 检测 + Motor 位置信号
用法：python scripts/test_aruco_realtime.py <video_path>

使用 .env 中的配置文件
"""

import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT_DIR)
os.chdir(ROOT_DIR)

import cv2
import numpy as np
import json
import yaml
from pathlib import Path
from collections import deque
from dotenv import load_dotenv
import bisect
import scipy.ndimage as sn

from umi.common.cv_util import (
    parse_aruco_config,
    parse_fisheye_intrinsics,
    convert_fisheye_intrinsics_resolution,
    detect_localize_aruco_tags,
    draw_predefined_mask,
    get_gripper_width,
    FisheyeRectConverter
)

# 加载 .env 配置
env_path = Path(ROOT_DIR) / 'scripts_slam_pipeline' / '.env'
load_dotenv(env_path)

CAMERA_INTR = os.getenv('CAMERA_INTR')
ARUCO_YAML = os.getenv('ARUCO_YAML')
SESSION_DIR = os.getenv('SESSION_DIR')

print(f"Camera intrinsics: {CAMERA_INTR}")
print(f"ArUco config: {ARUCO_YAML}")


def load_motor_data(motor_path):
    """加载 motor JSONL 数据，返回 (timestamps, positions) 数组"""
    timestamps = []
    positions = []
    with open(motor_path, 'r') as f:
        for line in f:
            data = json.loads(line)
            timestamps.append(data['ts'])
            positions.append(data['pos'][0])  # 取第一个位置值
    return np.array(timestamps), np.array(positions)


def exp_smooth(x, alpha=0.3):
    """简单的指数平滑"""
    result = np.zeros_like(x)
    result[0] = x[0]
    for i in range(1, len(x)):
        result[i] = alpha * x[i] + (1 - alpha) * result[i-1]
    return result


def align_motor_to_tag(tag_widths, tag_timestamps, motor_ts, motor_pos, motor_scale_sign, fps):
    """
    使用 cross-correlation 对齐 motor 信号到 tag 信号，返回:
    - t_offset: 时间偏移
    - aligned_motor_widths: 对齐并转换为米的 motor 宽度
    - ratio, offset: 线性映射参数
    """
    # 重采样 motor 到 tag 时间戳
    motor_ts_rel = motor_ts - motor_ts[0]
    motor_resampled = np.interp(tag_timestamps, motor_ts_rel, motor_pos)

    # 应用极性校正
    if motor_scale_sign < 0:
        motor_for_corr = -motor_resampled
    else:
        motor_for_corr = motor_resampled

    # 1. 只用前一半数据做对齐
    n_samples = len(tag_widths) // 2
    tag_short = tag_widths[:n_samples]
    motor_short = motor_for_corr[:n_samples]

    # 直接差分，不做平滑
    tag_diff = np.diff(tag_short)
    motor_diff = np.diff(motor_short)

    # 过滤掉 motor 变化小于0.1rad的部分
    min_change = 0.1  # rad
    mask = np.abs(motor_diff) >= min_change
    tag_diff = np.where(mask, tag_diff, 0)
    motor_diff = np.where(mask, motor_diff, 0)

    # 归一化导数
    tag_diff_range = np.percentile(np.abs(tag_diff), 98)
    motor_diff_range = np.percentile(np.abs(motor_diff), 98)
    if tag_diff_range < 1e-9 or motor_diff_range < 1e-9:
        return None, None, None, None

    tag_normalized = tag_diff / tag_diff_range
    motor_normalized = motor_diff / motor_diff_range

    # Cross-correlation
    correlation = np.correlate(tag_normalized, motor_normalized, mode='full')
    n = len(tag_normalized)
    lags = np.arange(-(n-1), n)

    # 限制搜索范围在 +/- 2秒内
    max_lag_sec = 2.0
    max_lag_frames = int(max_lag_sec * fps)
    center = n - 1  # lag=0 的位置
    search_start = max(0, center - max_lag_frames)
    search_end = min(len(correlation), center + max_lag_frames)

    # 在限制范围内找最大
    search_corr = correlation[search_start:search_end]
    best_local_idx = np.argmax(search_corr)
    best_lag_idx = search_start + best_local_idx
    best_lag = lags[best_lag_idx]
    t_offset = best_lag / fps

    max_corr = correlation[best_lag_idx] / n
    print(f"  Cross-correlation: max_corr={max_corr:.3f}, t_offset={t_offset:.3f}s (search range: +/-{max_lag_sec}s)")

    # 打印 correlation 曲线的几个峰值
    import matplotlib.pyplot as plt
    lag_sec = lags / fps
    plt.figure(figsize=(12, 4))
    mask = (lag_sec >= -max_lag_sec) & (lag_sec <= max_lag_sec)
    plt.plot(lag_sec[mask], correlation[mask])
    plt.axvline(x=t_offset, color='r', linestyle='--', label=f'best: {t_offset:.3f}s')
    plt.axvline(x=-0.4, color='g', linestyle='--', label='expected: -0.4s')
    plt.xlabel('lag (s)')
    plt.ylabel('correlation')
    plt.legend()
    plt.title('Cross-correlation curve')
    plt.savefig('/tmp/correlation.png')
    plt.close()
    print(f"  Correlation curve saved to /tmp/correlation.png")

    # Step 2: 用 tag 的 min/max 时刻建立线性映射
    valid_mask = ~np.isnan(tag_widths)
    valid_indices = np.where(valid_mask)[0]
    valid_widths = tag_widths[valid_mask]

    N = min(10, max(3, len(valid_widths) // 10))
    sorted_idx = np.argsort(valid_widths)
    min_region = valid_indices[sorted_idx[:N]]
    max_region = valid_indices[sorted_idx[-N:]]

    t_close = np.mean(tag_timestamps[min_region])
    t_open = np.mean(tag_timestamps[max_region])
    tag_close = np.mean(tag_widths[min_region])
    tag_open = np.mean(tag_widths[max_region])

    # 对齐后的 motor 时间戳
    motor_ts_aligned = motor_ts_rel + t_offset

    motor_at_close = float(np.interp(t_close, motor_ts_aligned, motor_pos))
    motor_at_open = float(np.interp(t_open, motor_ts_aligned, motor_pos))

    motor_diff = motor_at_open - motor_at_close
    tag_span = tag_open - tag_close

    if abs(motor_diff) < 1e-6 or tag_span < 0.001:
        return None, None, None, None

    ratio = tag_span / motor_diff
    offset = tag_close - motor_at_close * ratio

    print(f"  Linear mapping: ratio={ratio:.6f} m/rad, offset={offset:.4f}m")

    # 转换 motor 到物理宽度
    aligned_motor_widths = motor_pos * ratio + offset

    return t_offset, aligned_motor_widths, ratio, offset


def main():
    if len(sys.argv) < 2:
        # 默认使用第一个 demo 视频
        video_path = f"{SESSION_DIR}/demos/demo_ep003_gp00/raw_video.mp4"
    else:
        video_path = sys.argv[1]

    print(f"\nProcessing: {video_path}")

    # 尝试加载 motor 数据
    video_dir = Path(video_path).parent
    motor_path = video_dir / "motor_data.jsonl"
    motor_ts, motor_pos = None, None
    if motor_path.exists():
        motor_ts, motor_pos = load_motor_data(motor_path)
        print(f"Loaded motor data: {len(motor_ts)} samples, duration: {motor_ts[-1] - motor_ts[0]:.2f}s")
    else:
        print(f"Motor data not found: {motor_path}")

    # 尝试加载 gripper_range.json 获取 motor_scale_sign
    motor_scale_sign = -1  # 默认值
    gripper_range_path = video_dir.parent / "gripper00" / "gripper_range.json"
    if not gripper_range_path.exists():
        # 尝试其他可能的路径
        for p in video_dir.parent.glob("gripper*/gripper_range.json"):
            gripper_range_path = p
            break
    if gripper_range_path.exists():
        gripper_range = json.load(open(gripper_range_path, 'r'))
        motor_scale_sign = gripper_range.get('motor_scale_sign', -1)
        print(f"Loaded motor_scale_sign={motor_scale_sign} from {gripper_range_path.name}")

    # 加载配置
    aruco_config = parse_aruco_config(yaml.safe_load(open(ARUCO_YAML, 'r')))
    aruco_dict = aruco_config['aruco_dict']
    marker_size_map = aruco_config['marker_size_map']

    raw_fisheye_intr = parse_fisheye_intrinsics(json.load(open(CAMERA_INTR, 'r')))
    print(f"Raw intrinsics DIM: {raw_fisheye_intr['DIM']}")

    # 打开视频
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: Cannot open video {video_path}")
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"Video: {width}x{height} @ {fps:.1f}fps, {total_frames} frames")

    # 转换内参到视频分辨率
    fisheye_intr = convert_fisheye_intrinsics_resolution(
        opencv_intr_dict=raw_fisheye_intr, target_resolution=(width, height))
    print(f"Converted intrinsics DIM: {fisheye_intr['DIM']}")
    print(f"K:\n{fisheye_intr['K']}")
    print(f"D: {fisheye_intr['D'].T}")

    # 创建畸变校正器（用于可视化）
    display_scale = 0.3
    display_w, display_h = int(width * display_scale), int(height * display_scale)

    # 创建 undistort map 用于可视化校正后的图像
    K = fisheye_intr['K']
    D = fisheye_intr['D']

    # 计算显示用的 undistort map
    new_K = K.copy()
    new_K[0, 0] *= 0.5  # 减小焦距以看到更大的视野
    new_K[1, 1] *= 0.5
    map1, map2 = cv2.fisheye.initUndistortRectifyMap(
        K, D, np.eye(3), new_K, (width, height), cv2.CV_16SC2)

    # 夹爪手指 tag ID（gripper 0）
    left_id = 0
    right_id = 1
    nominal_z = 0.034  # 从 aruco_config.yaml 和 calibrate_gripper_range.py 获取

    # ========== 预处理：从 tag_detection.pkl 加载 tag 宽度 ==========
    import pickle
    pkl_path = video_dir / "tag_detection.pkl"
    if pkl_path.exists():
        print(f"\n加载 tag_detection.pkl...")
        tag_detection_results = pickle.load(open(pkl_path, 'rb'))
        all_tag_widths = []
        for td in tag_detection_results:
            width = get_gripper_width(td['tag_dict'], left_id, right_id, nominal_z=nominal_z)
            all_tag_widths.append(width if width is not None else np.nan)
        all_tag_widths = np.array(all_tag_widths)
        tag_timestamps = np.arange(len(all_tag_widths)) / fps
        print(f"  加载完成. {len(all_tag_widths)} frames, 有效检测率: {np.sum(~np.isnan(all_tag_widths))/len(all_tag_widths)*100:.1f}%")
    else:
        print(f"\n警告: tag_detection.pkl 不存在，跳过对齐")
        all_tag_widths = np.array([])
        tag_timestamps = np.array([])

    # 计算对齐参数
    t_offset = 0.0
    aligned_motor_widths = None
    if motor_ts is not None:
        print("\n计算时间对齐...")
        # 填充 nan 值用于对齐计算
        tag_widths_filled = all_tag_widths.copy()
        valid_mask = ~np.isnan(tag_widths_filled)
        if valid_mask.sum() > 30:
            # 线性插值填充
            valid_indices = np.where(valid_mask)[0]
            valid_values = tag_widths_filled[valid_mask]
            tag_widths_filled = np.interp(np.arange(len(tag_widths_filled)), valid_indices, valid_values)

            result = align_motor_to_tag(tag_widths_filled, tag_timestamps, motor_ts, motor_pos, motor_scale_sign, fps)
            if result[0] is not None:
                t_offset, aligned_motor_widths, ratio, offset = result
                print(f"  对齐完成: t_offset={t_offset:.3f}s")
            else:
                print("  对齐失败，使用默认参数")
        else:
            print("  有效检测不足，跳过对齐")

    # 信号历史
    width_history = deque(maxlen=300)           # Tag width (m)
    detection_history = deque(maxlen=300)       # 检测状态
    motor_rad_history = deque(maxlen=300)       # Motor rad
    aligned_rad_history = deque(maxlen=300)     # Time aligned motor rad
    aligned_width_history = deque(maxlen=300)   # Aligned motor width (m)

    frame_idx = 0
    paused = False
    show_undistorted = False
    motor_sign = -1  # 默认取负值

    # motor 时间对齐
    motor_start_ts = motor_ts[0] if motor_ts is not None else 0
    motor_ts_rel = (motor_ts - motor_start_ts) if motor_ts is not None else None

    print("\n按键说明:")
    print("  q - 退出")
    print("  空格 - 暂停/继续")
    print("  n - 下一帧")
    print("  r - 重置")
    print("  u - 切换显示原始/畸变校正图像")
    print("  m - 切换 motor 信号正负")
    print("  左/右方向键 - 微调 t_offset (+/- 0.05s)")
    print("  上/下方向键 - 大幅调整 t_offset (+/- 0.5s)")
    print(f"\nGripper tags: left={left_id}, right={right_id}")

    while True:
        if not paused:
            ret, frame = cap.read()
            if not ret:
                print("\n视频结束")
                paused = True
                continue

            frame_idx += 1

            # 转为 RGB 用于检测
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # 应用 mask（避免镜像区域）
            masked_frame = draw_predefined_mask(rgb_frame, color=(0, 0, 0), mirror=True, gripper=False, finger=False)

            # 检测 ArUco tags
            tag_dict = detect_localize_aruco_tags(
                img=masked_frame,
                aruco_dict=aruco_dict,
                marker_size_map=marker_size_map,
                fisheye_intr_dict=fisheye_intr,
                refine_subpix=True
            )

            # 计算夹爪宽度
            gripper_width = get_gripper_width(tag_dict, left_id, right_id, nominal_z=nominal_z)

            if gripper_width is not None:
                width_history.append(gripper_width)
                detection_history.append(1)
                # Tag 平滑
                alpha = 0.3
                if tag_smooth_state is None:
                    tag_smooth_state = gripper_width
                else:
                    tag_smooth_state = alpha * gripper_width + (1 - alpha) * tag_smooth_state
                width_smooth_history.append(tag_smooth_state)
            else:
                width_history.append(width_history[-1] if width_history else 0)
                detection_history.append(0)
                width_smooth_history.append(tag_smooth_state if tag_smooth_state is not None else 0)

            # 预计算的 tag width
            if frame_idx <= len(all_tag_widths):
                precomputed = all_tag_widths[frame_idx - 1]
                if not np.isnan(precomputed):
                    precomputed_width_history.append(precomputed)
                else:
                    precomputed_width_history.append(precomputed_width_history[-1] if precomputed_width_history else 0)
            else:
                precomputed_width_history.append(0)

            # 获取当前帧对应的 motor 数据
            if motor_ts is not None:
                video_time = frame_idx / fps

                # 1. Motor rad（原始位置）
                idx = bisect.bisect_left(motor_ts_rel, video_time)
                idx = min(idx, len(motor_pos) - 1)
                raw_motor = motor_sign * motor_pos[idx]
                motor_rad_history.append(raw_motor)

                # 2. Motor rad（移动平均）
                motor_smooth_history.append(raw_motor)
                window = 30  # 移动平均窗口
                if len(motor_smooth_history) >= window:
                    avg = sum(list(motor_smooth_history)[-window:]) / window
                    motor_smooth_history[-1] = avg

                # 3. Time aligned motor rad（时间对齐后的位置）
                aligned_time = video_time - t_offset
                aligned_idx = bisect.bisect_left(motor_ts_rel, aligned_time)
                aligned_idx = min(max(0, aligned_idx), len(motor_pos) - 1)
                aligned_rad_history.append(motor_sign * motor_pos[aligned_idx])

                # 4. Aligned motor width（对齐后的宽度）
                if aligned_motor_widths is not None:
                    aligned_width_history.append(aligned_motor_widths[aligned_idx])
                else:
                    aligned_width_history.append(0)
            else:
                motor_rad_history.append(0)
                motor_smooth_history.append(0)
                aligned_rad_history.append(0)
                aligned_width_history.append(0)

        # ========== 可视化 ==========
        if show_undistorted:
            # 畸变校正后的图像
            vis_frame = cv2.remap(frame, map1, map2, interpolation=cv2.INTER_LINEAR)
        else:
            vis_frame = frame.copy()

        # 绘制检测到的 tags
        for tag_id, tag_data in tag_dict.items():
            corners = tag_data['corners'].astype(np.int32)
            tvec = tag_data['tvec']

            # 根据 tag 类型选择颜色
            if tag_id in [0, 1, 6, 7]:  # 夹爪手指 tags
                color = (0, 255, 0)  # 绿色
            elif tag_id in [2, 3, 4, 5, 8, 9, 10, 11]:  # 其他夹爪 tags
                color = (255, 255, 0)  # 青色
            else:  # 桌面 tags
                color = (0, 255, 255)  # 黄色

            # 绘制四边形
            cv2.polylines(vis_frame, [corners], True, color, 3)

            # 绘制 ID 和深度
            center = corners.mean(axis=0).astype(int)
            cv2.putText(vis_frame, f"ID:{tag_id}", (center[0]-20, center[1]-10),
                       cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
            cv2.putText(vis_frame, f"z:{tvec[2]:.3f}m", (center[0]-30, center[1]+30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        # 缩放显示
        vis_small = cv2.resize(vis_frame, (display_w, display_h))

        # 信息面板
        info_text = f"Frame: {frame_idx}/{total_frames} | Tags: {len(tag_dict)}"
        if gripper_width is not None:
            info_text += f" | Width: {gripper_width*1000:.1f}mm"
        cv2.putText(vis_small, info_text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

        mode_text = "Undistorted" if show_undistorted else "Original (fisheye)"
        cv2.putText(vis_small, mode_text, (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

        # ========== 4 个信号面板 ==========
        panel_h = 50

        def draw_signal_panel(history, label, color, panel_h=50):
            panel = np.zeros((panel_h, display_w, 3), dtype=np.uint8)
            if len(history) > 1:
                arr = np.array(history)
                a_min, a_max = arr.min(), arr.max()
                if a_max > a_min:
                    normalized = (arr - a_min) / (a_max - a_min)
                else:
                    normalized = arr * 0
                for i in range(1, len(normalized)):
                    x1 = int((i - 1) * display_w / 300)
                    x2 = int(i * display_w / 300)
                    y1 = int(panel_h - 5 - normalized[i-1] * (panel_h - 15))
                    y2 = int(panel_h - 5 - normalized[i] * (panel_h - 15))
                    cv2.line(panel, (x1, y1), (x2, y2), color, 2)
            cv2.putText(panel, label, (5, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            return panel

        # 1. Tag Width (绿色)
        det_rate = sum(detection_history) / len(detection_history) * 100 if len(detection_history) > 0 else 0
        tag_label = f"Tag Width | Det:{det_rate:.1f}%"
        if len(width_history) > 0:
            tag_label += f" | {width_history[-1]*1000:.1f}mm"
        panel1 = draw_signal_panel(width_history, tag_label, (0, 255, 0), panel_h)

        # 2. Motor Rad (橙色)
        sign_str = "-" if motor_sign == -1 else "+"
        motor_label = f"Motor Rad ({sign_str})"
        if len(motor_rad_history) > 0:
            motor_label += f" | {motor_rad_history[-1]:.4f} rad"
        panel2 = draw_signal_panel(motor_rad_history, motor_label, (0, 165, 255), panel_h)

        # 3. Time Aligned Motor Rad (青色)
        aligned_rad_label = f"Aligned Rad (t_offset={t_offset:.3f}s)"
        if len(aligned_rad_history) > 0:
            aligned_rad_label += f" | {aligned_rad_history[-1]:.4f} rad"
        panel3 = draw_signal_panel(aligned_rad_history, aligned_rad_label, (255, 255, 0), panel_h)

        # 4. Aligned Motor Width (紫色)
        aligned_width_label = "Aligned Width"
        if len(aligned_width_history) > 0:
            aligned_width_label += f" | {aligned_width_history[-1]*1000:.1f}mm"
        panel4 = draw_signal_panel(aligned_width_history, aligned_width_label, (255, 0, 255), panel_h)

        # 组合显示
        display = np.vstack([vis_small, panel1, panel2, panel3, panel4])
        cv2.imshow("ArUco + Motor Signal", display)

        # 按键处理
        wait_time = 1 if not paused else 0
        key = cv2.waitKey(wait_time) & 0xFF

        if key == ord('q'):
            break
        elif key == ord(' '):
            paused = not paused
        elif key == ord('n'):
            paused = False
        elif key == ord('r'):
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            frame_idx = 0
            width_history.clear()
            width_smooth_history.clear()
            precomputed_width_history.clear()
            detection_history.clear()
            motor_rad_history.clear()
            motor_smooth_history.clear()
            aligned_rad_history.clear()
            aligned_width_history.clear()
            motor_smooth_state = None
            tag_smooth_state = None
            paused = False
        elif key == ord('u'):
            show_undistorted = not show_undistorted
            print(f"Show undistorted: {show_undistorted}")
        elif key == ord('m'):
            motor_sign *= -1
            motor_rad_history.clear()
            motor_smooth_history.clear()
            aligned_rad_history.clear()
            motor_smooth_state = None
            print(f"Motor sign: {'+' if motor_sign == 1 else '-'}")
        elif key == 81 or key == 2:  # 左方向键
            t_offset -= 0.05
            aligned_rad_history.clear()
            aligned_width_history.clear()
            print(f"t_offset: {t_offset:.3f}s")
        elif key == 83 or key == 3:  # 右方向键
            t_offset += 0.05
            aligned_rad_history.clear()
            aligned_width_history.clear()
            print(f"t_offset: {t_offset:.3f}s")
        elif key == 82 or key == 0:  # 上方向键
            t_offset += 0.5
            aligned_rad_history.clear()
            aligned_width_history.clear()
            print(f"t_offset: {t_offset:.3f}s")
        elif key == 84 or key == 1:  # 下方向键
            t_offset -= 0.5
            aligned_rad_history.clear()
            aligned_width_history.clear()
            print(f"t_offset: {t_offset:.3f}s")

    cap.release()
    cv2.destroyAllWindows()

    # 最终统计
    if len(detection_history) > 0:
        det_rate = sum(detection_history) / len(detection_history) * 100
        print(f"\nFinal detection rate: {det_rate:.1f}%")

    print("Done.")


if __name__ == "__main__":
    main()
