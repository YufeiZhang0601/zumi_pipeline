#!/usr/bin/env bash
# Install zumi_pipeline runtime on Ubuntu 22.04 / 24.04 (ARM64 or x86_64).
# Idempotent: safe to re-run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

log() { printf "\033[1;36m[install]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
die() { printf "\033[1;31m[error]\033[0m %s\n" "$*" >&2; exit 1; }

if [[ "$(uname -s)" != "Linux" ]]; then
  die "This script is for Linux only. On macOS host use the native setup."
fi

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    die "sudo is required but not available. Run as root or install sudo."
  fi
fi

log "1/5  Installing system packages"
$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  curl \
  git \
  exiftool \
  ffmpeg \
  libgl1 \
  libglib2.0-0 \
  libgomp1 \
  libsm6 \
  libxext6 \
  libxrender1 \
  pkg-config \
  python3 \
  python3-dev \
  python3-pip \
  python3-venv \
  usbutils \
  v4l-utils

log "2/5  Creating .venv (Python virtual environment)"
if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

log "3/5  Installing Python dependencies (this takes a while)"
python -m pip install --upgrade pip setuptools wheel
if [[ -f "requirements-docker.txt" ]]; then
  python -m pip install --prefer-binary -r requirements-docker.txt
else
  die "requirements-docker.txt not found. Are you running this from repo root?"
fi
python -m pip install pyzmq

log "4/5  Adding current user to 'dialout' and 'video' groups"
TARGET_USER="${SUDO_USER:-$USER}"
$SUDO usermod -aG dialout "$TARGET_USER" || warn "failed to add $TARGET_USER to dialout"
$SUDO usermod -aG video   "$TARGET_USER" || warn "failed to add $TARGET_USER to video"

log "5/5  Probing attached hardware"
echo "---- serial devices ----"
ls -la /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "(no serial devices currently attached)"
echo "---- video devices ----"
ls -la /dev/video* 2>/dev/null || echo "(no video devices currently attached)"
echo "---- usb devices ----"
lsusb || true
echo "---- network interfaces that look like GoPro USB ----"
ip -4 addr show | grep -E "172\.2[0-9]\.1[0-9]{2}" || echo "(no GoPro subnet detected)"

cat <<'NEXT'

----------------------------------------
Install finished.

Important: log out and log back in (or reboot the VM) so the new
'dialout' and 'video' group membership takes effect.

Then:
  cd ~/zumi_pipeline
  source .venv/bin/activate
  source .env.capture     # if you created this file
  python orchestrator.py
----------------------------------------
NEXT
