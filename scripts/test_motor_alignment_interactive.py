# %%
import sys
import os
import json
import yaml
import pickle
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from dotenv import load_dotenv
from collections import deque
import bisect
from scipy import signal


# Add project root directory to PATH
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)
os.chdir(ROOT_DIR)

from umi.common.cv_util import get_gripper_width

# Load environment variables
env_path = Path(ROOT_DIR) / 'scripts_slam_pipeline' / '.env'
load_dotenv(env_path)

# SESSION_DIR = os.getenv('SESSION_DIR')
SESSION_DIR = "/home/yinzi/zumi_pipeline/data/run_20260108T192932Z"
print(f"ROOT_DIR: {ROOT_DIR}")
print(f"SESSION_DIR: {SESSION_DIR}")

# %% [markdown]
# ## 1. Set Path and Parameters

# %%
# By default, try to use the first demo under SESSION_DIR
if SESSION_DIR:
    demo_dir = Path(SESSION_DIR) / 'demos'
    # Find an existing demo directory
    found_demo = None
    if demo_dir.exists():
        for d in sorted(demo_dir.glob('demo_ep*_gp*')):
            if (d / 'raw_video.mp4').exists():
                found_demo = d
                break
    
    if found_demo:
        video_path = found_demo / 'raw_video.mp4'
    else:
        video_path = "path/to/your/video.mp4"
else:
    video_path = "path/to/your/video.mp4"


#%%
found_demo = Path("/home/yinzi/zumi_pipeline/data/run_20260108T192932Z/demos/gripper_calibration_gp00")
video_path = found_demo / 'raw_video.mp4'
#%%
print(f"Target video: {video_path}")
video_dir = Path(video_path).parent

#%%
# Parameters
fps = 60.0
left_id = 0
right_id = 1
nominal_z = 0.034

# %% [markdown]
# ## 2. Load Data

# %%
# --- Load Motor Data ---
motor_path = video_dir / "motor_data.jsonl"
motor_timestamps = []
motor_positions = []

if motor_path.exists():
    with open(motor_path, 'r') as f:
        for line in f:
            data = json.loads(line)
            motor_timestamps.append(data['ts'])
            motor_positions.append(data['pos'][0])  # Take the first position value
    motor_ts = np.array(motor_timestamps)
    motor_pos = np.array(motor_positions)
    print(f"Motor data loaded: {len(motor_ts)} samples, duration: {motor_ts[-1] - motor_ts[0]:.2f}s")
else:
    motor_ts = np.array([])
    motor_pos = np.array([])
    print(f"Motor data not found: {motor_path}")

# --- Load Gripper Configuration (Motor Scale Sign) ---
gripper_range_path = video_dir.parent / "gripper00" / "gripper_range.json"
motor_scale_sign = -1
if not gripper_range_path.exists():
    for p in video_dir.parent.glob("gripper*/gripper_range.json"):
        gripper_range_path = p
        break
        
if gripper_range_path.exists():
    with open(gripper_range_path, 'r') as f:
        gripper_config = json.load(f)
        motor_scale_sign = gripper_config.get('motor_scale_sign', -1)
        print(f"Loaded motor_scale_sign={motor_scale_sign} from {gripper_range_path.name}")
else:
    print(f"Gripper config not found, using default motor_scale_sign={motor_scale_sign}")

# --- Load Tag Data ---
pkl_path = video_dir / "tag_detection.pkl"
all_tag_widths = []
tag_timestamps = []

if pkl_path.exists():
    print(f"Loading tag data from {pkl_path}...")
    with open(pkl_path, 'rb') as f:
        tag_detection_results = pickle.load(f)
        
    for td in tag_detection_results:
        tag_dict = td.get('tag_dict', td) if isinstance(td, dict) else td
        width = get_gripper_width(tag_dict, left_id, right_id, nominal_z=nominal_z)
        all_tag_widths.append(width if width is not None else np.nan)
        
    all_tag_widths = np.array(all_tag_widths)
    tag_timestamps = np.arange(len(all_tag_widths)) / fps
    
    valid_count = np.sum(~np.isnan(all_tag_widths))
    print(f"Loaded {len(all_tag_widths)} frames. Valid detection: {valid_count} ({valid_count/len(all_tag_widths)*100:.1f}%)")
else:
    print(f"Tag detection pkl not found: {pkl_path}")
    all_tag_widths = None

# %% [markdown]
# ## 3. Preprocess and Prepare Signals
# %%
# Fill NaNs in Tag data for alignment calculation
tag_widths_filled = all_tag_widths.copy()
valid_mask = ~np.isnan(tag_widths_filled)
if valid_mask.sum() > 30:
    valid_indices = np.where(valid_mask)[0]
    valid_values = tag_widths_filled[valid_mask]
    tag_widths_filled = np.interp(np.arange(len(tag_widths_filled)), valid_indices, valid_values)
    
    # Apply median filter to remove spikes
    from scipy.signal import medfilt, savgol_filter
    # Reduce median filter kernel size to avoid "step" artifacts, just remove outliers
    tag_widths_filled = medfilt(tag_widths_filled, kernel_size=31)
    
    # Apply Savitzky-Golay filter to smooth out high-frequency noise in flat regions
    # window_length=15 (approx 0.25s), polyorder=2 is more stable for flat regions
    wl = 15
    if len(tag_widths_filled) > wl:
        tag_widths_filled = savgol_filter(tag_widths_filled, window_length=wl, polyorder=2)
else:
    print("Warning: Not enough valid tag detections for interpolation")
# plot
plt.figure(figsize=(10, 4))
plt.plot(tag_widths_filled, label='tag_widths_filled')
plt.legend()
plt.show()
#%%
# Resample motor to tag timestamps
motor_ts_rel = motor_ts - motor_ts[0]
motor_resampled = np.interp(tag_timestamps, motor_ts_rel, motor_pos)
# %%
# Apply polarity correction
motor_for_corr = motor_resampled * (1.0 if motor_scale_sign >= 0 else -1.0)
# plot
plt.figure(figsize=(10, 4))
plt.plot(motor_for_corr, label='motor_pos_resampled (rad), corrected sign')
plt.legend()
plt.show()
#%%
# Calculate difference (derivative)
tag_diff = np.diff(tag_widths_filled)
motor_diff = np.diff(motor_for_corr)
#%%
def normalize_z_score(arr):
    arr_std = np.std(arr)
    if arr_std < 1e-9:
        return np.zeros_like(arr)
    return (arr - np.mean(arr)) / arr_std
#%%
tag_diff_normalized = normalize_z_score(tag_diff)
motor_diff_normalized = normalize_z_score(motor_diff)

#%%
# plot
plt.figure(figsize=(10, 4))
plt.title("Before Time Alignment")
plt.plot(tag_diff_normalized, label='tag_diff_normalized')
plt.plot(motor_diff_normalized, label='motor_diff_normalized')
plt.legend()
plt.show()
#%%
# %% [markdown]
# ## 4. Cross-Correlation Time Alignment
#%%
correlation = signal.correlate(tag_diff_normalized, motor_diff_normalized, mode='full')

# %%
t_offset = 0.0
max_corr = 0.0
lags = np.array([])
correlation = np.array([])
best_lag = 0

if 'tag_diff_normalized' in locals() and 'motor_diff_normalized' in locals():
    # Cross-correlation
    correlation = signal.correlate(tag_diff_normalized, motor_diff_normalized, mode='full')
    n = len(tag_diff_normalized)
    lags = np.arange(-(n-1), n)

    # Limit search range (+/- 2 seconds)
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

    print(f"Cross-correlation result: max_corr={max_corr:.3f}, t_offset={t_offset:.3f}s, lag_frames={best_lag}")
    
    # Plot Correlation
    plt.figure(figsize=(10, 4))
    lag_sec = lags / fps
    mask = (lag_sec >= -2.0) & (lag_sec <= 2.0)
    plt.plot(lag_sec[mask], correlation[mask])
    plt.axvline(x=t_offset, color='r', linestyle='--', label=f'Best: {t_offset:.3f}s')
    plt.xlabel('Lag (s)')
    plt.ylabel('Correlation')
    plt.title('Cross-correlation')
    plt.legend()
    plt.grid(True)
    plt.show()

#%% Plot after time alignment

def shift_signal(arr, lag):
    result = np.zeros_like(arr)
    if lag > 0:
        result[lag:] = arr[:-lag]
    elif lag < 0:
        result[:lag] = arr[-lag:]
    else:
        result[:] = arr
    return result

# Aligned motor signals
motor_diff_aligned = shift_signal(motor_diff_normalized, best_lag)
motor_pos_aligned = shift_signal(motor_for_corr, best_lag)

plt.figure(figsize=(10, 8))

# 1. Differential signal comparison (Normalized)
plt.subplot(2, 1, 1)
plt.title(f"Aligned Diff Signals (Shift: {best_lag} frames)")
plt.plot(tag_diff_normalized, label='Tag Width Diff (Norm)')
plt.plot(motor_diff_aligned, label='Motor Pos Diff (Norm, Aligned)')
plt.legend()
plt.grid(True, alpha=0.3)

# 2. Raw signal comparison (Normalized for visualization)
plt.subplot(2, 1, 2)
plt.title("Aligned Position Signals (Normalized)")
plt.plot(normalize_z_score(tag_widths_filled), label='Tag Width (Norm)')
plt.plot(normalize_z_score(motor_pos_aligned), label='Motor Pos (Norm, Aligned)')
plt.legend()
plt.grid(True, alpha=0.3)

plt.tight_layout()
plt.show()

#%%
# %% [markdown]
# ## 5. Linear Mapping

# %%
# Identify Reference Points
# 1. Close Point: Simple argmin on filtered tag data (usually robust for momentary close)
idx_close = np.argmin(tag_widths_filled)
width_close = tag_widths_filled[idx_close]
motor_close = motor_pos_aligned[idx_close]

# 2. Open Point: Find stable region in motor data
# Calculate absolute difference of motor position
motor_abs_diff = np.abs(np.diff(motor_pos_aligned, prepend=motor_pos_aligned[0]))

# Thresholds
diff_threshold = 1e-3  # rad/frame
min_duration = 60      # frames (approx 1s)

# Find stable segments
is_stable = motor_abs_diff < diff_threshold
from scipy.ndimage import label as scipy_label
labeled_array, num_features = scipy_label(is_stable)

valid_segments = []
for i in range(1, num_features + 1):
    indices = np.where(labeled_array == i)[0]
    if len(indices) > min_duration:
        # Calculate average motor position for this segment
        avg_motor_pos = np.mean(motor_pos_aligned[indices])
        valid_segments.append({
            'indices': indices,
            'length': len(indices),
            'avg_pos': avg_motor_pos,
            'start': indices[0],
            'end': indices[-1]
        })

print(f"Found {len(valid_segments)} stable segments > {min_duration} frames.")

if not valid_segments:
    print("Warn: No stable segments found! Falling back to simple argmax. That's okay if this is the gripper_cal stage.")
    idx_open = np.argmax(tag_widths_filled)
    width_open = tag_widths_filled[idx_open]
    motor_open = motor_pos_aligned[idx_open]
    open_segment = None
else:
    # Strategy: Choose the segment with the MAXIMUM average motor position (most open)
    # This avoids selecting a long closed segment.
    best_segment = max(valid_segments, key=lambda x: x['avg_pos'])
    
    indices = best_segment['indices']
    width_open = np.mean(tag_widths_filled[indices])
    motor_open = np.mean(motor_pos_aligned[indices])
    idx_open = int((indices[0] + indices[-1]) / 2) # Midpoint for visualization
    open_segment = best_segment
    
    print(f"Selected Open Segment:")
    print(f"  Range: {indices[0]} - {indices[-1]} (Len: {len(indices)})")
    print(f"  Avg Motor Pos: {best_segment['avg_pos']:.4f}")

print("-" * 30)
print(f"Reference Points:")
print(f"  Close: index={idx_close}, width={width_close:.4f} m, motor={motor_close:.4f} rad")
print(f"  Open:  index={idx_open}, width={width_open:.4f} m, motor={motor_open:.4f} rad")

# Calculate Linear Parameters
# width = ratio * motor + offset
if abs(motor_open - motor_close) > 1e-6:
    ratio = (width_open - width_close) / (motor_open - motor_close)
    offset = width_open - ratio * motor_open
else:
    ratio = 0.0
    offset = 0.0
    print("Warning: Motor positions for open/close are too close to calculate ratio.")

print("-" * 30)
print(f"Calculated Mapping:")
print(f"  Ratio:  {ratio:.6f} m/rad")
print(f"  Offset: {offset:.6f} m")
print("-" * 30)

# Apply Mapping
aligned_motor_widths = ratio * motor_pos_aligned + offset

# Visualization
plt.figure(figsize=(10, 6))
plt.title("Linear Mapping Verification (Stable Region Method)")
plt.plot(tag_widths_filled, label='Tag Width (Filtered)', linewidth=2, alpha=0.7)
plt.plot(aligned_motor_widths, label='Predicted Width from Motor', linestyle='--', linewidth=2, alpha=0.7)

# Highlight Open Stable Region
if open_segment:
    plt.axvspan(open_segment['start'], open_segment['end'], color='green', alpha=0.2, label='Open Stable Region')

# Mark reference points
plt.scatter([idx_close], [width_close], color='red', s=100, zorder=5, label='Ref Point (Close)')
plt.scatter([idx_open], [width_open], color='green', s=100, zorder=5, label='Ref Point (Open - Avg)')

plt.xlabel("Frame Index")
plt.ylabel("Gripper Width (m)")
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.show()

print(f"Final Formula: width = {ratio:.6f} * motor_pos + {offset:.6f}")

# Calculate statistics based on motor data (extrapolated)
min_width_val = np.min(aligned_motor_widths)
max_width_val = np.max(aligned_motor_widths)

print("-" * 30)
print(f"Video Aligned Width Range (Motor Extrapolated):")
print(f"  Min Width: {min_width_val:.6f} m")
print(f"  Max Width: {max_width_val:.6f} m")

# Also calculate on raw motor data to catch peaks missed by resampling or outside video
if len(motor_pos) > 0:
    motor_pos_sign_corrected = motor_pos * (1.0 if motor_scale_sign >= 0 else -1.0)
    raw_motor_widths = ratio * motor_pos_sign_corrected + offset
    print(f"Full Log Width Range (Motor Extrapolated):")
    print(f"  Min Width: {np.min(raw_motor_widths):.6f} m")
    print(f"  Max Width: {np.max(raw_motor_widths):.6f} m")
print("-" * 30)

# %%
