# Implementation Plan: 00_process_videos.py

## Summary
Adapt the video processing script to:
1. Handle new data format with JSONL motor files and UVC camera files
2. **Use new directory naming convention: `demo_ep{N}_gp{XX}/` instead of `demo_{SN}_{timestamp}/`**
3. **Rename gripper_calibration to `gripper_calibration_gp{XX}/`**

## 文件命名规则

### 输入文件格式
| 类型 | 文件名格式 | 示例 |
|------|-----------|------|
| GoPro | `{run_id}_ep{N}_gp{XX}_{gopro_id}.MP4` | `run_20260107T161428Z_ep001_gp00_GX011810.MP4` |
| IMU | `{run_id}_ep{N}_gp{XX}_{gopro_id}_imu.json` | `run_20260107T161428Z_ep001_gp00_GX011810_imu.json` |
| Motor | `{run_id}_ep{N}_gp{XX}_motor.jsonl` | `run_20260107T161428Z_ep001_gp00_motor.jsonl` |
| UVC | `{run_id}_ep{N}_gp{XX}_uvc.MP4` | `run_20260107T161428Z_ep001_gp00_uvc.MP4` |
| UVC ts | `{run_id}_ep{N}_gp{XX}_uvc.jsonl` | `run_20260107T161428Z_ep001_gp00_uvc.jsonl` |

### 输出目录结构
```
demos/
├── mapping/                           # mapping 视频（最长的那个）
│   ├── raw_video.mp4
│   ├── imu_data.json
│   ├── motor_data.jsonl
│   ├── uvc_video.mp4
│   └── uvc_data.jsonl
├── gripper_calibration_gp00/          # gripper 0 标定（每个 gripper 第一个视频）
│   ├── raw_video.mp4
│   ├── imu_data.json
│   ├── motor_data.jsonl
│   ├── uvc_video.mp4
│   └── uvc_data.jsonl
├── gripper_calibration_gp01/          # gripper 1 标定
│   └── ...
├── demo_ep001_gp00/                   # episode 1, gripper 0
│   ├── raw_video.mp4
│   ├── imu_data.json
│   ├── motor_data.jsonl
│   ├── uvc_video.mp4
│   └── uvc_data.jsonl
├── demo_ep001_gp01/                   # episode 1, gripper 1
│   └── ...
├── demo_ep002_gp00/                   # episode 2, gripper 0
│   └── ...
└── ...
```

**关键设计**：
- 目录名直接包含 `ep{N}` 和 `gp{XX}`，06 阶段可直接解析
- 不再依赖 GoPro SN 或时间戳推断 episode/gripper
- `gp00` = gripper 0, `gp01` = gripper 1, 依次类推

## Changes Overview

### Change 1: Add helper functions (After imports)
```python
import re

def parse_episode_gripper(filename):
    """Extract episode number and gripper id from filename.

    Example: 'run_20260107T161428Z_ep001_gp00_GX011810' -> ('ep001', 'gp00')
    Returns: (episode_str, gripper_str) or (None, None) if not matched
    """
    match = re.search(r'(ep\d+)_(gp\d+)', filename)
    if match:
        return match.group(1), match.group(2)
    return None, None


def validate_filename_format(filename, context=""):
    """Validate filename contains required ep{N}_gp{XX} pattern.

    Raises ValueError with detailed message if validation fails.
    """
    episode_str, gripper_str = parse_episode_gripper(filename)
    if episode_str is None or gripper_str is None:
        # 尝试找到部分匹配，给出更有用的提示
        has_ep = re.search(r'ep\d+', filename) is not None
        has_gp = re.search(r'gp\d+', filename) is not None

        if has_ep and not has_gp:
            hint = "Found 'ep{N}' but missing 'gp{XX}'"
        elif has_gp and not has_ep:
            hint = "Found 'gp{XX}' but missing 'ep{N}'"
        elif '_ep' in filename.lower() or '_gp' in filename.lower():
            hint = "Pattern found but format incorrect (need ep{N}_gp{XX})"
        else:
            hint = "No episode/gripper pattern found"

        raise ValueError(
            f"Invalid filename format{context}:\n"
            f"  Actual:   '{filename}'\n"
            f"  Expected: '{{run_id}}_ep{{N}}_gp{{XX}}_{{gopro_id}}' format\n"
            f"  Example:  'run_20260107T161428Z_ep001_gp00_GX011810'\n"
            f"  Issue:    {hint}"
        )
    return episode_str, gripper_str


def get_gripper_prefix(gopro_filename):
    """Extract {run_id}_ep{N}_gp{XX} prefix from GoPro filename.

    Example: 'run_20260107T161428Z_ep001_gp00_GX011810' -> 'run_20260107T161428Z_ep001_gp00'
    """
    match = re.match(r'(.+_ep\d+_gp\d+)', gopro_filename)
    if match:
        return match.group(1)
    return None
```

### Change 2: Add new dictionaries (Line ~47)
```python
gripper_prefix_to_motor_data_path = dict()
gripper_prefix_to_uvc_video_path = dict()
gripper_prefix_to_uvc_data_path = dict()
# 新增：记录每个 gripper 的第一个视频（用于 calibration）
gripper_id_first_video = dict()  # {'gp00': (start_date, mp4_path), ...}
```

### Change 3: Update file discovery loop (Lines 48-64)
```python
for mp4_path in list(input_dir.glob('**/*.MP4')) + list(input_dir.glob('**/*.mp4')):
    name_without_ext = mp4_path.with_suffix('').name

    # 跳过 mapping 文件
    if name_without_ext.startswith('mapping'):
        continue

    # 解析文件名格式
    episode_str, gripper_str = parse_episode_gripper(name_without_ext)
    if episode_str is None or gripper_str is None:
        print(f"Warning: Skipping '{mp4_path.name}' - filename does not match expected format")
        print(f"  Expected: '{{run_id}}_ep{{N}}_gp{{XX}}_{{gopro_id}}.MP4'")
        continue

    gripper_prefix = get_gripper_prefix(name_without_ext)

    # IMU 使用完整文件名
    imu_json_path = session.joinpath(name_without_ext + "_imu.json")
    if imu_json_path.exists():
        out_json_path = input_dir.joinpath(name_without_ext + "_imu.json")
        shutil.move(imu_json_path, out_json_path)
        mp4_name_to_imu_json_name[name_without_ext] = out_json_path

    # Motor 和 UVC 使用 gripper 前缀（避免重复处理）
    if gripper_prefix and gripper_prefix not in gripper_prefix_to_motor_data_path:
        # Motor data (JSONL format)
        motor_data_path = session.joinpath(gripper_prefix + "_motor.jsonl")
        if motor_data_path.exists():
            out_motor_data_path = input_dir.joinpath(gripper_prefix + "_motor.jsonl")
            shutil.move(motor_data_path, out_motor_data_path)
            gripper_prefix_to_motor_data_path[gripper_prefix] = out_motor_data_path

        # UVC files
        uvc_video_path = session.joinpath(gripper_prefix + "_uvc.MP4")
        uvc_data_path = session.joinpath(gripper_prefix + "_uvc.jsonl")
        if uvc_video_path.exists() and uvc_data_path.exists():
            out_uvc_video_path = input_dir.joinpath(gripper_prefix + "_uvc.MP4")
            out_uvc_data_path = input_dir.joinpath(gripper_prefix + "_uvc.jsonl")
            shutil.move(uvc_video_path, out_uvc_video_path)
            shutil.move(uvc_data_path, out_uvc_data_path)
            gripper_prefix_to_uvc_video_path[gripper_prefix] = out_uvc_video_path
            gripper_prefix_to_uvc_data_path[gripper_prefix] = out_uvc_data_path
        elif uvc_video_path.exists() or uvc_data_path.exists():
            print(f"Warning: UVC files incomplete for {gripper_prefix}. Need both video and timestamps.")

    # 记录每个 gripper 的第一个视频（按时间）
    if gripper_str:
        start_date = mp4_get_start_datetime(str(mp4_path))
        if gripper_str not in gripper_id_first_video or start_date < gripper_id_first_video[gripper_str][0]:
            gripper_id_first_video[gripper_str] = (start_date, mp4_path)
```

### Change 4: Update mapping video handling (Lines 66-88)
保持不变，mapping 视频还是选最长的那个。

### Change 5: Update gripper_calibration handling (Lines 89-132)
**改动：目录名从 `gripper_calibration` 改为 `gripper_calibration_gp{XX}`**

```python
# create gripper calibration video if don't exist (one per gripper)
for gripper_str, (start_date, mp4_path) in gripper_id_first_video.items():
    gripper_cal_dir = output_dir.joinpath(f'gripper_calibration_{gripper_str}')

    if gripper_cal_dir.is_dir():
        print(f"{gripper_cal_dir.name} already exists, skipping")
        continue

    gripper_cal_dir.mkdir(parents=True, exist_ok=True)
    print(f"Creating {gripper_cal_dir.name} with {mp4_path.name}")

    # Move video
    out_path = gripper_cal_dir.joinpath('raw_video.mp4')
    shutil.move(mp4_path, out_path)

    # Move IMU
    imu_path = mp4_name_to_imu_json_name.get(mp4_path.with_suffix('').name, None)
    if imu_path is not None:
        shutil.move(imu_path, gripper_cal_dir.joinpath("imu_data.json"))

    # Move motor data (use gripper prefix)
    gripper_prefix = get_gripper_prefix(mp4_path.with_suffix('').name)
    if gripper_prefix:
        motor_data_path = gripper_prefix_to_motor_data_path.get(gripper_prefix, None)
        if motor_data_path is not None:
            shutil.move(motor_data_path, gripper_cal_dir.joinpath("motor_data.jsonl"))

        # Move UVC files
        uvc_video_path = gripper_prefix_to_uvc_video_path.get(gripper_prefix, None)
        if uvc_video_path is not None:
            shutil.move(uvc_video_path, gripper_cal_dir.joinpath("uvc_video.mp4"))
            uvc_data_path = gripper_prefix_to_uvc_data_path.get(gripper_prefix, None)
            if uvc_data_path is not None:
                shutil.move(uvc_data_path, gripper_cal_dir.joinpath("uvc_data.jsonl"))
```

### Change 6: Update demo directory naming (Line 147)
**核心改动：目录名从 `demo_{SN}_{timestamp}` 改为 `demo_ep{N}_gp{XX}`**

```python
# 原来的代码：
# out_dname = 'demo_' + cam_serial + '_' + start_date.strftime(r"%Y.%m.%d_%H.%M.%S.%f")

# 新代码：使用 validate_filename_format 提供详细错误信息
name_without_ext = mp4_path.with_suffix('').name
episode_str, gripper_str = validate_filename_format(
    name_without_ext,
    context=f" in file '{mp4_path.name}'"
)
out_dname = f'demo_{episode_str}_{gripper_str}'
```

### Change 7: Update demo handling - move associated files (Lines 173-185)
```python
# special folders
if mp4_path.name.startswith('mapping'):
    out_dname = "mapping"
    # ... existing mapping handling ...
else:
    # Move IMU
    imu_path = mp4_name_to_imu_json_name.get(mp4_path.with_suffix('').name, None)
    if imu_path is not None:
        shutil.move(imu_path, this_out_dir.joinpath("imu_data.json"))

    # Move motor and UVC using gripper prefix
    gripper_prefix = get_gripper_prefix(mp4_path.with_suffix('').name)
    if gripper_prefix:
        motor_data_path = gripper_prefix_to_motor_data_path.get(gripper_prefix, None)
        if motor_data_path is not None:
            shutil.move(motor_data_path, this_out_dir.joinpath("motor_data.jsonl"))

        uvc_video_path = gripper_prefix_to_uvc_video_path.get(gripper_prefix, None)
        if uvc_video_path is not None:
            shutil.move(uvc_video_path, this_out_dir.joinpath("uvc_video.mp4"))
            uvc_data_path = gripper_prefix_to_uvc_data_path.get(gripper_prefix, None)
            if uvc_data_path is not None:
                shutil.move(uvc_data_path, this_out_dir.joinpath("uvc_data.jsonl"))
```

## Testing
1. 目录名正确使用 `demo_ep{N}_gp{XX}` 格式
2. `gripper_calibration_gp{XX}` 目录正确创建（每个 gripper 一个）
3. Motor JSONL 文件正确匹配（使用 gripper prefix）
4. UVC 文件正确匹配和移动
5. IMU 文件仍使用完整文件名匹配
6. **文件名格式错误时显示详细提示**：
   - 测试缺少 `ep{N}` 的文件名
   - 测试缺少 `gp{XX}` 的文件名
   - 测试完全不符合格式的文件名
   - 验证错误信息包含：实际文件名、期望格式、示例、具体问题
