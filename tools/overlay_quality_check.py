#!/usr/bin/env python3
"""
Overlay quality check
=====================

Draws the SLAM-derived **world frame** (table ArUco tag center, id=13) and
the **end-effector / TCP frame** on top of each demo's GoPro ``raw_video.mp4``
so a human can eyeball whether SLAM + TCP calibration are sane *before*
spending compute on training.

What the overlay shows
----------------------
* WORLD triad: origin = center of the table ArUco tag chosen by step 05.
  As the camera moves, this triad should stay glued to the physical tag on
  the table. If it floats away, SLAM/tag calibration is bad.
* TCP triad: origin = where step 06 puts the gripper tip in camera frame.
  This stays fixed in the image (camera-frame quantity) and should land at
  the actual gripper tip. If it sits on empty space, ``tcp_offset`` is
  wrong for this hardware.
* TCP trajectory polyline: the entire demo's TCP path expressed in the
  current frame, drawn back-projected. Sudden jumps mean SLAM tracking
  glitched.
* HUD text: TRACKED / LOST status + frame index per frame.

Usage
-----
::

    python tools/overlay_quality_check.py <session_dir>
    python tools/overlay_quality_check.py <session_dir> --demos demo_ep003_gp00,mapping
    python tools/overlay_quality_check.py <session_dir> --scale 1.0 --no_trajectory

Outputs (per demo dir):
    quality_overlay.mp4           - annotated video
    quality_overlay_summary.json  - tracked ratio, world/tcp visibility, etc.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import math
import multiprocessing
import os
import pathlib
import sys
from typing import Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import cv2
import numpy as np
import pandas as pd

from umi.common.cv_util import parse_fisheye_intrinsics
from umi.common.pose_util import pose_to_mat


LOGGER = logging.getLogger("overlay_quality")


# ---------------------------------------------------------------------------
# math helpers
# ---------------------------------------------------------------------------

def quat_xyzw_to_rotmat(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n == 0.0:
        return np.eye(3)
    x /= n
    y /= n
    z /= n
    w /= n
    return np.array([
        [1 - 2 * (y * y + z * z),     2 * (x * y - z * w),     2 * (x * z + y * w)],
        [    2 * (x * y + z * w), 1 - 2 * (x * x + z * z),     2 * (y * z - x * w)],
        [    2 * (x * z - y * w),     2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def project_fisheye(P_cam: np.ndarray, K: np.ndarray, D: np.ndarray) -> np.ndarray:
    """Project Nx3 points (camera frame) through a fisheye/Kannala-Brandt model.

    Returns ``Nx2`` pixel coords. Points behind the camera (or too close to the
    plane Z=0) are returned as ``np.nan`` so the caller can skip them.
    """
    P = np.asarray(P_cam, dtype=np.float64).reshape(-1, 3)
    out = np.full((P.shape[0], 2), np.nan, dtype=np.float64)
    in_front = P[:, 2] > 1e-3
    if not in_front.any():
        return out
    pts = P[in_front].reshape(-1, 1, 3)
    rvec = np.zeros((3, 1))
    tvec = np.zeros((3, 1))
    px, _ = cv2.fisheye.projectPoints(pts, rvec, tvec, K, D)
    out[in_front] = px.reshape(-1, 2)
    return out


def _safe_int_pt(uv: np.ndarray) -> Optional[Tuple[int, int]]:
    if np.any(np.isnan(uv)):
        return None
    return int(round(uv[0])), int(round(uv[1]))


def draw_triad(img: np.ndarray, origin_uv: np.ndarray, end_uvs: np.ndarray,
               colors, thickness: int = 2, label: Optional[str] = None) -> bool:
    o = _safe_int_pt(origin_uv)
    if o is None:
        return False
    h, w = img.shape[:2]
    if not (0 <= o[0] < w and 0 <= o[1] < h):
        return False
    drawn = False
    for end_uv, color in zip(end_uvs, colors):
        e = _safe_int_pt(end_uv)
        if e is None:
            continue
        cv2.line(img, o, e, color, thickness, cv2.LINE_AA)
        drawn = True
    if drawn and label is not None:
        cv2.putText(img, label, (o[0] + 6, o[1] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                    cv2.LINE_AA)
    return drawn


def draw_polyline(img: np.ndarray, pts_uv: np.ndarray,
                  color=(0, 255, 255), thickness: int = 2) -> None:
    last = None
    for uv in pts_uv:
        p = _safe_int_pt(uv)
        if p is None:
            last = None
            continue
        if last is not None:
            cv2.line(img, last, p, color, thickness, cv2.LINE_AA)
        last = p


# ---------------------------------------------------------------------------
# per-demo overlay
# ---------------------------------------------------------------------------

def _resolve_csv(demo_dir: pathlib.Path) -> Optional[pathlib.Path]:
    for name in ("camera_trajectory.csv", "mapping_camera_trajectory.csv"):
        p = demo_dir / name
        if p.is_file():
            return p
    return None


def overlay_one_demo(
    demo_dir: pathlib.Path,
    tx_slam_tag: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    intr_dim: Tuple[int, int],
    tcp_offset: float,
    axis_len_world: float,
    axis_len_tcp: float,
    output_scale: float,
    draw_trajectory: bool,
    out_filename: str,
) -> Tuple[str, str, Optional[dict]]:
    raw_path = demo_dir / "raw_video.mp4"
    if not raw_path.is_file():
        return ("skip_no_video", demo_dir.name, None)

    csv_path = _resolve_csv(demo_dir)
    if csv_path is None:
        return ("skip_no_csv", demo_dir.name, None)

    df = pd.read_csv(csv_path)
    required_cols = {"is_lost", "x", "y", "z", "q_x", "q_y", "q_z", "q_w"}
    if not required_cols.issubset(df.columns):
        return ("skip_bad_csv", demo_dir.name, None)

    # TCP transform inside camera frame -- mirror of step 06's constants.
    cam_to_mount_offset = 0.01450
    cam_to_center_height = 0.068
    pose_cam_tcp = np.array([
        0.0, cam_to_center_height, tcp_offset - cam_to_mount_offset,
        0.0, 0.0, 0.0,
    ], dtype=np.float64)
    tx_cam_tcp = pose_to_mat(pose_cam_tcp)

    def axes_h(L: float) -> np.ndarray:
        return np.array([
            [0, 0, 0, 1],
            [L, 0, 0, 1],
            [0, L, 0, 1],
            [0, 0, L, 1],
        ], dtype=np.float64).T  # (4, 4)

    world_axes_local = axes_h(axis_len_world)
    tcp_axes_local = axes_h(axis_len_tcp)

    cap = cv2.VideoCapture(str(raw_path))
    if not cap.isOpened():
        return ("skip_open_failed", demo_dir.name, None)

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    in_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    in_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    intr_w, intr_h = int(intr_dim[0]), int(intr_dim[1])
    if (in_w, in_h) != (intr_w, intr_h):
        LOGGER.warning(
            "%s: video %dx%d != intrinsics %dx%d (projection may be off)",
            demo_dir.name, in_w, in_h, intr_w, intr_h,
        )

    out_w = max(1, int(round(in_w * output_scale)))
    out_h = max(1, int(round(in_h * output_scale)))
    out_path = demo_dir / out_filename
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (out_w, out_h))
    if not writer.isOpened():
        cap.release()
        return ("skip_writer_failed", demo_dir.name, None)

    tx_tag_slam = np.linalg.inv(tx_slam_tag)

    # Pre-compute downsampled TCP trajectory in tag frame for the polyline.
    traj_tag_h: Optional[np.ndarray] = None
    if draw_trajectory:
        valid_df = df.loc[~df["is_lost"]]
        if len(valid_df) > 1:
            n_keep = min(300, len(valid_df))
            sel = np.linspace(0, len(valid_df) - 1, n_keep).astype(int)
            cam_pos = valid_df[["x", "y", "z"]].to_numpy()[sel]
            cam_q = valid_df[["q_x", "q_y", "q_z", "q_w"]].to_numpy()[sel]
            tcp_in_tag = np.empty((n_keep, 3), dtype=np.float64)
            for j, (p, q) in enumerate(zip(cam_pos, cam_q)):
                tx_slam_cam = np.eye(4)
                tx_slam_cam[:3, :3] = quat_xyzw_to_rotmat(q)
                tx_slam_cam[:3, 3] = p
                tx_tag_tcp = tx_tag_slam @ tx_slam_cam @ tx_cam_tcp
                tcp_in_tag[j] = tx_tag_tcp[:3, 3]
            traj_tag_h = np.concatenate(
                [tcp_in_tag, np.ones((n_keep, 1))], axis=1
            ).T  # (4, n_keep)

    n_tracked = 0
    n_world_drawn = 0
    n_tcp_drawn = 0
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        is_lost = True
        row = None
        if frame_idx < len(df):
            row = df.iloc[frame_idx]
            is_lost = bool(row["is_lost"])

        if not is_lost and row is not None:
            n_tracked += 1
            t = np.array([row["x"], row["y"], row["z"]], dtype=np.float64)
            q = np.array(
                [row["q_x"], row["q_y"], row["q_z"], row["q_w"]],
                dtype=np.float64,
            )
            tx_slam_cam = np.eye(4)
            tx_slam_cam[:3, :3] = quat_xyzw_to_rotmat(q)
            tx_slam_cam[:3, 3] = t
            tx_cam_slam = np.linalg.inv(tx_slam_cam)
            tx_cam_tag = tx_cam_slam @ tx_slam_tag

            world_axes_cam = (tx_cam_tag @ world_axes_local)[:3].T
            world_uv = project_fisheye(world_axes_cam, K, D)
            if draw_triad(
                frame, world_uv[0], world_uv[1:],
                [(0, 0, 255), (0, 255, 0), (255, 0, 0)],
                thickness=4, label="WORLD",
            ):
                n_world_drawn += 1

            tcp_axes_cam = (tx_cam_tcp @ tcp_axes_local)[:3].T
            tcp_uv = project_fisheye(tcp_axes_cam, K, D)
            if draw_triad(
                frame, tcp_uv[0], tcp_uv[1:],
                [(0, 0, 255), (0, 255, 0), (255, 0, 0)],
                thickness=3, label="TCP",
            ):
                n_tcp_drawn += 1

            if traj_tag_h is not None:
                traj_cam = (tx_cam_tag @ traj_tag_h)[:3].T
                traj_uv = project_fisheye(traj_cam, K, D)
                draw_polyline(frame, traj_uv, color=(0, 255, 255), thickness=2)

            status_color = (0, 255, 0)
            status_text = "TRACKED"
        else:
            status_color = (0, 0, 255)
            status_text = "LOST"

        title_scale = max(in_h / 1080.0, 1.0)
        cv2.putText(
            frame, demo_dir.name, (20, int(60 * title_scale)),
            cv2.FONT_HERSHEY_SIMPLEX, 1.2 * title_scale,
            (255, 255, 255), max(2, int(3 * title_scale)), cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            f"{status_text}  frame {frame_idx}/{n_frames}",
            (20, int(110 * title_scale)),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0 * title_scale,
            status_color, max(2, int(3 * title_scale)), cv2.LINE_AA,
        )

        if (out_w, out_h) != (in_w, in_h):
            frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()

    n_total = max(frame_idx, 1)
    n_tr = max(n_tracked, 1)
    summary = {
        "demo": demo_dir.name,
        "csv_used": csv_path.name,
        "n_frames": int(frame_idx),
        "n_tracked": int(n_tracked),
        "tracked_ratio": float(n_tracked) / float(n_total),
        "world_origin_visible_ratio": float(n_world_drawn) / float(n_tr),
        "tcp_origin_visible_ratio": float(n_tcp_drawn) / float(n_tr),
        "out_path": str(out_path),
        "out_resolution": [int(out_w), int(out_h)],
    }
    with (demo_dir / "quality_overlay_summary.json").open("w") as fh:
        json.dump(summary, fh, indent=2)

    return ("ok", demo_dir.name, summary)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(
        description="Overlay world & TCP triads on GoPro demos for visual QA.",
    )
    parser.add_argument("session_dir", help="Session directory (parent of demos/).")
    parser.add_argument(
        "--demos", default=None,
        help="Comma-separated demo dir names; default: all demo_* + mapping.",
    )
    parser.add_argument(
        "--intrinsics", default=None,
        help="Path to GoPro fisheye intrinsics JSON (defaults to "
             "example/calibration/gopro_intrinsics_gopro13sn7674.json).",
    )
    parser.add_argument(
        "--tx_slam_tag", default=None,
        help="Override path to tx_slam_tag.json (default: <session>/demos/mapping/tx_slam_tag.json).",
    )
    parser.add_argument(
        "--tcp_offset", type=float, default=0.107335,
        help="Distance from gripper tip to mounting screw (matches step 06).",
    )
    parser.add_argument(
        "--axis_len_world", type=float, default=0.10,
        help="Length of world triad axes (m).",
    )
    parser.add_argument(
        "--axis_len_tcp", type=float, default=0.05,
        help="Length of TCP triad axes (m).",
    )
    parser.add_argument(
        "--scale", type=float, default=0.5,
        help="Output scale factor; default 0.5x to keep file size manageable.",
    )
    parser.add_argument(
        "--no_trajectory", action="store_true",
        help="Disable TCP trajectory polyline overlay.",
    )
    parser.add_argument("--num_workers", type=int, default=None)
    parser.add_argument(
        "--out_name", default="quality_overlay.mp4",
        help="Output filename inside each demo dir.",
    )
    args = parser.parse_args()

    session_dir = pathlib.Path(args.session_dir).expanduser().absolute()
    demos_dir = session_dir / "demos"
    if not demos_dir.is_dir():
        LOGGER.error("Not a session directory (no demos/): %s", session_dir)
        sys.exit(2)

    tx_path = (
        pathlib.Path(args.tx_slam_tag).expanduser().absolute()
        if args.tx_slam_tag
        else demos_dir / "mapping" / "tx_slam_tag.json"
    )
    if not tx_path.is_file():
        LOGGER.error(
            "Missing %s. Did step 05 (calibrate_slam_tag) run successfully?",
            tx_path,
        )
        sys.exit(2)
    tx_slam_tag = np.array(
        json.load(tx_path.open())["tx_slam_tag"], dtype=np.float64,
    )

    intr_path = (
        pathlib.Path(args.intrinsics).expanduser().absolute()
        if args.intrinsics
        else pathlib.Path(ROOT_DIR) / "example" / "calibration"
                                       / "gopro_intrinsics_gopro13sn7674.json"
    )
    if not intr_path.is_file():
        LOGGER.error("Intrinsics JSON not found: %s", intr_path)
        sys.exit(2)
    intr = parse_fisheye_intrinsics(json.load(intr_path.open()))
    K = intr["K"]
    D = intr["D"]
    intr_dim = tuple(int(x) for x in intr["DIM"])

    if args.demos:
        names = [s.strip() for s in args.demos.split(",") if s.strip()]
        demo_dirs = [demos_dir / n for n in names]
    else:
        demo_dirs = sorted(
            p for p in demos_dir.iterdir()
            if p.is_dir()
            and (p.name.startswith("demo_") or p.name == "mapping")
        )
    demo_dirs = [d for d in demo_dirs if d.is_dir()]

    if not demo_dirs:
        LOGGER.error("No demo directories found under %s", demos_dir)
        sys.exit(2)

    LOGGER.info("session: %s", session_dir)
    LOGGER.info("demos:   %d", len(demo_dirs))
    LOGGER.info("scale:   %.2f x  (output: %s)", args.scale, args.out_name)
    LOGGER.info("tx_slam_tag: %s", tx_path)
    LOGGER.info("intrinsics:  %s", intr_path)

    n_workers = args.num_workers or max(1, multiprocessing.cpu_count() // 2)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [
            pool.submit(
                overlay_one_demo,
                d, tx_slam_tag, K, D, intr_dim,
                args.tcp_offset, args.axis_len_world, args.axis_len_tcp,
                args.scale, not args.no_trajectory, args.out_name,
            )
            for d in demo_dirs
        ]
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                LOGGER.exception("Worker failed for an unknown demo")
                results.append(("error", "?", None))

    print()
    header = (
        f"{'demo':<32} {'csv':<32} "
        f"{'frames':>7} {'tracked%':>9} {'world%':>8} {'tcp%':>7}"
    )
    print(header)
    print("-" * len(header))
    n_ok = 0
    for status, name, summary in sorted(results, key=lambda r: r[1]):
        if status != "ok" or summary is None:
            print(f"{name:<32} {status}")
            continue
        n_ok += 1
        print(
            f"{summary['demo']:<32} {summary['csv_used']:<32} "
            f"{summary['n_frames']:>7d} "
            f"{summary['tracked_ratio'] * 100:>8.1f}% "
            f"{summary['world_origin_visible_ratio'] * 100:>7.1f}% "
            f"{summary['tcp_origin_visible_ratio'] * 100:>6.1f}%"
        )
    print()
    print(
        f"Done. {n_ok} / {len(results)} demos produced overlays. "
        f"See <demo>/{args.out_name}"
    )


if __name__ == "__main__":
    main()
