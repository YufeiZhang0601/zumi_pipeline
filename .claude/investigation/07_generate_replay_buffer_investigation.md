# Investigation: 07_generate_replay_buffer.py

## File Overview

**Path:** `scripts_slam_pipeline/07_generate_replay_buffer.py`
**Lines:** 275
**Purpose:** Generate zarr replay buffer from dataset_plan.pkl

## File Structure

```
main() function (Lines 45-270):
  - Line 70-71: Create output replay buffer
  - Line 80-148: Load dataset_plan.pkl and aggregate video tasks
  - Line 153-155: Get image size from first video
  - Line 158-167: Create camera datasets in zarr
  - Line 169-243: video_to_zarr() - Main video processing function
  - Line 245-260: ThreadPoolExecutor for parallel processing
  - Line 264-269: Save to zip store
```

## Camera Processing Analysis

### video_to_zarr() Function (Lines 169-243)

#### Tag Detection Loading (Lines 170-171)
```python
pkl_path = os.path.join(os.path.dirname(mp4_path), 'tag_detection.pkl')
tag_detection_results = pickle.load(open(pkl_path, 'rb'))
```
**Issue:** UVC doesn't have tag_detection.pkl

#### Image Transform (Lines 172-175)
```python
resize_tf = get_image_transform(
    in_res=(iw, ih),
    out_res=out_res
)
```
**Issue:** UVC may have different resolution than GoPro

#### Fisheye Correction (Lines 226-229)
```python
if fisheye_converter is None:
    img = resize_tf(img)
else:
    img = fisheye_converter.forward(img)
```
**Issue:** UVC doesn't need fisheye correction

#### Tag Inpainting (Lines 217-220)
```python
this_det = tag_detection_results[frame_idx]
all_corners = [x['corners'] for x in this_det['tag_dict'].values()]
for corners in all_corners:
    img = inpaint_tag(img, corners)
```
**Issue:** UVC doesn't have tag detection results

### Input Resolution Handling (Lines 153-155)
```python
with av.open(vid_args[0][0]) as container:
    in_stream = container.streams.video[0]
    ih, iw = in_stream.height, in_stream.width
```
**Issue:** All videos assumed to have same resolution. UVC may differ.

## Key Modification Points

### 1. Pass is_uvc flag through camera dict
The camera dict in dataset_plan.pkl needs to include `is_uvc` flag.

### 2. Modify video_to_zarr() to handle UVC
- Skip tag_detection.pkl loading for UVC
- Skip tag inpainting for UVC
- Create separate resize transform for UVC (different input resolution)
- Never use fisheye correction for UVC

### 3. Handle different resolutions
Currently assumes all videos have same resolution. Need to:
- Query each video's resolution separately
- Create appropriate resize transform per video

## Suggested Code Changes

### video_to_zarr() modifications:

```python
def video_to_zarr(replay_buffer, mp4_path, tasks, is_uvc=False):
    # Get video resolution
    with av.open(mp4_path) as container:
        in_stream = container.streams.video[0]
        this_ih, this_iw = in_stream.height, in_stream.width

    # Create resize transform for this video's resolution
    resize_tf = get_image_transform(
        in_res=(this_iw, this_ih),
        out_res=out_res
    )

    # Only load tag detection for non-UVC
    tag_detection_results = None
    if not is_uvc:
        pkl_path = os.path.join(os.path.dirname(mp4_path), 'tag_detection.pkl')
        if os.path.exists(pkl_path):
            tag_detection_results = pickle.load(open(pkl_path, 'rb'))

    # ... in the frame loop:

    # Only inpaint tags for non-UVC cameras
    if tag_detection_results is not None:
        this_det = tag_detection_results[frame_idx]
        all_corners = [x['corners'] for x in this_det['tag_dict'].values()]
        for corners in all_corners:
            img = inpaint_tag(img, corners)

    # Never use fisheye for UVC
    if is_uvc or fisheye_converter is None:
        img = resize_tf(img)
    else:
        img = fisheye_converter.forward(img)
```

### Call site modification (Line 256-257):
```python
is_uvc = tasks[0].get('is_uvc', False) if tasks else False
futures.add(executor.submit(video_to_zarr,
    out_replay_buffer, mp4_path, tasks, is_uvc))
```
