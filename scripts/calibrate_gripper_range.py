# %%
import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT_DIR)
os.chdir(ROOT_DIR)

# %%
import click
import collections
import pickle
import json
import numpy as np
from pathlib import Path
from exiftool import ExifToolHelper
from umi.common.cv_util import get_gripper_width


def load_motor_jsonl(path):
    """Load motor data from JSONL format."""
    ts_list = []
    pos_list = []
    with open(path, 'r') as f:
        for line in f:
            if line.strip():
                data = json.loads(line)
                ts_list.append(data['ts'])
                pos = data['pos']
                pos_list.append(pos[0] if isinstance(pos, list) else pos)
    return np.array(ts_list), np.array(pos_list)
# %%
@click.command()
@click.option('-i', '--input', required=True, help='Tag detection pkl')
@click.option('-o', '--output', required=True, help='output json')
@click.option('-g', '--gripper_id', type=int, default=None, help='Gripper hardware ID (if not provided, infer from tags)')
@click.option('-t', '--tag_det_threshold', type=float, default=0.3)
@click.option('-nz', '--nominal_z', type=float, default=0.034, help="nominal Z value for gripper finger tag")
def main(input, output, gripper_id, tag_det_threshold, nominal_z):
    tag_detection_results = pickle.load(open(input, 'rb'))
    tag_per_gripper = 6

    # 如果提供了 gripper_id，直接使用；否则从 tag detection 推断
    if gripper_id is None:
        # 原有的 tag detection 推断逻辑
        n_frames = len(tag_detection_results)
        tag_counts = collections.defaultdict(lambda: 0)
        for frame in tag_detection_results:
            for key in frame['tag_dict'].keys():
                tag_counts[key] += 1
        tag_stats = collections.defaultdict(lambda: 0.0)
        for k, v in tag_counts.items():
            tag_stats[k] = v / n_frames

        max_tag_id = np.max(list(tag_stats.keys()))
        max_gripper_id = max_tag_id // tag_per_gripper

        gripper_prob_map = dict()
        for gid in range(max_gripper_id+1):
            left_id = gid * tag_per_gripper
            right_id = left_id + 1
            left_prob = tag_stats[left_id]
            right_prob = tag_stats[right_id]
            gripper_prob = min(left_prob, right_prob)
            if gripper_prob <= 0:
                continue
            gripper_prob_map[gid] = gripper_prob
        if len(gripper_prob_map) == 0:
            print("No grippers detected!")
            exit(1)

        gripper_probs = sorted(gripper_prob_map.items(), key=lambda x:x[1])
        gripper_id = gripper_probs[-1][0]
        gripper_prob = gripper_probs[-1][1]
        print(f"Detected gripper id: {gripper_id} with probability {gripper_prob}")
        if gripper_prob < tag_det_threshold:
            print(f"Detection rate {gripper_prob} < {tag_det_threshold} threshold.")
            exit(1)
    else:
        print(f"Using provided gripper_id: {gripper_id}")
        
    # run calibration
    left_id = gripper_id * tag_per_gripper
    right_id = left_id + 1

    gripper_widths = list()
    for i, dt in enumerate(tag_detection_results):
        tag_dict = dt['tag_dict']
        width = get_gripper_width(tag_dict, left_id, right_id, nominal_z=nominal_z)
        if width is None:
            width = float('Nan')
        gripper_widths.append(width)
    gripper_widths = np.array(gripper_widths)
    max_width = np.nanmax(gripper_widths)
    min_width = np.nanmin(gripper_widths)

    # ========== 计算 motor_scale_sign ==========
    # 加载 motor 数据
    motor_path = Path(input).parent / 'motor_data.jsonl'
    if not motor_path.exists():
        raise FileNotFoundError(f"标定数据不完整，缺少 {motor_path}")

    motor_ts, motor_pos = load_motor_jsonl(motor_path)
    motor_ts = motor_ts - motor_ts[0]  # 相对时间

    # 获取视频 fps
    video_path = Path(input).parent / 'raw_video.mp4'
    with ExifToolHelper() as et:
        metadata = et.get_metadata(str(video_path))[0]
        fps = float(metadata.get('QuickTime:VideoFrameRate', metadata.get('Track1:VideoFrameRate')))

    # 对齐时间轴
    tag_ts = np.arange(len(gripper_widths)) / fps

    # 找 tag 的 min 和 max 时刻（排除 nan）
    valid_mask = ~np.isnan(gripper_widths)
    valid_indices = np.where(valid_mask)[0]
    valid_widths = gripper_widths[valid_mask]

    if len(valid_widths) < 10:
        raise ValueError(f"有效的 tag 检测数据不足 ({len(valid_widths)} 帧)，请重新标定")

    # 找闭合和打开时刻
    close_idx = valid_indices[np.argmin(valid_widths)]
    open_idx = valid_indices[np.argmax(valid_widths)]

    t_close = tag_ts[close_idx]
    t_open = tag_ts[open_idx]

    # 采样 motor 位置
    motor_at_close = np.interp(t_close, motor_ts, motor_pos)
    motor_at_open = np.interp(t_open, motor_ts, motor_pos)

    # 计算 scale 符号
    # tag_width 打开时增大，motor 打开时可能增大或减小
    motor_span = motor_at_open - motor_at_close

    # 如果 motor 变化太小，说明标定过程有问题
    if abs(motor_span) < 0.01:  # 至少 0.01 rad 的变化
        raise ValueError(f"标定数据异常：motor 变化范围太小 ({motor_span:.4f} rad)，请重新标定")

    motor_scale_sign = 1 if motor_span > 0 else -1
    print(f"计算得到 motor_scale_sign = {motor_scale_sign} (motor_span = {motor_span:.4f} rad)")

    result = {
        'gripper_id': gripper_id,
        'left_finger_tag_id': left_id,
        'right_finger_tag_id': right_id,
        'max_width': max_width,
        'min_width': min_width,
        'motor_scale_sign': motor_scale_sign
    }
    json.dump(result, open(output, 'w'), indent=2)

# %%
if __name__ == "__main__":
    main()
