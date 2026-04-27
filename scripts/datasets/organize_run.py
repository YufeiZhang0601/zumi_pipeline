"""
Normalize a raw data/run_<timestamp>/ directory into the canonical
data/runs/<date>/ layout with role-based filenames.

Input  (example):
    data/run_20260424T084256Z/
        run_20260424T084256Z_ep001_gp00_GX010008.MP4
        run_20260424T084256Z_ep001_gp00_GX010008_imu.json
        ...

Output (example):
    data/runs/20260424/
        gripper_cal.mp4       (<- earliest episode, epNNN)
        gripper_cal.imu.json
        mapping.mp4           (<- largest file, epNNN)
        mapping.imu.json
        demo_001.mp4
        demo_001.imu.json
        ...
        manifest.csv          (original_file <-> canonical_file <-> role <-> mtime <-> size)

Usage:
    python scripts/datasets/organize_run.py \
        --src data/run_20260424T084256Z \
        --dst data/runs/20260424
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


RUN_PAT = re.compile(
    r"^(?P<run>run_\d{8}T\d{6}Z)_ep(?P<ep>\d{3})_gp\d{2}_GX\d+\.MP4$"
)


@dataclass
class Episode:
    ep: int
    mp4: Path
    imu: Path | None
    size: int
    mtime: float

    @property
    def stem(self) -> str:
        return self.mp4.stem


def collect_episodes(src: Path) -> list[Episode]:
    episodes: list[Episode] = []
    for mp4 in sorted(src.glob("*.MP4")):
        m = RUN_PAT.match(mp4.name)
        if not m:
            print(f"[skip] unrecognized MP4 name: {mp4.name}")
            continue
        ep = int(m.group("ep"))
        imu = src / (mp4.stem + "_imu.json")
        if not imu.exists():
            imu = None
        episodes.append(
            Episode(
                ep=ep,
                mp4=mp4,
                imu=imu,
                size=mp4.stat().st_size,
                mtime=mp4.stat().st_mtime,
            )
        )
    return episodes


def assign_roles(episodes: list[Episode]) -> dict[int, str]:
    """
    Mirrors scripts_slam_pipeline/00_process_videos.py:
      - earliest episode -> gripper_cal
      - largest file     -> mapping
      - rest by time     -> demo_001, demo_002, ...
    """
    if not episodes:
        return {}

    earliest = min(episodes, key=lambda e: (e.mtime, e.ep))
    remaining = [e for e in episodes if e.ep != earliest.ep]
    largest = max(remaining, key=lambda e: e.size) if remaining else None

    roles: dict[int, str] = {earliest.ep: "gripper_cal"}
    if largest is not None:
        roles[largest.ep] = "mapping"

    demos = [
        e for e in episodes
        if e.ep not in roles
    ]
    demos.sort(key=lambda e: (e.mtime, e.ep))
    for i, e in enumerate(demos, start=1):
        roles[e.ep] = f"demo_{i:03d}"
    return roles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path,
                    help="Source run directory, e.g. data/run_20260424T084256Z")
    ap.add_argument("--dst", required=True, type=Path,
                    help="Destination canonical directory, e.g. data/runs/20260424")
    ap.add_argument("--move", action="store_true",
                    help="Move instead of copy (default: copy, safer)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src: Path = args.src.resolve()
    dst: Path = args.dst.resolve()
    assert src.is_dir(), f"src not found: {src}"

    episodes = collect_episodes(src)
    print(f"[info] found {len(episodes)} episodes in {src}")
    roles = assign_roles(episodes)

    if not args.dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    manifest_rows = []
    for e in sorted(episodes, key=lambda e: e.ep):
        role = roles[e.ep]
        new_mp4 = dst / f"{role}.mp4"
        new_imu = dst / f"{role}.imu.json"
        row = {
            "role": role,
            "ep": e.ep,
            "canonical_mp4": new_mp4.name,
            "canonical_imu": new_imu.name if e.imu else "",
            "original_mp4": e.mp4.name,
            "original_imu": e.imu.name if e.imu else "",
            "size_bytes": e.size,
            "mtime_epoch": f"{e.mtime:.3f}",
        }
        manifest_rows.append(row)
        action = "MOVE" if args.move else "COPY"
        print(f"  [{action}] {e.mp4.name}  ->  {new_mp4.name}")
        if not args.dry_run:
            if args.move:
                shutil.move(str(e.mp4), str(new_mp4))
                if e.imu:
                    shutil.move(str(e.imu), str(new_imu))
            else:
                shutil.copy2(e.mp4, new_mp4)
                if e.imu:
                    shutil.copy2(e.imu, new_imu)

    if args.dry_run:
        print("[dry-run] no files written")
        return

    manifest_path = dst / "manifest.csv"
    with manifest_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()))
        w.writeheader()
        for r in manifest_rows:
            w.writerow(r)
    print(f"[ok] wrote {manifest_path}")


if __name__ == "__main__":
    main()
