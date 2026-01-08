# Implementation Plan: 07_generate_replay_buffer.py

## Summary
Handle UVC cameras by skipping tag detection, fisheye correction, and handling different resolutions.

## Changes Overview

### Change 1: Modify video_to_zarr() signature (Line 169)
**Location:** Line 169
**Old:** `def video_to_zarr(replay_buffer, mp4_path, tasks):`
**New:** `def video_to_zarr(replay_buffer, mp4_path, tasks, is_uvc=False):`

### Change 2: Get per-video resolution (After Line 169)
**Location:** After line 169 (start of video_to_zarr)
**Add:**
```python
# Get this video's resolution
with av.open(mp4_path) as container:
    in_stream = container.streams.video[0]
    this_ih, this_iw = in_stream.height, in_stream.width

# Create resize transform for this video's resolution
resize_tf = get_image_transform(
    in_res=(this_iw, this_ih),
    out_res=out_res
)
```

### Change 3: Conditional tag detection loading (Lines 170-171)
**Location:** Lines 170-171
**Old:**
```python
pkl_path = os.path.join(os.path.dirname(mp4_path), 'tag_detection.pkl')
tag_detection_results = pickle.load(open(pkl_path, 'rb'))
```
**New:**
```python
# Only load tag detection for non-UVC cameras
tag_detection_results = None
if not is_uvc:
    pkl_path = os.path.join(os.path.dirname(mp4_path), 'tag_detection.pkl')
    if os.path.exists(pkl_path):
        tag_detection_results = pickle.load(open(pkl_path, 'rb'))
```

### Change 4: Remove duplicate resize_tf creation (Lines 172-175)
**Location:** Lines 172-175
**Action:** Remove these lines (moved to per-video resolution handling above)

### Change 5: Conditional tag inpainting (Lines 216-220)
**Location:** Lines 216-220
**Old:**
```python
# inpaint tags
this_det = tag_detection_results[frame_idx]
all_corners = [x['corners'] for x in this_det['tag_dict'].values()]
for corners in all_corners:
    img = inpaint_tag(img, corners)
```
**New:**
```python
# inpaint tags (only for non-UVC cameras with tag detection)
if tag_detection_results is not None:
    this_det = tag_detection_results[frame_idx]
    all_corners = [x['corners'] for x in this_det['tag_dict'].values()]
    for corners in all_corners:
        img = inpaint_tag(img, corners)
```

### Change 6: Skip fisheye for UVC (Lines 226-229)
**Location:** Lines 226-229
**Old:**
```python
if fisheye_converter is None:
    img = resize_tf(img)
else:
    img = fisheye_converter.forward(img)
```
**New:**
```python
# UVC cameras don't need fisheye correction
if is_uvc or fisheye_converter is None:
    img = resize_tf(img)
else:
    img = fisheye_converter.forward(img)
```

### Change 7: Pass is_uvc flag in executor.submit (Lines 256-257)
**Location:** Lines 256-257
**Old:**
```python
futures.add(executor.submit(video_to_zarr,
    out_replay_buffer, mp4_path, tasks))
```
**New:**
```python
# Check if this is a UVC camera from the task info
is_uvc = tasks[0].get('is_uvc', False) if tasks else False
futures.add(executor.submit(video_to_zarr,
    out_replay_buffer, mp4_path, tasks, is_uvc))
```

### Change 8: (已删除 - 保持原有的 n_cameras 严格断言)
**说明:** 同一组数据中，如果有 UVC 则所有 episode 都必须有 UVC。
相机数量不一致是严重的数据异常，应该直接报错。
保持原有代码不变：
```python
if n_cameras is None:
    n_cameras = len(cameras)
else:
    assert n_cameras == len(cameras)
```

### Change 9: Handle task is_uvc propagation (Lines 139-144)
**Location:** Lines 139-144, in the video tasks building
**Old:**
```python
videos_dict[str(video_path)].append({
    'camera_idx': cam_id,
    'frame_start': video_start,
    'frame_end': video_end,
    'buffer_start': buffer_start
})
```
**New:**
```python
videos_dict[str(video_path)].append({
    'camera_idx': cam_id,
    'frame_start': video_start,
    'frame_end': video_end,
    'buffer_start': buffer_start,
    'is_uvc': camera.get('is_uvc', False)
})
```

## Testing
After implementation, verify:
1. UVC videos are processed without tag detection errors
2. UVC videos use correct resize transform for their resolution
3. Fisheye correction is skipped for UVC
4. Tag inpainting is skipped for UVC
5. Output zarr contains UVC camera data
