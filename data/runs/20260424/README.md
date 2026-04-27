# Run `20260424`

GoPro-only data collection, no motor power, no UVC.

- **Hardware**: GoPro Hero 13 (serial `7674`, wired USB), gripper `gp00`.
- **Tags on scene**:
  - `ID 0` (1.6 cm black square, with 2x2 cm white border) on **left** fingertip.
  - `ID 1` (1.6 cm) on **right** fingertip.
  - `ID 13` (16 cm) as **workspace anchor**.
- **Missing on purpose**: `motor_data.jsonl` (motor unpowered), `uvc_video.mp4` (no UVC).
- **29 episodes** total, see `manifest.csv` for original GoPro filenames and recording timestamps.

## Canonical layout

```
gripper_cal.mp4        # ep001 – slow open/close cycles for gripper range calibration
gripper_cal.imu.json
mapping.mp4            # ep002 – workspace scan (largest file, used for ORB-SLAM3 map)
mapping.imu.json
demo_001.mp4           # ep003..ep029 sorted by recording time
demo_001.imu.json
demo_002.mp4
demo_002.imu.json
...
demo_027.mp4
demo_027.imu.json
manifest.csv           # tracked in git
```

## Downloading the raw MP4/IMU

Raw media is **not** tracked in git; it is published as a GitHub Release
attachment (tarball). After cloning this repo:

```bash
bash scripts/datasets/pull.sh 20260424
```

This fetches `20260424.tar.gz` from the latest `data-20260424` release and
extracts it into this directory.

## Rebuilding the post-processing session

After the raw data is in place:

```bash
mkdir -p data_workspace/test_run2/20260424
cp data/runs/20260424/*.mp4 data/runs/20260424/*.imu.json \
   data_workspace/test_run2/20260424/
# (rename .imu.json -> _imu.json for 00_process_videos compatibility)
for f in data_workspace/test_run2/20260424/*.imu.json; do
    mv "$f" "${f%.imu.json}_imu.json"
done
python scripts_slam_pipeline/00_process_videos.py data_workspace/test_run2/20260424
```
