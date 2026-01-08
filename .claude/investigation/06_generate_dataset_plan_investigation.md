# Investigation: 06_generate_dataset_plan.py

## File Overview

**Path:** `scripts_slam_pipeline/06_generate_dataset_plan.py`
**Lines:** 871
**Purpose:** Generate dataset_plan.pkl from processed demos

## File Structure

```
Stage 0 (Line 95-149): Gather inputs, load gripper calibration
Stage 1 (Line 152-214): Extract video metadata, build video_meta_df
Stage 2 (Line 217-288): Match videos into demos
Stage 3 (Line 289-354): Identify gripper ID using aruco
Stage 4 (Line 356-502): Disambiguate gripper left/right
Stage 6 (Line 504-856): Generate dataset plan (main processing)
```

## Motor Data Loading Analysis

### Current Implementation (Lines 677-703)
```python
# Line 677-680
motor_widths = None
motor_ts = None
motor_path = video_dir.joinpath('motor_data.npz')  # <-- OLD FORMAT
if motor_path.is_file():
    # Line 682-688
    motor_data = np.load(motor_path)
    motor_cols = [str(c) for c in motor_data['columns']]  # <-- OLD: columns array
    motor_idx = {c: i for i, c in enumerate(motor_cols)}
    motor_ts = motor_data['data'][:, motor_idx['ts']].astype(float)  # <-- OLD: 2D data array
    motor_ts = motor_ts - motor_ts[0]  # use relative time
    motor_pos = motor_data['data'][:, motor_idx['pos']].astype(float)
```

**Old NPZ Format:**
- `columns`: Array of column names ['ts', 'pos', ...]
- `data`: 2D numpy array with columns matching `columns`

**New JSONL Format:**
- Each line: `{"ts": float, "pos": [float, ...]}`

## Camera List Construction (Lines 834-846)

```python
# all cams
video_dir = row['video_dir']
vid_start_frame = cam_start_frame_idxs[cam_idx]
cameras.append({
    "video_path": str(video_dir.joinpath('raw_video.mp4').relative_to(video_dir.parent)),
    "video_start_end": (start+vid_start_frame, end+vid_start_frame)
})
```

Current cameras list only includes GoPro cameras.

## t_offset Application Analysis

GoPro timestamps are aligned using cross-correlation with motor data (Lines 722-748):
```python
# Cross-correlation alignment
t_offset = best_lag / fps
motor_ts_aligned = motor_ts + t_offset
```

For UVC, we need to apply a fixed t_offset (0.179s from plan) to align with GoPro time.

## Key Modification Points

### 1. Add load_motor_jsonl() function (before Stage 0)
```python
def load_motor_jsonl(path):
    """Load motor data from JSONL format."""
    ts_list = []
    pos_list = []
    with open(path, 'r') as f:
        for line in f:
            data = json.loads(line)
            ts_list.append(data['ts'])
            pos_list.append(data['pos'][0])  # Assuming single motor position
    return np.array(ts_list), np.array(pos_list)
```

### 2. Modify motor loading (Lines 677-703)
- Change file path from `motor_data.npz` to `motor_data.jsonl`
- Use `load_motor_jsonl()` instead of `np.load()`

### 3. Add UVC camera to cameras list (after Line 840)
- Discover UVC video path
- Apply t_offset (0.179s) to UVC timestamps
- Mark UVC camera with `is_uvc: True` flag

### 4. UVC Video Discovery
Add function to find UVC video for each demo:
```python
def get_uvc_video_path(video_dir):
    uvc_video = video_dir.joinpath('uvc_video.mp4')
    uvc_data = video_dir.joinpath('uvc_data.jsonl')
    if uvc_video.is_file() and uvc_data.is_file():
        return uvc_video, uvc_data
    return None, None
```

## Output Structure Changes

**Current camera dict:**
```python
{
    "video_path": str,
    "video_start_end": (int, int)
}
```

**New camera dict (for UVC):**
```python
{
    "video_path": str,
    "video_start_end": (int, int),
    "is_uvc": True,
    "uvc_timestamps_path": str,  # path to uvc_data.jsonl
    "t_offset": float  # 0.179 for UVC
}
```
