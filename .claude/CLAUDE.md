# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Distributed hardware data capture system for robotic manipulation. Uses FastAPI nodes coordinated by an orchestrator to capture synchronized data from GoPro cameras, UVC cameras, and DM3510 motors.

## Architecture

```
┌─────────────────┐     HTTP/ZMQ      ┌──────────────┐
│   Orchestrator  │◄────────────────►│  node_gopro  │ (port 8001)
│  (CLI + Policy) │                   └──────────────┘
│                 │◄────────────────►│  node_motor  │ (port 8002)
│                 │                   └──────────────┘
│                 │◄────────────────►│   node_uvc   │ (port 8003)
└─────────────────┘                   └──────────────┘
```

**Separation of Concerns:**
- **Nodes (Mechanism)**: Provide atomic operations (`start`, `stop`, `download`). Don't decide policy.
- **Orchestrator (Policy)**: Manages lifecycle, coordinates nodes, handles user interaction.
- **zumi_core.py**: Base `NodeHTTPService` class with state machine.

### State Machine

```
INIT → IDLE ⇄ READY → RECORDING → SAVING → IDLE
       ↓        ↓         ↓
     ERROR ← ERROR ← ERROR
       ↓
   RECOVERING → IDLE (success) / ERROR (fail)
```

### Thread/Process Model

```
NodeHTTPService (Main Thread: FastAPI uvicorn)
├── main_loop_wrapper    (Daemon Thread: business logic loop)
├── _heartbeat_loop      (Daemon Thread: ZMQ status push + health check)
└── _writer_loop         (Daemon Thread: async disk IO) [MotorNode only]

MotorPreviewProcess      (Separate Process: real-time visualization, avoids GIL)
```

### ZMQ Status Protocol

Nodes push status to Orchestrator via ZMQ PUB/SUB:
```python
{
    "node": "motor_gp00",
    "status": "RECORDING",  # INIT/IDLE/READY/RECORDING/SAVING/ERROR/OFFLINE/RECOVERING
    "is_recording": True,
    "run_id": "run_20251221T072536Z",
    "episode": 1,
    "last_error": None,
    "ts": 1704844800.123  # unix timestamp
}
```

## Development Commands

```bash
# Dependency management (using uv)
uv sync                              # Install all dependencies
uv add <package>                     # Add new dependency
uv run python <script.py>            # Run with venv

# Tests
pytest                               # Run all tests
pytest tests/test_orchestrator.py -v # Run specific test

# Start nodes (each in separate terminal)
python node_motor.py                 # Basic
python node_motor.py --preview       # With real-time visualization
python node_uvc.py
python node_gopro.py

# Start orchestrator (interactive CLI)
python orchestrator.py
python orchestrator.py --run-id <existing_id>  # Resume existing run
python orchestrator.py --tag <tag>             # New run with tag
python orchestrator.py --delay 0.2             # Custom sync delay
```

## System Dependencies

External tools required (not managed by uv):

| Tool | Purpose | Used By |
|------|---------|---------|
| `ffmpeg` / `ffprobe` | Video decoding, metadata extraction | validator.py |
| `paplay` | Sound notifications (Linux) | orchestrator.py |
| `docker` | IMU data extraction (chicheng/openicc) | validator.py |

## Data Directory Structure

```
data/
└── {run_id}/                                    # e.g. run_20251221T072536Z
    ├── {run_id}_ep{NNN}_{gripper_id}_motor.jsonl   # Motor data (JSONL)
    ├── {run_id}_ep{NNN}_{gripper_id}_uvc.mp4       # UVC camera video
    ├── {run_id}_ep{NNN}_{gripper_id}_{video_id}.MP4 # GoPro video
    ├── .recollect.json                          # Episodes to re-collect
    └── .validated.json                          # Validation history
```

**Naming Convention:** `{run_id}_ep{NNN}_{gripper_id}_{source}.{ext}`
- `run_id`: UTC timestamp, e.g. `run_20251221T072536Z`
- `NNN`: Zero-padded episode number, e.g. `001`, `002`
- `gripper_id`: Gripper identifier, e.g. `gp00`

## Data Processing Pipeline

Sequential scripts in `scripts_slam_pipeline/`:
```
00_process_videos.py    → Extract/convert videos
01_extract_gopro_imu.py → Extract IMU from GoPro
02_create_map.py        → SLAM mapping
03_batch_slam.py        → Localization
04_detect_aruco.py      → ArUco detection
05_run_calibrations.py  → Gripper calibration
06_generate_dataset_plan.py → Cross-correlation alignment
07_generate_replay_buffer.py → Final dataset
```

Run with: `python scripts_slam_pipeline/0X_script.py --input data/{run_id}`

## Configuration

All config in `zumi_config.py`. Key dataclasses:
- `NodeStatus`: State enum (`INIT`, `IDLE`, `READY`, `RECORDING`, `SAVING`, `ERROR`, `OFFLINE`, `RECOVERING`)
- `MOTOR_CONF`: Serial port, slave/master IDs, target frequency
- `HTTP_CONF`: Node URLs and ports
- `GRIPPER_MAPPINGS`: Maps gripper_id to motor/camera config

### Key Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MOTOR_CONF.TARGET_FREQ` | 150 Hz | Motor control loop frequency |
| `UVC_CONF.FPS` | 60 | UVC camera frame rate |
| `HEALTH_CHECK_INTERVAL` | 5s | Time between health checks |
| `HEALTH_CHECK_MAX_FAILURES` | 3 | Failures before ERROR state |
| `MAX_RECOVERY_ATTEMPTS` | 5 | Max auto-recovery attempts |
| `RECOVERY_BACKOFF_BASE` | 2.0s | Initial backoff (exponential) |
| `RECOVERY_BACKOFF_MAX` | 60s | Maximum backoff time |

## Recovery Mechanism

Nodes implement automatic recovery with exponential backoff:

1. **Health Check** → Detects hardware failure (3 consecutive failures)
2. **Discard Recording** → If recording, data is discarded immediately
3. **Enter RECOVERING** → State transition, publish to orchestrator
4. **Exponential Backoff** → Wait `2^n * 2.0s` (max 60s)
5. **Cleanup** → `_cleanup_for_recovery()` releases old resources
6. **Reinitialize** → `on_recover()` recreates driver/connection
7. **Reset State** → `after_recover()` clears buffers, counters
8. **Resume** → Back to IDLE, main_loop restarts

## Validation

Each node implements a `validate(run_id, episode)` function returning `ValidationResult`.

### Error Codes

| Error Code | Meaning |
|------------|---------|
| `video_missing` | GoPro video file not found |
| `video_corrupt` | Video decoding failed (ffmpeg error) |
| `video_too_short` | Video duration below threshold |
| `motor_missing` | Motor JSONL file not found |
| `motor_empty` | Motor data file is empty |
| `motor_flat` | Motor position not changing (<5 unique values) |
| `motor_sample_rate_low` | Sample rate <50% of expected |
| `motor_start_nonzero` | Initial position not near zero |
| `uvc_missing` | UVC video file not found |
| `uvc_corrupt` | UVC video decoding failed |

## Test Structure

```
tests/
├── test_node_motor.py         # Motor node unit tests
├── test_node_gopro.py         # GoPro node unit tests
├── test_node_uvc_integration.py # UVC integration tests
├── test_orchestrator.py       # Orchestrator logic tests
└── test_validator.py          # Validation logic tests
```

---

# Code Principles (The "Zen of Zumi")

Core Values: **Simple, Robust, Observable.**

## 1. Architecture & Design

### 1.1 Separation of Mechanism and Policy
*   **Node (Mechanism)**: Responsible for "how to do". Provides atomic operations (`start`, `stop`, `download`) and hardware abstraction. The Node should not decide "whether to record now", only "whether it can record".
*   **Orchestrator (Policy)**: Responsible for "when to do". Manages the entire lifecycle, run ID generation, node coordination, and error handling decisions.
*   *Anti-pattern Example*: Hardcoding logic like "if ep001, then auto-restart" inside a GoPro node.

### 1.2 Flat is Better than Nested
*   Avoid deep inheritance hierarchies. `NodeHTTPService` -> `GoProNode` (two levels) is enough.
*   Avoid overly abstract factory patterns. If there is only one type of motor, instantiate `DMMotorDriver` directly—no need for an `AbstractMotorFactory`.
*   FastAPI route functions (views) should be minimal, only parsing parameters, and should delegate logic to business methods.

## 2. State Management

### 2.1 State is Truth
*   **Explicit State Machines**: Strictly define node states using Enums (`IDLE`, `READY`, `RECORDING`, `ERROR`, `RECOVERING`).
*   **No Implicit Flags**: Do not use boolean flag combos like `is_running` and `flag_started` to guess the state.
*   **Single Source of Truth**: Node status must be based on its in-memory/hardware actual state. The Orchestrator is just an "observer" and "commander" of the node state and should not naively cache state.

### 2.2 Deadman Switch / Watchdog
*   Hardware control systems must assume the controller can crash at any time.
*   **Principle**: Any continuous and potentially dangerous operation (motor turning, high-power recording) must rely on an ongoing heartbeat.
*   *Rule*: If no heartbeat from the Orchestrator is received after X seconds -> **immediately auto-stop and roll back to a safe state**.
*   **Current Implementation**: Nodes use `HEALTH_CHECK_INTERVAL` (5s) to detect hardware failures and auto-recover.

## 3. Data Safety & IO

### 3.1 Persistence First
*   **Memory is Untrustworthy**: Processes can be killed anytime, computers may lose power abruptly.
*   **Queue Persistence**: Download queues and pending tasks must be written to disk (JSON/SQLite). On startup, always reload tasks remaining on disk.
*   **Streaming Writes**: Do not keep high-frequency data (e.g., motor logs) in memory to write at the end. Use `append-only` streaming writes to disk, or write in batches (chunks).
*   **Current Implementation**: MotorNode uses a dedicated writer thread with batch writes (50 records/batch at 200Hz).

### 3.2 Idempotency & Atomicity
*   **Atomic File Operations**: When writing files, first write to `.tmp`, and only after closing, rename to the final filename. This prevents partial files if power loss occurs during the write.
*   **Idempotency**: If `download(file_a)` is called more than once, the second call should detect "already done" and just return Success, not error or re-download.

## 4. Error Handling

### 4.1 Boundary Defense
*   **Do not wrap every line of business code in try-catch**. This makes code unreadable and hides real bugs.
*   **Only catch at boundaries**:
    1.  **API entry points**: Catch all unknown exceptions, return HTTP 500 and log the stacktrace.
    2.  **Hardware IO layer**: Catch connection/timeouts, convert to explicit `HardwareError` and re-raise upward.
    3.  **Thread/process entry point**: Prevent subthread crashes from causing silent main process exits.

### 4.2 Let It Crash & Recover
*   If facing an unresolvable hardware error (e.g. USB disconnect), do not try to patch it at a low level.
*   **Directly throw exception -> change state to ERROR -> trigger recovery flow**.
*   The recovery flow should be thorough: close old connections -> release resources -> wait cooldown -> reinitialize.

### 4.3 No Fallback Defaults
*   If required data is missing or invalid, raise an error immediately.
*   Do not silently use default values to "keep going"—this masks upstream problems.

## 5. Concurrency & Communication

### 5.1 Avoid Lock Contention (Lock-Free Design)
*   Python's GIL limits multithreading computation.
*   **Compute/IO Separation**: Heavy IO (disk writes) or computation should go in a separate `Process`, communicating via `Queue`.
*   **Keep Main Thread Lightweight**: Main thread should only handle HTTP requests and state scheduling, ensuring API never blocks.
*   **Current Implementation**: MotorNode uses separate writer thread for disk IO, separate process for preview visualization.

### 5.2 Timeout is Mandatory
*   Never make any network/hardware calls without a `timeout`.
*   `requests.get(url)` → **Wrong**.
*   `requests.get(url, timeout=3.0)` → **Correct**.
*   **Current Implementation**: `NodeClient` uses `timeout=2.0` for all HTTP calls.

## 6. Style Details

*   **Logs are Documentation**: Logs must include `[Component] [Action] Status`. You should be able to reconstruct sequence diagrams from the logs.
*   **Type Hints**: Since we use Python 3.9+, add type hints to all function inputs and return values whenever possible.
*   **Configuration Separation**: All IPs, ports, timeouts, file paths must be extracted to `zumi_config.py`—no magic numbers or strings in code.
