# Docker Usage

This repository can be run from a single Docker image so you do not need to
install the Python dependencies on the host.

## Quick start

Build the image:

```bash
docker compose build workspace
```

Open an interactive shell inside the container:

```bash
docker compose run --rm workspace
```

Run the full post-processing pipeline:

```bash
docker compose run --rm workspace \
  python run_slam_pipeline.py /workspace/data/<session_dir>
```

Generate the replay buffer:

```bash
docker compose run --rm workspace \
  python scripts_slam_pipeline/07_generate_replay_buffer.py \
  /workspace/data/<session_dir> \
  -o /workspace/data/<session_dir>/dataset.zarr
```

## Notes for Apple Silicon Macs

The compose file defaults to `linux/amd64` because the upstream Docker images
used by the SLAM steps are commonly distributed for x86 only. Docker Desktop
will emulate x86 on Apple Silicon. This is slower, but it is the most reliable
way to keep the pipeline working.

If you later confirm every dependency you use has stable `arm64` images, you
can override the platform:

```bash
DOCKER_PLATFORM=linux/arm64 docker compose build workspace
```

## What works well in Docker

- Python dependencies and CLI tools
- Video processing
- GoPro IMU extraction
- ORB-SLAM Docker steps
- ArUco detection
- Calibration and dataset generation

## Important limitation on macOS

macOS Docker containers do not provide reliable direct passthrough for the
serial motor device and UVC camera. Because of that:

- post-processing is a good fit for Docker on macOS
- live data collection should still run on the host Mac or on a Linux machine

The repository is now containerized end-to-end at the code level, but the
capture stack is still limited by Docker Desktop hardware access on macOS.
