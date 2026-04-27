#!/usr/bin/env bash
# Pack a run directory and upload it as a GitHub Release asset.
#
# Usage:
#   bash scripts/datasets/push.sh 20260424
#
# Mechanics:
#   - Tars data/runs/<RUN>/ (excluding README.md / manifest.csv which are
#     already tracked in git) into /tmp/<RUN>.tar.gz.
#   - Creates or updates a release tagged `data-<RUN>` and uploads the
#     tarball as an asset named `<RUN>.tar.gz`.
#
# Requires: `gh` CLI, logged in (`gh auth status`).

set -euo pipefail

RUN="${1:-}"
if [[ -z "$RUN" ]]; then
    echo "Usage: $0 <run_id>   (e.g. 20260424)" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO_ROOT/data/runs/$RUN"
TAG="data-$RUN"
ASSET_NAME="$RUN.tar.gz"
TARBALL="/tmp/$ASSET_NAME"

if [[ ! -d "$SRC" ]]; then
    echo "[push] source not found: $SRC" >&2
    exit 3
fi
if ! command -v gh >/dev/null 2>&1; then
    echo "[push] gh CLI not installed. Install from https://cli.github.com/" >&2
    exit 4
fi

cd "$REPO_ROOT/data/runs"

echo "[push] tarring $SRC -> $TARBALL ..."
# Keep README.md and manifest.csv in the tarball too so the archive is
# self-contained; git just happens to track those same files separately.
tar -czf "$TARBALL" "$RUN"
SIZE=$(du -h "$TARBALL" | cut -f1)
echo "[push] tarball size: $SIZE"

if gh release view "$TAG" >/dev/null 2>&1; then
    echo "[push] release $TAG exists, uploading (clobber) ..."
    gh release upload "$TAG" "$TARBALL" --clobber
else
    echo "[push] creating release $TAG ..."
    gh release create "$TAG" "$TARBALL" \
        --title "Dataset $RUN" \
        --notes "Raw GoPro + IMU tarball for run $RUN. Extract with scripts/datasets/pull.sh."
fi

echo "[push] done. Asset URL:"
gh release view "$TAG" --json assets \
    --jq ".assets[] | select(.name==\"$ASSET_NAME\") | .url"
