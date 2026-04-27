#!/usr/bin/env bash
# Download a named raw-data run from this repo's GitHub Releases.
#
# Usage:
#   bash scripts/datasets/pull.sh 20260424
#
# Mechanics:
#   - Expects a release tagged `data-<RUN>` (e.g. `data-20260424`) with a
#     single tarball asset `<RUN>.tar.gz` whose top-level directory is `<RUN>/`.
#   - Extracts into data/runs/<RUN>/ (merging with the tracked manifest.csv
#     and README.md).
#
# Requires: `gh` CLI (recommended, handles auth + mirrors) or curl+tar as fallback.

set -euo pipefail

RUN="${1:-}"
if [[ -z "$RUN" ]]; then
    echo "Usage: $0 <run_id>   (e.g. 20260424)" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DST="$REPO_ROOT/data/runs/$RUN"
TAG="data-$RUN"
ASSET="$RUN.tar.gz"

mkdir -p "$DST"
cd "$REPO_ROOT"

echo "[pull] target run     : $RUN"
echo "[pull] release tag    : $TAG"
echo "[pull] asset name     : $ASSET"
echo "[pull] extract to     : $DST"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
TARBALL="$TMP_DIR/$ASSET"

if command -v gh >/dev/null 2>&1; then
    echo "[pull] downloading via gh release download ..."
    gh release download "$TAG" \
        --pattern "$ASSET" \
        --output "$TARBALL" \
        --clobber
else
    # Fall back to anonymous download against the origin remote.
    REMOTE_URL="$(git config --get remote.origin.url)"
    # Normalize to <owner>/<repo>.
    if [[ "$REMOTE_URL" =~ github.com[:/](.+/.+)(\.git)?$ ]]; then
        SLUG="${BASH_REMATCH[1]%.git}"
    else
        echo "[pull] can't parse GitHub slug from '$REMOTE_URL'" >&2
        exit 3
    fi
    URL="https://github.com/$SLUG/releases/download/$TAG/$ASSET"
    echo "[pull] downloading via curl $URL"
    curl -L --fail --retry 3 -o "$TARBALL" "$URL"
fi

echo "[pull] extracting into $DST ..."
# tarball contains a single top-level directory named <RUN>/, so strip it
# and place contents directly under data/runs/<RUN>/.
tar -xzf "$TARBALL" -C "$DST" --strip-components=1

echo "[pull] done."
ls -lh "$DST"
