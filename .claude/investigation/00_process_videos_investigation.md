# Investigation: 00_process_videos.py

## File Overview

**Path:** `scripts_slam_pipeline/00_process_videos.py`
**Lines:** 199
**Purpose:** Organize raw video and data files into demos/ structure for downstream processing

## File Structure

```
main() function:
  Line 22-198: Main processing logic
    - Line 26-34: Setup directories (input_dir, output_dir, motor_datas_dir, gripper_cal_dir)
    - Line 36-42: Create raw_videos if doesn't exist, move MP4s
    - Line 44-64: Create name maps for IMU, motor_data, motor_meta_data
    - Line 66-88: Create mapping.mp4 (largest video)
    - Line 89-132: Create gripper_calibration
    - Line 134-191: Main loop - process all MP4s into demos
```

## Current File Discovery Logic

### Data Files Lookup (Lines 44-64)
```python
for mp4_path in list(input_dir.glob('**/*.MP4')) + list(input_dir.glob('**/*.mp4')):
    name_without_ext = mp4_path.with_suffix('').name
    imu_json_path = session.joinpath(name_without_ext + "_imu.json")
    motor_data_path = session.joinpath(name_without_ext + "_motor.npz")  # <-- OLD FORMAT
    motor_meta_data_path = session.joinpath(name_without_ext + "_motor_meta.json")
```

### Output File Structure
- `motor_data.npz` (Line 84, 125-127, 168-169, 180-181)
- `imu_data.json`
- `raw_video.mp4`

## Key Modification Points

### 1. Motor Data Format (Lines 51, 58-60, 82-84, 123-127, 167-169, 178-181)
**Current:** `*_motor.npz`
**New:** `*_motor.jsonl`

### 2. UVC File Discovery (Need to add)
**Pattern:** `{run_id}_ep{N}_uvc.MP4` and `{run_id}_ep{N}_uvc.jsonl`
**Action:** Add new dictionaries for UVC tracking

### 3. Output Structure Changes
**Current output per demo:**
```
demo_xxx/
  raw_video.mp4
  imu_data.json
  motor_data.npz
```

**New output per demo:**
```
demo_xxx/
  raw_video.mp4
  imu_data.json
  motor_data.jsonl    # Changed format
  uvc_video.mp4       # NEW (optional)
  uvc_data.jsonl      # NEW (optional)
```

## Suggested Modifications

### Phase 1: Add UVC file discovery
- Line ~45: Add `mp4_name_to_uvc_video_path = dict()`
- Line ~46: Add `mp4_name_to_uvc_data_path = dict()`
- In the loop (48-64): Add discovery for `*_uvc.MP4` and `*_uvc.jsonl`

### Phase 2: Change motor format
- Line 51: Change `_motor.npz` to `_motor.jsonl`
- Line 58-60: Change output name to `_motor.jsonl`
- Similar changes at lines 82-84, 123-127, 167-169, 178-181

### Phase 3: Copy UVC files to demo folders
- After moving motor files (around line 185), add UVC file handling:
  - Copy `uvc_video.mp4` if exists
  - Copy `uvc_data.jsonl` if exists
  - Warn if UVC video exists but timestamps don't, or vice versa
