import os
import sys
import time
from pathlib import Path
from contextlib import nullcontext

import cv2
import pytest
from fastapi.testclient import TestClient

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from zumi_config import STORAGE_CONF, UVC_CONF  # noqa: E402
from node_uvc import UvcNode, validate  # noqa: E402
from validator import get_video_duration  # noqa: E402


def _camera_available(device: str) -> bool:
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    ok = cap.isOpened()
    cap.release()
    return ok


@pytest.mark.skipif(
    os.environ.get("UVC_TEST", "0") != "1",
    reason="Set UVC_TEST=1 to run hardware-dependent UVC test.",
)
def test_uvc_recording_roundtrip(tmp_path, monkeypatch):
    video_path, sidecar_path, result, duration = _record_and_validate(
        tmp_path, monkeypatch, run_id="run_test_uvc", ep=1, record_seconds=3.0
    )
    assert result.success
    assert duration and duration > 1.0


@pytest.mark.skipif(
    os.environ.get("UVC_TEST", "0") != "1",
    reason="Set UVC_TEST=1 to run hardware-dependent UVC test.",
)
def test_uvc_recording_long_duration(tmp_path, monkeypatch):
    video_path, sidecar_path, result, duration = _record_and_validate(
        tmp_path, monkeypatch, run_id="run_test_uvc_long", ep=1, record_seconds=20.0
    )
    assert result.success
    assert duration and duration > 15.0


@pytest.mark.skipif(
    os.environ.get("UVC_TEST", "0") != "1",
    reason="Set UVC_TEST=1 to run hardware-dependent UVC test.",
)
def test_uvc_recording_multiple_episodes(tmp_path, monkeypatch):
    device = os.environ.get("UVC_DEVICE", UVC_CONF.DEVICE)
    if not _camera_available(device):
        pytest.skip(f"Camera not available at {device}")

    monkeypatch.setattr(STORAGE_CONF, "DATA_DIR", tmp_path)
    monkeypatch.setattr(UVC_CONF, "DEVICE", device)

    node = UvcNode()
    run_id = "run_test_uvc_multi"
    results = []
    with TestClient(node.app) as client:
        for ep in range(1, 4):
            video_path, sidecar_path, result, duration = _record_and_validate(
                tmp_path,
                monkeypatch,
                run_id=run_id,
                ep=ep,
                record_seconds=2.0,
                node=node,
                client=client,
                check_camera=False,
            )
            results.append((video_path, sidecar_path, result, duration))

    for video_path, sidecar_path, result, duration in results:
        assert result.success
        assert video_path.exists()
        assert sidecar_path.exists()
        assert duration and duration > 1.0


@pytest.mark.skipif(
    os.environ.get("UVC_TEST", "0") != "1",
    reason="Set UVC_TEST=1 to run hardware-dependent UVC test.",
)
def test_uvc_recording_with_stop_delay(tmp_path, monkeypatch):
    video_path, sidecar_path, result, duration = _record_and_validate(
        tmp_path,
        monkeypatch,
        run_id="run_test_uvc_stop_delay",
        ep=1,
        record_seconds=2.0,
        stop_delay=2.0,
    )
    assert result.success
    assert duration and duration > 3.0


@pytest.mark.skipif(
    os.environ.get("UVC_TEST", "0") != "1",
    reason="Set UVC_TEST=1 to run hardware-dependent UVC test.",
)
def test_uvc_recording_short_clip(tmp_path, monkeypatch):
    video_path, sidecar_path, result, duration = _record_and_validate(
        tmp_path,
        monkeypatch,
        run_id="run_test_uvc_short",
        ep=1,
        record_seconds=1.0,
    )
    assert result.success
    assert duration and duration > 0.5


def _record_and_validate(
    tmp_path,
    monkeypatch,
    run_id: str,
    ep: int,
    record_seconds: float,
    stop_delay: float = 0.0,
    node: UvcNode | None = None,
    client: TestClient | None = None,
    check_camera: bool = True,
):
    device = os.environ.get("UVC_DEVICE", UVC_CONF.DEVICE)
    if check_camera and not _camera_available(device):
        pytest.skip(f"Camera not available at {device}")

    # Point data dir to temp and device to env override
    monkeypatch.setattr(STORAGE_CONF, "DATA_DIR", tmp_path)
    monkeypatch.setattr(UVC_CONF, "DEVICE", device)

    if node is None:
        node = UvcNode()

    def _poll_files(video_path: Path, sidecar_path: Path, attempts: int = 100, interval: float = 0.2):
        for _ in range(attempts):
            if video_path.exists() and video_path.stat().st_size > 0 and sidecar_path.exists():
                return True
            time.sleep(interval)
        return False

    def _wait_idle(ctx, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = ctx.get("/status").json().get("status")
            except Exception:
                status = None
            if status in ("IDLE", "READY"):
                return True
            time.sleep(0.1)
        return False

    def _wait_duration(video_path: Path, attempts: int = 10, interval: float = 0.5):
        for _ in range(attempts):
            dur = get_video_duration(video_path)
            if dur is not None:
                return dur
            time.sleep(interval)
        return None

    manager = TestClient(node.app) if client is None else nullcontext(client)
    with manager as ctx:
        resp = ctx.post("/prepare", json={"run_id": run_id, "episode": ep})
        assert resp.status_code == 200

        resp = ctx.post("/start", json={"run_id": run_id, "episode": ep})
        assert resp.status_code in (200, 202)

        time.sleep(record_seconds)

        if stop_delay > 0:
            time.sleep(stop_delay)

        resp = ctx.post("/stop", json={})
        assert resp.status_code in (200, 202)

        video_path, sidecar_path = node._episode_paths(run_id, ep)

        _wait_idle(ctx, timeout=15.0)
        assert _poll_files(video_path, sidecar_path)

        assert video_path.exists() and video_path.stat().st_size > 0
        assert sidecar_path.exists()
        with sidecar_path.open() as fh:
            first_line = fh.readline().strip()
            assert first_line, "Sidecar should contain at least one record"

        duration = _wait_duration(video_path)
        result = validate(run_id, ep)

    return video_path, sidecar_path, result, duration
