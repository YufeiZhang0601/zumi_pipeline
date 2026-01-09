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
from umi.common.motor_alignment import (
    preprocess_tag_signal,
    cross_correlate_diff_signals,
    shift_signal,
    find_reference_points,
    calculate_linear_mapping
)


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


def get_video_fps(video_path: Path) -> float:
    """Get video frame rate from metadata."""
    with ExifToolHelper() as et:
        metadata = et.get_metadata(str(video_path))[0]
        fps = float(metadata.get('QuickTime:VideoFrameRate', metadata.get('Track1:VideoFrameRate')))
    return fps


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

    # If gripper_id is provided, use it directly; otherwise infer from tag detection
    if gripper_id is None:
        # Original tag detection inference logic
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

    # Run calibration
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

    # Load motor data
    motor_path = Path(input).parent / 'motor_data.jsonl'
    if not motor_path.exists():
        raise FileNotFoundError(f"Calibration data incomplete, missing {motor_path}")

    motor_ts, motor_pos = load_motor_jsonl(motor_path)
    motor_ts = motor_ts - motor_ts[0]  # relative time

    # Get video fps
    video_path = Path(input).parent / 'raw_video.mp4'
    if not video_path.exists():
        raise FileNotFoundError(f"Calibration video not found: {video_path}")
    fps = get_video_fps(video_path)
    print(f"Video fps: {fps}")

    # === Step 1: Polarity detection (keep existing logic) ===
    # Based on standard calibration procedure: start closed, then open
    initial_window = min(10, len(motor_pos))
    close_pos_est = np.median(motor_pos[:initial_window])
    max_dist_idx = np.argmax(np.abs(motor_pos - close_pos_est))
    open_pos_est = motor_pos[max_dist_idx]
    motor_span = open_pos_est - close_pos_est
    motor_scale_sign = 1 if motor_span > 0 else -1

    if abs(motor_span) < 0.01:
        raise ValueError(f"Motor span too small ({motor_span:.4f} rad), check calibration data")

    print(f"Polarity: close={close_pos_est:.4f}, open={open_pos_est:.4f}, sign={motor_scale_sign}")

    # === Step 2: Preprocessing and alignment (matches test script) ===
    tag_timestamps = np.arange(len(gripper_widths)) / fps
    tag_widths_filled = preprocess_tag_signal(np.array(gripper_widths))

    # Resample motor to tag timeline, apply polarity correction
    motor_resampled = np.interp(tag_timestamps, motor_ts, motor_pos)
    motor_for_corr = motor_resampled * (1.0 if motor_scale_sign >= 0 else -1.0)

    # Cross-correlation time alignment
    best_lag, t_offset, max_corr = cross_correlate_diff_signals(
        tag_widths_filled, motor_for_corr, fps
    )

    if abs(t_offset) > 1.0:
        print(f"Warning: large t_offset ({t_offset:.2f}s) - may indicate sync issue")
    if abs(t_offset) > 2.0:
        raise ValueError(f"t_offset {t_offset:.2f}s exceeds 2.0s limit")

    motor_pos_aligned = shift_signal(motor_for_corr, best_lag)

    # === Step 3: Reference point detection and linear mapping ===
    close_point, open_point = find_reference_points(tag_widths_filled, motor_pos_aligned)
    ratio, offset = calculate_linear_mapping(close_point, open_point)

    # ratio should be positive (motor already direction-corrected)
    if ratio <= 0:
        raise ValueError(f"Unexpected negative ratio ({ratio:.6f}), check polarity logic")

    # === Step 4: Extrapolate max/min width using full motor log ===
    # Apply polarity correction to full motor_pos
    motor_pos_corrected = motor_pos * (1.0 if motor_scale_sign >= 0 else -1.0)

    # Extrapolate widths
    motor_full_max = np.max(motor_pos_corrected)
    motor_full_min = np.min(motor_pos_corrected)
    extrapolated_max_width = ratio * motor_full_max + offset
    extrapolated_min_width = ratio * motor_full_min + offset

    # Tag detection raw values (for reference)
    tag_max_width = np.nanmax(gripper_widths)
    tag_min_width = np.nanmin(gripper_widths)

    # Safety check: allow 5% tolerance
    tolerance = 0.05 * (extrapolated_max_width - extrapolated_min_width)
    if tag_max_width > extrapolated_max_width + tolerance:
        raise ValueError(f"Tag width ({tag_max_width:.4f}m) > extrapolated limit ({extrapolated_max_width:.4f}m)")
    if tag_min_width < extrapolated_min_width - tolerance:
        print(f"Warning: Tag min ({tag_min_width:.4f}m) < extrapolated min ({extrapolated_min_width:.4f}m)")

    max_width = extrapolated_max_width
    min_width = max(0, extrapolated_min_width)  # width cannot be negative

    print(f"Tag detected: min={tag_min_width:.4f}m, max={tag_max_width:.4f}m")
    print(f"Extrapolated: min={extrapolated_min_width:.4f}m, max={extrapolated_max_width:.4f}m")
    print(f"Final: min={min_width:.4f}m, max={max_width:.4f}m")
    print(f"Linear mapping: width = {ratio:.6f} * motor_corrected + {offset:.6f}")

    # === Step 5: Save results ===
    result = {
        'gripper_id': gripper_id,
        'left_finger_tag_id': left_id,
        'right_finger_tag_id': right_id,
        'max_width': float(max_width),
        'min_width': float(min_width),
        'motor_scale_sign': motor_scale_sign,
        # Debug info (not used by downstream, downstream computes ratio independently)
        'debug_ratio': float(ratio),
        'debug_offset': float(offset),
        'debug_t_offset': float(t_offset),
        'debug_correlation': float(max_corr),
        'tag_max_width': float(tag_max_width),
        'tag_min_width': float(tag_min_width),
    }
    json.dump(result, open(output, 'w'), indent=2)

# %%
if __name__ == "__main__":
    main()
