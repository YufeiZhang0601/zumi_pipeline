#!/usr/bin/env python3
"""Debug script to compare correlation calculation between interactive and 06 script."""

import sys
import os
import json
import pickle
import numpy as np
from pathlib import Path

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)
os.chdir(ROOT_DIR)

from scipy import signal
from scipy.signal import medfilt, savgol_filter
from umi.common.cv_util import get_gripper_width
from umi.common.interpolation_util import get_interp1d, get_gripper_calibration_interpolator
from umi.common.motor_alignment import preprocess_tag_signal, cross_correlate_diff_signals

# === Config ===
DEMO_DIR = Path("/nvmessd/yinzi/run_20260108T192932Z/demos/demo_ep004_gp00")
# Adjust if different:
# DEMO_DIR = Path("/home/yinzi/zumi_pipeline/data/run_20260108T192932Z/demos/demo_ep003_gp00")

fps = 60.0
left_id = 0
right_id = 1
nominal_z_interactive = 0.034
nominal_z_06 = 0.03

# === Load data ===
pkl_path = DEMO_DIR / "tag_detection.pkl"
motor_path = DEMO_DIR / "motor_data.jsonl"
gripper_range_path = DEMO_DIR.parent / "gripper_calibration_gp00" / "gripper_range.json"

print(f"Demo dir: {DEMO_DIR}")
print(f"pkl exists: {pkl_path.exists()}")
print(f"motor exists: {motor_path.exists()}")
print(f"gripper_range exists: {gripper_range_path.exists()}")

if not pkl_path.exists():
    print("ERROR: tag_detection.pkl not found!")
    sys.exit(1)

# Load tag detection
with open(pkl_path, 'rb') as f:
    tag_detection_results = pickle.load(f)
print(f"Loaded {len(tag_detection_results)} tag detection results")

# Load motor data
motor_ts_list = []
motor_pos_list = []
with open(motor_path, 'r') as f:
    for line in f:
        data = json.loads(line)
        motor_ts_list.append(data['ts'])
        motor_pos_list.append(data['pos'][0])
motor_ts = np.array(motor_ts_list)
motor_pos = np.array(motor_pos_list)
print(f"Loaded {len(motor_ts)} motor samples")

# Load gripper range
with open(gripper_range_path, 'r') as f:
    gripper_config = json.load(f)
min_width = gripper_config['min_width']
max_width = gripper_config['max_width']
motor_scale_sign = gripper_config.get('motor_scale_sign', -1)
print(f"min_width={min_width}, max_width={max_width}, motor_scale_sign={motor_scale_sign}")

# ============================================================
# Method 1: Interactive script approach
# ============================================================
print("\n" + "="*60)
print("Method 1: Interactive script approach")
print("="*60)

# Get raw tag widths with NaN for missing
all_tag_widths_interactive = []
for td in tag_detection_results:
    tag_dict = td.get('tag_dict', td) if isinstance(td, dict) else td
    width = get_gripper_width(tag_dict, left_id, right_id, nominal_z=nominal_z_interactive)
    all_tag_widths_interactive.append(width if width is not None else np.nan)
all_tag_widths_interactive = np.array(all_tag_widths_interactive)

valid_count = np.sum(~np.isnan(all_tag_widths_interactive))
print(f"Tag detection rate: {valid_count}/{len(all_tag_widths_interactive)} ({valid_count/len(all_tag_widths_interactive)*100:.1f}%)")

# Fill NaN with interpolation
tag_widths_filled = all_tag_widths_interactive.copy()
valid_mask = ~np.isnan(tag_widths_filled)
valid_indices = np.where(valid_mask)[0]
valid_values = tag_widths_filled[valid_mask]
tag_widths_filled = np.interp(np.arange(len(tag_widths_filled)), valid_indices, valid_values)

# Median + Savgol filter
tag_widths_filled = medfilt(tag_widths_filled, kernel_size=31)
tag_widths_filled = savgol_filter(tag_widths_filled, window_length=15, polyorder=2)

# Resample motor
tag_timestamps = np.arange(len(all_tag_widths_interactive)) / fps
motor_ts_rel = motor_ts - motor_ts[0]
motor_resampled = np.interp(tag_timestamps, motor_ts_rel, motor_pos)
motor_for_corr = motor_resampled * (1.0 if motor_scale_sign >= 0 else -1.0)

# Cross-correlation (interactive method)
tag_diff = np.diff(tag_widths_filled)
motor_diff = np.diff(motor_for_corr)

def normalize_z_score(arr):
    arr_std = np.std(arr)
    if arr_std < 1e-9:
        return np.zeros_like(arr)
    return (arr - np.mean(arr)) / arr_std

tag_diff_norm = normalize_z_score(tag_diff)
motor_diff_norm = normalize_z_score(motor_diff)

correlation = signal.correlate(tag_diff_norm, motor_diff_norm, mode='full')
n = len(tag_diff_norm)
lags = np.arange(-(n-1), n)

max_lag_sec = 2.0
max_lag_frames = int(max_lag_sec * fps)
center = n - 1
search_start = max(0, center - max_lag_frames)
search_end = min(len(correlation), center + max_lag_frames)

search_corr = correlation[search_start:search_end]
best_local_idx = np.argmax(search_corr)
best_lag_idx = search_start + best_local_idx
best_lag = lags[best_lag_idx]

t_offset = best_lag / fps
max_corr = correlation[best_lag_idx] / n

print(f"Interactive result: max_corr={max_corr:.4f}, t_offset={t_offset:.3f}s, lag={best_lag}")
print(f"tag_diff_norm std: {np.std(tag_diff_norm):.6f}")
print(f"motor_diff_norm std: {np.std(motor_diff_norm):.6f}")

# ============================================================
# Method 2: 06 script approach
# ============================================================
print("\n" + "="*60)
print("Method 2: 06_generate_dataset_plan.py approach")
print("="*60)

# Build gripper_cal_interp (as in 06 script line 226-231)
gripper_cal_data = {
    'aruco_measured_width': [min_width, max_width],
    'aruco_actual_width': [min_width, max_width]
}
gripper_cal_interp = get_gripper_calibration_interpolator(**gripper_cal_data)

# Get tag widths (as in 06 script line 691-699)
gripper_timestamps = []
gripper_widths = []
for i, td in enumerate(tag_detection_results):
    width = get_gripper_width(td['tag_dict'], left_id=left_id, right_id=right_id, nominal_z=nominal_z_06)
    if width is not None:
        gripper_timestamps.append(i / fps)
        gripper_widths.append(gripper_cal_interp(width) - min_width)

print(f"Tag detection rate (06 method): {len(gripper_widths)}/{len(tag_detection_results)} ({len(gripper_widths)/len(tag_detection_results)*100:.1f}%)")

gripper_interp = get_interp1d(gripper_timestamps, gripper_widths)

# Get tag_widths_full (as in 06 script line 722-723)
full_video_timestamps = np.arange(len(tag_detection_results), dtype=float) / fps
tag_widths_full = gripper_interp(full_video_timestamps)

# Preprocess (as in 06 script line 726)
tag_widths_smooth = preprocess_tag_signal(tag_widths_full)

# Resample motor (as in 06 script line 730-732)
motor_pos_resampled = np.interp(full_video_timestamps, motor_ts_rel, motor_pos)
motor_pos_for_corr = motor_pos_resampled * (1.0 if motor_scale_sign >= 0 else -1.0)

# Cross-correlation (as in 06 script line 735-738)
best_lag_06, t_offset_06, max_corr_06 = cross_correlate_diff_signals(
    tag_widths_smooth, motor_pos_for_corr, fps
)

print(f"06 script result: max_corr={max_corr_06:.4f}, t_offset={t_offset_06:.3f}s, lag={best_lag_06}")

# ============================================================
# Debug: Compare intermediate values
# ============================================================
print("\n" + "="*60)
print("Debug: Comparing intermediate values")
print("="*60)

print(f"Tag widths range (interactive): [{np.min(tag_widths_filled):.6f}, {np.max(tag_widths_filled):.6f}]")
print(f"Tag widths range (06 script):   [{np.min(tag_widths_smooth):.6f}, {np.max(tag_widths_smooth):.6f}]")
print(f"Motor range (both):             [{np.min(motor_for_corr):.6f}, {np.max(motor_for_corr):.6f}]")

# Check diff signals
tag_diff_06 = np.diff(tag_widths_smooth)
motor_diff_06 = np.diff(motor_pos_for_corr)
tag_diff_norm_06 = normalize_z_score(tag_diff_06)
motor_diff_norm_06 = normalize_z_score(motor_diff_06)

print(f"\nTag diff std (interactive): {np.std(tag_diff):.6e}")
print(f"Tag diff std (06 script):   {np.std(tag_diff_06):.6e}")
print(f"Motor diff std (both):      {np.std(motor_diff):.6e}")

print(f"\nTag diff norm std (interactive): {np.std(tag_diff_norm):.6f}")
print(f"Tag diff norm std (06 script):   {np.std(tag_diff_norm_06):.6f}")

# Check if tag_diff is essentially flat (no variation)
if np.std(tag_diff_06) < 1e-9:
    print("\n*** WARNING: tag_diff_06 is essentially FLAT (no variation)! ***")
    print("This would cause correlation to fail!")
