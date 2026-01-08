# Implementation Plan: 06_generate_dataset_plan.py

## Summary
Major refactoring to:
1. **Parse episode and gripper id directly from directory name** (`demo_ep{N}_gp{XX}/`)
2. **Remove time-overlap based episode grouping** (stage 2 simplification)
3. **Remove tag detection based gripper id inference** (stage 3 simplification)
4. Add support for JSONL motor format and UVC camera integration

## 新目录命名规则

### 输入目录结构 (00 处理后)
```
demos/
├── mapping/
├── gripper_calibration_gp00/
├── gripper_calibration_gp01/
├── demo_ep001_gp00/           # episode 1, gripper 0
│   ├── raw_video.mp4
│   ├── imu_data.json
│   ├── motor_data.jsonl
│   ├── uvc_video.mp4
│   └── uvc_data.jsonl
├── demo_ep001_gp01/           # episode 1, gripper 1
├── demo_ep002_gp00/           # episode 2, gripper 0
└── ...
```

### 关键设计变更
| 原来 | 现在 |
|-----|------|
| 目录名 `demo_{SN}_{timestamp}` | 目录名 `demo_ep{N}_gp{XX}` |
| 用时间重叠推断 episode 分组 | 直接从目录名解析 `ep{N}` |
| 用 tag detection 推断 gripper id | 直接从目录名解析 `gp{XX}` |
| UVC 用 `cam_idx` 索引 | UVC 用 `gripper_id` 索引 |

## 时间对齐关系（重要）

### 时钟来源
| 传感器 | 时钟类型 | 说明 |
|-------|---------|------|
| **GoPro** | 帧号时间 | `t_gopro = frame_index / fps`，相对时间 |
| **Motor** | 系统时钟 | Unix timestamp（绝对时间） |
| **UVC** | 系统时钟 | Unix timestamp，与 Motor 天然对齐 |
| **其他传感器** | 系统时钟 | 与 Motor/UVC 天然对齐 |

### 时间转换链条
```
GoPro 帧号 i
    ↓ (÷ fps)
GoPro 相对时间 t_gopro = i / fps
    ↓ (cross-correlation 得到 t_offset)
Motor 相对时间 = t_gopro - t_offset
    ↓ (+ motor_ts_origin)
系统绝对时间 = motor_ts_origin + t_gopro - t_offset
    ↓
UVC 绝对时间戳 → 找到对应 UVC 帧
```

### t_offset 公式严格推导

**现有代码逻辑（Line 746）**：
```python
motor_ts_aligned = motor_ts + t_offset
motor_interp = get_interp1d(motor_ts_aligned, motor_widths)
this_motor_widths = motor_interp(video_timestamps)
```

**推导**：
1. `motor_ts_aligned = motor_ts + t_offset` 用于查询 `video_timestamps`（GoPro 相对时间）
2. 这意味着：`motor_ts + t_offset ≈ gopro_relative_time`
3. 变换：`motor_ts = gopro_relative_time - t_offset`
4. 从 GoPro 时间转系统绝对时间：
   ```
   system_time = motor_ts_origin + motor_ts
               = motor_ts_origin + (gopro_relative_time - t_offset)
               = motor_ts_origin + gopro_relative_time - t_offset
   ```

**最终公式**（所有涉及时间转换的地方必须使用此公式）：
```python
system_time = motor_ts_origin + video_timestamps - t_offset
```

### 公式验证

**场景 A：Motor 先开始 0.5 秒**
- Motor 系统时间 1000.0 开始 → `motor_ts_origin = 1000.0`
- GoPro 系统时间 1000.5 开始
- 物理事件发生在系统时间 1001.0
  - GoPro 记录在 `gopro_rel = 0.5`
  - Motor 记录在 `motor_rel = 1.0`
- Cross-correlation：`motor_rel + t_offset = gopro_rel` → `1.0 + t_offset = 0.5` → `t_offset = -0.5`
- 验证：`system_time = 1000.0 + 0.5 - (-0.5) = 1001.0` ✓

**场景 B：GoPro 先开始 0.5 秒**
- GoPro 系统时间 1000.0 开始
- Motor 系统时间 1000.5 开始 → `motor_ts_origin = 1000.5`
- 物理事件发生在系统时间 1001.0
  - GoPro 记录在 `gopro_rel = 1.0`
  - Motor 记录在 `motor_rel = 0.5`
- Cross-correlation：`0.5 + t_offset = 1.0` → `t_offset = 0.5`
- 验证：`system_time = 1000.5 + 1.0 - 0.5 = 1001.0` ✓

### t_offset 计算（已有逻辑，Lines 721-764）
每个 (episode, gripper) 组合需要独立计算 `t_offset`：

1. **视觉线索**：从 GoPro 视频中检测夹爪 ArUco tag，得到夹爪宽度随帧变化的信号
2. **电机信号**：从 motor.jsonl 读取电机位置信号（系统时钟时间戳）
3. **Cross-correlation**：将两个信号做互相关，找到最佳对齐的 lag
4. **得到 t_offset**：`t_offset = best_lag / fps`

**已有代码（需要小改动，保存 motor_ts_origin）**：
```python
# Lines 677-703: Motor loading
motor_ts_origin = None  # 新增：保存原始系统时间起点
motor_path = video_dir.joinpath('motor_data.jsonl')
if motor_path.is_file():
    motor_ts_raw, motor_pos = load_motor_jsonl(motor_path)
    motor_ts_origin = motor_ts_raw[0]  # 必须在减法前保存！
    motor_ts = motor_ts_raw - motor_ts_origin  # 转成相对时间，用于 cross-correlation

# Lines 721-764: Cross-correlation alignment（保持不变）
if motor_widths is not None:
    fps = float(row['fps'])
    full_video_timestamps = np.arange(len(full_tag_detection_results), dtype=float) / fps
    tag_widths_full = gripper_interp(full_video_timestamps)
    tag_widths_smooth = sn.gaussian_filter1d(tag_widths_full, sigma=2)

    # Resample motor to uniform grid (tag timestamps)
    motor_resampled = np.interp(full_video_timestamps, motor_ts, motor_widths)

    # Normalize for correlation
    tag_signal = tag_widths_smooth - np.mean(tag_widths_smooth)
    motor_signal = motor_resampled - np.mean(motor_resampled)

    # Cross-correlation to find best alignment
    correlation = np.correlate(tag_signal, motor_signal, mode='full')
    n = len(tag_signal)
    lags = np.arange(-(n-1), n)
    best_lag_idx = np.argmax(correlation)
    best_lag = lags[best_lag_idx]

    # Convert lag to time offset
    t_offset = best_lag / fps
```

### UVC 帧对齐（新增逻辑）
UVC 使用系统时钟，与 Motor 天然对齐。需要用 `motor_ts_origin` 和 `t_offset` 把 GoPro 相对时间转换为系统绝对时间：

```python
# video_timestamps 是 GoPro 帧的相对时间：frame_index / fps
# 例如：video_timestamps = np.arange(start_frame_idx, start_frame_idx + n_frames) / fps

# 转换到系统绝对时间（使用验证过的公式）
system_timestamps = motor_ts_origin + video_timestamps - t_offset

# 在 UVC 时间戳（系统绝对时间）中查找对应帧
uvc_frame_indices = np.searchsorted(uvc_ts, system_timestamps)
uvc_frame_indices = np.clip(uvc_frame_indices, 0, len(uvc_ts) - 1)
```

**为什么需要 motor_ts_origin**：
- `motor_ts = motor_ts - motor_ts[0]` 把 motor 时间转成相对时间，方便 cross-correlation
- 但 UVC 时间戳是绝对系统时间，需要 `motor_ts_origin` 才能转换回去
- 如果不保存 `motor_ts_origin`，就无法正确对齐 UVC

## Changes Overview

### Change 1: Add helper functions (After imports, ~Line 38)
```python
import re

def parse_demo_dir_name(dir_name):
    """Parse episode and gripper id from demo directory name.
    
    Strictly matches 'demo_ep{N}_gp{XX}'.
    Directories with suffixes (e.g. _backup) will be ignored.

    Args:
        dir_name: Directory name like 'demo_ep001_gp00'

    Returns:
        Tuple of (episode_num: int, gripper_id: int) or (None, None) if not matched
    """
    # Use anchor $ to ensure exact match (no suffixes allowed)
    match = re.match(r'demo_ep(\d+)_gp(\d+)$', dir_name)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None

def parse_gripper_cal_dir_name(dir_name):
    """Parse gripper id from gripper calibration directory name.

    Args:
        dir_name: Directory name like 'gripper_calibration_gp00'

    Returns:
        gripper_id: int or None if not matched
    """
    match = re.search(r'gp(\d+)', dir_name)
    if match:
        return int(match.group(1))
    return None

def load_motor_jsonl(path):
    """Load motor data from JSONL format.

    Args:
        path: Path to motor.jsonl file

    Returns:
        Tuple of (timestamps, positions) as numpy arrays
    """
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

def get_uvc_info(video_dir):
    """Get UVC video and timestamp info for a demo directory."""
    uvc_video_path = video_dir.joinpath('uvc_video.mp4')
    uvc_data_path = video_dir.joinpath('uvc_data.jsonl')

    if not uvc_video_path.is_file() or not uvc_data_path.is_file():
        return None

    uvc_timestamps = []
    with open(uvc_data_path, 'r') as f:
        for line in f:
            if line.strip():
                data = json.loads(line)
                uvc_timestamps.append(data['ts'])

    with av.open(str(uvc_video_path), 'r') as container:
        stream = container.streams.video[0]
        n_frames = stream.frames
        fps = float(stream.average_rate)

    return {
        'video_path': uvc_video_path,
        'data_path': uvc_data_path,
        'timestamps': np.array(uvc_timestamps),
        'n_frames': n_frames,
        'fps': fps
    }
```

### Change 2: Update gripper_calibration loading (Lines 127-149)
**从目录名解析 gripper_id，而非从 JSON 文件读取：**

```python
# 原来的代码：
# gripper_range_data = json.load(gripper_cal_path.open('r'))
# gripper_id = gripper_range_data['gripper_id']  # 从 JSON 读取

# 新代码：从目录名解析
for gripper_cal_path in demos_dir.glob("gripper*/gripper_range.json"):
    mp4_path = gripper_cal_path.parent.joinpath('raw_video.mp4')
    meta = list(et.get_metadata(str(mp4_path)))[0]
    cam_serial = meta['QuickTime:CameraSerialNumber']

    # 从目录名解析 gripper_id（而非从 JSON）
    gripper_id = parse_gripper_cal_dir_name(gripper_cal_path.parent.name)
    assert gripper_id is not None, f"Cannot parse gripper_id from {gripper_cal_path.parent.name}"

    gripper_range_data = json.load(gripper_cal_path.open('r'))
    max_width = gripper_range_data['max_width']
    min_width = gripper_range_data['min_width']

    # ... 后续逻辑不变 ...
```

**注意**：配合 `05_run_calibrations_plan.md` 的改动，JSON 文件中的 `gripper_id` 也会是正确的（来自 05 传入的参数）。
但 06 仍从目录名解析，保持 single source of truth。

### Change 3: Simplify Stage 1 - Add episode/gripper parsing (Lines 157-214)
在 video metadata 提取时，直接从目录名解析 episode 和 gripper id：

```python
# 原来只提取 video metadata
rows.append({
    'video_dir': video_dir,
    'camera_serial': cam_serial,
    ...
})

# 新增：解析目录名获取 episode 和 gripper id
episode_num, gripper_id = parse_demo_dir_name(video_dir.name)
rows.append({
    'video_dir': video_dir,
    'camera_serial': cam_serial,
    'episode_num': episode_num,      # 新增
    'gripper_id': gripper_id,        # 新增
    ...
})
```

### Change 4: Replace Stage 2 - Episode grouping (Lines 230-288)
**删除时间重叠推断，改为直接按 episode_num 分组：**

```python
# 原来的代码（删除）：
# events = list()
# for vid_idx, row in video_meta_df.iterrows():
#     events.append({'t': row['start_timestamp'], 'is_start': True})
#     ...
# 通过时间重叠推断 episode

# 新代码：直接按 episode_num 分组
demo_data_list = list()
episode_groups = video_meta_df.groupby('episode_num')

for episode_num, episode_df in episode_groups:
    if episode_num is None:
        # 跳过无法解析的目录（如 mapping）
        continue

    video_idxs = list(episode_df.index)

    # 计算时间范围（取所有相机的交集）
    t_start = episode_df['start_timestamp'].max()
    t_end = episode_df['end_timestamp'].min()

    if t_end <= t_start:
        print(f"Warning: episode {episode_num} has no valid time overlap")
        continue

    demo_data_list.append({
        "video_idxs": sorted(video_idxs),
        "start_timestamp": t_start,
        "end_timestamp": t_end,
        "episode_num": episode_num  # 新增：保留 episode 信息
    })

# 按 episode_num 排序
demo_data_list = sorted(demo_data_list, key=lambda x: x['episode_num'])
```

### Change 5: Simplify Stage 3 - Gripper ID (Lines 289-354)
**删除 tag detection 推断，直接使用目录名中的 gripper_id：**

```python
# 原来的代码（删除）：
# for vid_idx, row in video_meta_df.iterrows():
#     pkl_path = video_dir.joinpath('tag_detection.pkl')
#     tag_data = pickle.load(...)
#     # 通过 tag 统计推断 gripper_id
#     gripper_id_by_tag = ...

# 新代码：直接使用已解析的 gripper_id
# gripper_id 已经在 Stage 1 从目录名解析并存入 video_meta_df['gripper_id']

# 构建 cam_serial -> gripper_id 映射（用于兼容性）
cam_serial_gripper_hardware_id_map = dict()
for vid_idx, row in video_meta_df.iterrows():
    if row['gripper_id'] is not None:
        cam_serial_gripper_hardware_id_map[row['camera_serial']] = row['gripper_id']

# 添加 gripper_hardware_id 列（与原接口兼容）
video_meta_df['gripper_hardware_id'] = video_meta_df['gripper_id']
```

**可选：保留 tag detection 作为校验**
```python
# 如果需要校验，可以保留 tag detection 但只用于 warning
for vid_idx, row in video_meta_df.iterrows():
    if row['gripper_id'] is None:
        continue
    pkl_path = row['video_dir'].joinpath('tag_detection.pkl')
    if pkl_path.is_file():
        gripper_id_by_tag = infer_gripper_by_tag(pkl_path)  # 原有逻辑
        if gripper_id_by_tag != row['gripper_id']:
            print(f"Warning: {row['video_dir'].name} tag detection ({gripper_id_by_tag}) "
                  f"differs from directory name ({row['gripper_id']})")
```

### Change 6: Fix gripper_hardware_id check for None (Lines 402, 658)
**原来的代码检查 `< 0`，但新逻辑中 mapping 目录的 gripper_id 是 `None`，需要改为检查 `is None`：**

**位置 1: Line 402**
```python
# 原来的代码：
# if row.gripper_hardware_id < 0:  # 会导致 TypeError: '<' not supported between 'NoneType' and 'int'

# 新代码：
if row.gripper_hardware_id is None:
    # not gripper camera (e.g., mapping)
    cam_serial = row['camera_serial']
    if cam_serial in cam_serial_cam_idx_map:
        # ... 原有逻辑 ...
```

**位置 2: Line 658**
```python
# 原来的代码：
# ghi = row['gripper_hardware_id']
# if ghi < 0:

# 新代码：
ghi = row['gripper_hardware_id']
if ghi is None:
    print(f"Skipping {video_dir.name}, invalid gripper hardware id {ghi}")
    dropped_camera_count[row['camera_serial']] += 1
    continue
```

**注意**：所有检查 `gripper_hardware_id < 0` 或 `gripper_id < 0` 的地方都需要改为 `is None`。

### Change 7: Update UVC info storage - use gripper_id as key (Lines 580, 779)
**改用 dict 以 gripper_id 为 key，而非 list 以 cam_idx 为索引：**

**注意**：`all_uvc_info` 是 **per-episode** 的，在每个 episode 循环开始时初始化。
`t_offset` 是通过 GoPro-Motor cross-correlation **动态计算**的，每个 (episode, gripper) 组合有自己的值。

```python
# 外层循环：每个 episode
for demo_idx, demo_data in enumerate(demo_data_list):
    all_uvc_info = dict()  # per-episode，每个 episode 重新初始化

    # 内层循环：该 episode 内的每个 gripper camera
    for cam_idx, row in demo_video_meta_df.iterrows():
        gripper_id = row['gripper_id']

        # ... 计算 t_offset（cross-correlation，第 744 行）...
        # t_offset 是 per-episode-per-gripper 的

        # 存储该 gripper 的 UVC 信息
        all_uvc_info[gripper_id] = {
            'uvc_info': uvc_info,
            'uvc_frame_indices': uvc_frame_indices,  # 对齐后的 UVC 帧索引数组
        }

    # 使用时（仍在同一 episode 内）
    for cam_idx, row in demo_video_meta_df.iterrows():
        gripper_id = row['gripper_id']
        if gripper_id in all_uvc_info:
            uvc_data = all_uvc_info[gripper_id]
```

### Change 7.1: UVC 帧率校验（新增，在 motor 对齐完成后）
**位置**：在 gripper camera 循环内，加载 UVC 信息后

**重要**：UVC 帧率必须与 GoPro 一致，否则 ReplayBuffer 帧数会不匹配。

```python
# 加载 UVC 信息
uvc_info = get_uvc_info(video_dir)

# UVC 帧率校验（必须与 GoPro 一致）
if uvc_info is not None:
    gopro_fps = float(row['fps'])
    uvc_fps = uvc_info['fps']

    # 允许 0.5% 的误差（如 59.94 vs 60.0）
    fps_tolerance = 0.005
    fps_diff = abs(uvc_fps - gopro_fps) / gopro_fps

    if fps_diff > fps_tolerance:
        raise ValueError(
            f"UVC fps mismatch in {video_dir.name}: "
            f"UVC={uvc_fps:.2f}fps, GoPro={gopro_fps:.2f}fps. "
            f"UVC camera must have same fps as GoPro for ReplayBuffer compatibility."
        )
```

### Change 7.2: UVC 帧对齐计算（新增，在 t_offset 计算完成后）
**位置**：在 motor 对齐（cross-correlation）完成后，约 Line 768 之后

```python
    # 加载 UVC 信息并计算对齐帧（在 motor 对齐计算完成后）
    uvc_info = None
    uvc_frame_indices = None
    
    # 尝试加载 UVC 信息
    uvc_info = get_uvc_info(video_dir)
    
    # 如果存在 UVC 数据但 motor_ts_origin 缺失，无法对齐，抛出异常
    if uvc_info is not None and motor_ts_origin is None:
        raise ValueError(f"Found UVC data in {video_dir.name} but missing motor data. Cannot align UVC without motor timestamps.")
        
    if motor_ts_origin is not None and uvc_info is not None:
        # 计算 GoPro 帧对应的 UVC 帧索引
        # video_timestamps 是当前 episode 片段的 GoPro 相对时间
        # 使用验证过的公式：system_time = motor_ts_origin + video_timestamps - t_offset
        system_timestamps = motor_ts_origin + video_timestamps - t_offset

        uvc_ts = uvc_info['timestamps']  # UVC 帧的系统绝对时间

        # 边界检查
        out_of_range_before = np.sum(system_timestamps < uvc_ts[0])
        out_of_range_after = np.sum(system_timestamps > uvc_ts[-1])
        if out_of_range_before > 0 or out_of_range_after > 0:
            print(f"Warning: {video_dir.name} UVC time range issue: "
                  f"{out_of_range_before} frames before, {out_of_range_after} frames after UVC range")

        # 使用 searchsorted 找到最近的 UVC 帧
        uvc_frame_indices = np.searchsorted(uvc_ts, system_timestamps)
        uvc_frame_indices = np.clip(uvc_frame_indices, 0, len(uvc_ts) - 1)

    # 存储到 all_uvc_info
    if uvc_info is not None and uvc_frame_indices is not None:
        all_uvc_info[gripper_id] = {
            'uvc_info': uvc_info,
            'uvc_frame_indices': uvc_frame_indices,
        }
```

### Change 8: Update motor loading to JSONL format (Lines 677-703)
**改动**：
1. 文件格式从 `.npz` 改为 `.jsonl`
2. **新增 `motor_ts_origin`**：保存原始系统时间起点，用于后续 UVC 对齐（见"时间对齐关系"章节）

```python
motor_widths = None
motor_ts = None
motor_ts_origin = None  # 新增：保存原始系统时间起点
motor_path = video_dir.joinpath('motor_data.jsonl')
if motor_path.is_file():
    motor_ts, motor_pos = load_motor_jsonl(motor_path)
    motor_ts_origin = motor_ts[0]  # 新增：保存绝对时间起点，用于 UVC 对齐
    motor_ts = motor_ts - motor_ts_origin  # 转成相对时间，用于 cross-correlation
```

**注意**：`motor_ts_origin` 必须在减法之前保存，否则无法将 GoPro 相对时间转换回系统绝对时间来对齐 UVC。

### Change 9: Update camera building with UVC (Lines 834-840)
```python
# all GoPro cams
video_dir = row['video_dir']
vid_start_frame = cam_start_frame_idxs[cam_idx]
cameras.append({
    "video_path": str(video_dir.joinpath('raw_video.mp4').relative_to(video_dir.parent)),
    "video_start_end": (start+vid_start_frame, end+vid_start_frame),
    "is_uvc": False
})

# Add UVC camera for this gripper (if available)
gripper_id = row['gripper_id']
if gripper_id is not None and gripper_id in all_uvc_info:
    uvc_data = all_uvc_info[gripper_id]
    uvc_info = uvc_data['uvc_info']
    uvc_frame_indices = uvc_data['uvc_frame_indices']

    # 获取当前 episode 片段对应的 UVC 帧范围
    # start, end 是当前片段在完整 video_timestamps 中的索引
    uvc_start_frame = int(uvc_frame_indices[start])
    uvc_end_frame = int(uvc_frame_indices[end - 1]) + 1  # +1 因为 video_start_end 是 [start, end) 左闭右开

    # 验证帧数一致性（关键！）
    gopro_n_frames = (end + vid_start_frame) - (start + vid_start_frame)  # = end - start
    uvc_n_frames = uvc_end_frame - uvc_start_frame
    if uvc_n_frames != gopro_n_frames:
        print(f"Warning: {video_dir.name} frame count mismatch: GoPro={gopro_n_frames}, UVC={uvc_n_frames}")
        # 由于 fps 已校验一致，这里的不匹配通常是边界 clip 导致的
        # 强制调整 UVC 帧数以匹配 GoPro（可能导致重复帧，但保证 ReplayBuffer 不崩溃）
        uvc_end_frame = uvc_start_frame + gopro_n_frames

    cameras.append({
        "video_path": str(uvc_info['video_path'].relative_to(video_dir.parent)),
        "video_start_end": (uvc_start_frame, uvc_end_frame),
        "is_uvc": True,
        "gripper_idx": gripper_id
    })
```

### Change 10: Enforce strict camera ordering and consistency (New)
**为了满足 ReplayBuffer 对相机数量固定和顺序确定的严格要求（07 计划依赖项）：**

1.  **明确排序规则**：
    - 外层：按 `camera_idx` (0=Right, 1=Left) 排序。
    - 内层：对于每个位置，先添加 GoPro，后添加 UVC (如果存在)。
    - 最终顺序：`[GoPro_Right, UVC_Right, GoPro_Left, UVC_Left]` (假设双臂且都有 UVC)。

2.  **强制一致性**：
    - 记录首个 episode 的 `n_cameras`。
    - 后续所有 episodes 必须具有相同的相机数量。
    - 如果某个 episode 缺少 UVC 但其他有，或者反之，抛出 `ValueError`。

```python
# 初始化全局变量（在循环外）
global_n_cameras = None

# 在处理每个 episode 时 (Stage 5 loop)
for demo_idx, demo_data in enumerate(demo_data_list):
    # ... (Stage 5 setup) ...

    # 1. 明确按 camera_idx 排序，确保 [Right, Left] 顺序
    demo_video_meta_df = demo_video_meta_df.sort_values('camera_idx')

    cameras = list()
    for _, row in demo_video_meta_df.iterrows():
        # Append GoPro (Change 9 logic)
        # ...

        # Append UVC (Change 9 logic)
        # ...

    # 2. 验证一致性
    if global_n_cameras is None:
        global_n_cameras = len(cameras)
        print(f"Locked camera configuration: {global_n_cameras} cameras found in first episode.")
    elif len(cameras) != global_n_cameras:
        raise ValueError(
            f"Inconsistent camera count in episode {demo_data['episode_num']}. "
            f"Expected {global_n_cameras}, got {len(cameras)}. "
            f"All episodes must have the same set of cameras (check UVC presence)."
        )

    # ...
```

## 保留的逻辑

### Stage 4: 左右判断（保留不变）
`gripper_hardware_id`（从目录名）只是硬件区分，不表示左右位置。

Stage 4 通过相机位置的 x 投影自动判断左右顺序：
- `camera_idx = 0` → 右边的 gripper
- `camera_idx = 1` → 左边的 gripper

**数据流**：
```
目录名 gp{XX} → gripper_hardware_id（硬件区分，用于匹配 motor/uvc）
                    ↓
Stage 4 位置推断 → camera_idx（0=右，1=左，用于 grippers 列表排序）
                    ↓
grippers 列表按 camera_idx 排序
                    ↓
07: robot0=右边 gripper，robot1=左边 gripper
```

**好处**：采集时不需要约定哪个 gripper 在哪边，系统自动判断。

## Summary of Deleted Code

| 位置 | 删除内容 | 原因 |
|-----|---------|------|
| Stage 2 (Lines 230-288) | 时间重叠推断 episode | 改为直接从目录名解析 |
| Stage 3 (Lines 289-354) | Tag detection 推断 gripper id | 改为直接从目录名解析 |

## Testing
1. 目录名 `demo_ep001_gp00` 正确解析为 episode=1, gripper=0
2. `gripper_calibration_gp00` 正确解析为 gripper_id=0
3. Episode 分组正确（同一 episode_num 的视频分到一组）
4. Gripper id 正确获取（从目录名而非 tag detection）
5. mapping 目录（`gripper_id is None`）正确处理，不报 TypeError
6. UVC 使用 gripper_id 正确匹配
7. Motor JSONL 文件正确加载
8. 兼容性：mapping 和 gripper_calibration 目录正常处理
9. 输出 pickle 格式与原来兼容
10. **时间对齐测试**：
    - 验证 `motor_ts_origin` 在减法前正确保存
    - 验证公式 `system_time = motor_ts_origin + video_timestamps - t_offset` 正确性
    - Motor 先开始和 GoPro 先开始两种场景都能正确对齐
11. **UVC 帧率校验**：
    - UVC fps 与 GoPro fps 差异 > 0.5% 时报错
    - 误差 < 0.5% 时正常通过
12. **UVC 帧对齐**：
    - 时间范围超出 UVC 范围时打印 warning
    - UVC 帧数与 GoPro 帧数一致
    - dataset_plan.pkl 中 UVC camera 的 video_start_end 正确
