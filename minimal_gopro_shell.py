#!/usr/bin/env python3
"""
Minimal helper to turn on the GoPro Labs USB shell (SHEL=1) and print a line
every time the shutter button is pressed.

Reference material:
  - node_gopro.py for IP discovery and Labs command sending
  - https://gopro.github.io/labs/control/extensions/ (SHEL=1 over USB serial)
  - https://gopro.github.io/labs/control/tech/ (z/shutter_presses variable)
"""

import argparse
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Any
from types import SimpleNamespace
import glob

import requests
import serial  # pyserial
from serial.tools import list_ports

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("gopro-shell")


def discover_gopro_ip():
    """Find the camera on USB ethernet (172.2x.1xx.51 pattern)."""
    cmd = "ip -4 --oneline link | grep -v 'state DOWN' | grep -v LOOPBACK | grep -v 'NO-CARRIER'"
    try:
        output = subprocess.check_output(cmd, shell=True, text=True)
    except Exception:
        return None

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if "inet" not in line:
            try:
                dev = line.split(":")[1].strip()
                ip_out = subprocess.check_output(f"ip -4 addr show dev {dev}", shell=True, text=True)
            except Exception:
                continue
        else:
            ip_out = line
        match = re.search(r"inet (172\.2\d\.1\d\d)\.\d+", ip_out)
        if match:
            subnet = match.group(1)
            return f"{subnet}.51"
    return None


def ip_from_serial(sn: str):
    """Derive camera IP from the last three digits of the serial number."""
    if len(sn) < 3 or not sn[-3:].isdigit():
        raise ValueError("Serial number must end with three digits (e.g. ...789)")
    return f"172.2{sn[-3:-2]}.1{sn[-2:]}.51"


class LabsUSBClient:
    def __init__(self, ip: str):
        self.ip = ip
        self.base_url = f"http://{ip}:8080"
        self.session = requests.Session()

    def check_connection(self):
        self.session.get(f"{self.base_url}/gopro/camera/control/wired_usb?p=1", timeout=2)
        resp = self.session.get(f"{self.base_url}/gopro/camera/info", timeout=2)
        resp.raise_for_status()
        return resp.json()

    def send_labs_command(self, code: str):
        url = f"{self.base_url}/gopro/qrcode"
        params = {"labs": 1, "code": code}
        resp = self.session.get(url, params=params, timeout=5)
        resp.raise_for_status()
        return resp.text

    def enable_shell(self):
        # Doc text: "Using $SHEL=1 enables a command shell for Labs over a USB serial port."
        log.info("Sending Labs command to enable USB shell ($SHEL=1)...")
        self.send_labs_command("$SHEL=1")
        log.info("Labs USB shell command sent.")


def _maybe_gopro_port(port: Any) -> bool:
    """Heuristically detect a GoPro Labs shell port."""
    text = " ".join(
        filter(
            None,
            [
                port.manufacturer or "",
                port.product or "",
                port.description or "",
                port.hwid or "",
            ],
        )
    ).lower()
    return any(tok in text for tok in ["gopro", "hero", "max"])


def _format_port(port: Any) -> str:
    return (
        f"{port.device} - {port.description or 'n/a'} "
        f"(manu={port.manufacturer or '?'}, prod={port.product or '?'}, "
        f"serial={getattr(port, 'serial_number', '') or '?'}, hwid={port.hwid or '?'})"
    )


def _prompt_for_port(ports):
    print("Multiple serial ports found. Select the GoPro Labs shell port:")
    for idx, p in enumerate(ports, start=1):
        print(f"  [{idx}] {_format_port(p)}")
    while True:
        choice = input("Enter number (empty to cancel): ").strip()
        if choice == "":
            return None
        if choice.isdigit():
            val = int(choice)
            if 1 <= val <= len(ports):
                return ports[val - 1].device
        print("Invalid selection, try again.")


def find_serial_device(
    port_hint: Optional[str] = None, available_ports=None, interactive=True, force_prompt: bool = False
):
    if port_hint and Path(port_hint).exists():
        return port_hint

    ports = list(available_ports) if available_ports is not None else list(list_ports.comports())
    port_map = {p.device: p for p in ports}

    # Prefer USB-like devices; also include glob results to catch anything missing from list_ports.
    usb_patterns = [
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
        "/dev/tty.usbmodem*",
        "/dev/tty.usbserial*",
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
    ]
    usb_names = set()
    for pat in usb_patterns:
        for dev in glob.glob(pat):
            usb_names.add(dev)
    for p in ports:
        if re.search(r"(ttyACM|ttyUSB|usbmodem|usbserial)", p.device):
            usb_names.add(p.device)

    usb_candidates = []
    for dev in sorted(usb_names):
        if dev in port_map:
            usb_candidates.append(port_map[dev])
        else:
            usb_candidates.append(SimpleNamespace(device=dev, description="(not in list_ports)", manufacturer=None, product=None, serial_number=None, hwid="n/a"))

    # If no USB-ish devices, fall back to all ports.
    candidates = usb_candidates or ports
    if not candidates:
        return None

    gopro_ports = [p for p in candidates if _maybe_gopro_port(p)]
    if len(gopro_ports) == 1 and not force_prompt:
        p = gopro_ports[0]
        if len(candidates) == 1 and not interactive:
            log.info(f"Auto-selected GoPro-looking port: {p.device} ({p.description})")
            return p.device
        # More than one candidate overall, allow user to override.

    # Build priority list (GoPro-looking first, then others).
    ordered = []
    seen = set()
    for lst in (gopro_ports, candidates):
        for p in lst:
            if p.device not in seen:
                ordered.append(p)
                seen.add(p.device)

    if interactive:
        if len(ordered) == 1 and not force_prompt:
            p = ordered[0]
            log.info(f"Auto-selected serial port: {p.device} ({p.description})")
            return p.device
        selected = _prompt_for_port(ordered)
        if selected:
            log.info(f"Selected serial port: {selected}")
        return selected

    log.info("No clear GoPro serial port found. Pass --serial-port to pick one:")
    for p in ordered:
        log.info(f"  {_format_port(p)}")
    return None


def wait_for_serial_device(port_hint=None, timeout=20, force_prompt=False):
    return wait_for_serial_device_with_baseline(
        port_hint=port_hint, timeout=timeout, baseline=None, force_prompt=force_prompt
    )


def wait_for_serial_device_with_baseline(port_hint=None, timeout=20, baseline=None, force_prompt=False):
    """
    Wait for a serial device, preferring devices that appeared after baseline.
    """
    start = time.time()
    baseline = set(baseline or [])
    last_log = 0
    while time.time() - start < timeout:
        ports = list(list_ports.comports())
        if not ports:
            time.sleep(1.0)
            continue

        new_ports = [p for p in ports if p.device not in baseline]
        if not new_ports and not port_hint:
            now = time.time()
            if now - last_log > 3.0:
                log.info("Waiting for new serial device from camera...")
                last_log = now
            time.sleep(1.0)
            continue

        dev = find_serial_device(
            port_hint, available_ports=new_ports or ports, interactive=True, force_prompt=force_prompt
        )
        if dev:
            return dev
        time.sleep(1.0)
    return None


def query_shutter_presses(serial_port: serial.Serial, wait=1.2, verbose=False):
    """Send $z over the Labs shell and parse the shutter_presses value."""
    serial_port.reset_input_buffer()
    serial_port.write(b"$z\n")
    serial_port.flush()

    deadline = time.time() + wait
    while time.time() < deadline:
        line = serial_port.readline()
        if not line:
            continue
        text = line.decode("ascii", errors="ignore").strip()
        if not text:
            continue

        if verbose:
            log.info(f"[shell] {text}")

        lower = text.lower()
        if lower.startswith("z") or "shutter" in lower or "press" in lower or re.fullmatch(r"-?\d+", text):
            match = re.search(r"(-?\d+)", text)
            if match:
                return int(match.group(1))
    return None


def listen_for_shutter(port_path: str, poll_interval: float, verbose_shell: bool = False):
    with serial.Serial(port_path, 115200, timeout=0.5) as ser:
        count = query_shutter_presses(ser, verbose=verbose_shell)
        if count is None:
            count = 0
        log.info(f"Listening on {port_path} (starting count: {count})")

        try:
            while True:
                latest = query_shutter_presses(ser, verbose=verbose_shell)
                if latest is None:
                    time.sleep(poll_interval)
                    continue
                if latest > count:
                    for _ in range(latest - count):
                        log.info("Shutter button pressed.")
                    count = latest
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            log.info("Stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="Enable GoPro Labs USB shell and print shutter button presses.")
    parser.add_argument("--ip", help="Camera IP (e.g. 172.2X.1YZ.51). If omitted, auto-discovery is used.")
    parser.add_argument("--sn", help="Camera serial number (last three digits used to compute IP).")
    parser.add_argument("--serial-port", help="Serial device path (e.g. /dev/ttyACM0). If omitted, auto-detect.")
    parser.add_argument("--poll", type=float, default=0.4, help="Seconds between polling $z over serial.")
    parser.add_argument("--wait", type=float, default=30.0, help="Seconds to wait for camera serial port to appear.")
    parser.add_argument("--debug-shell", action="store_true", help="Log raw shell lines for troubleshooting.")
    parser.add_argument("--force-prompt", action="store_true", help="Always prompt to pick a serial port if multiple USB candidates exist.")
    args = parser.parse_args()

    ip = args.ip
    if not ip and args.sn:
        try:
            ip = ip_from_serial(args.sn)
        except ValueError as exc:
            parser.error(str(exc))
    if not ip:
        ip = discover_gopro_ip()
    if not ip:
        log.error("Could not determine GoPro IP. Provide --ip or --sn.")
        sys.exit(1)

    client = LabsUSBClient(ip)
    try:
        info = client.check_connection()
        log.info(f"Connected to GoPro at {ip} (serial: {info.get('info', {}).get('serial_number', 'unknown')})")
    except Exception as exc:
        log.error(f"Camera connection failed: {exc}")
        sys.exit(1)

    try:
        client.enable_shell()
    except Exception as exc:
        log.error(f"Failed to send $SHEL=1: {exc}")
        sys.exit(1)

    log.info("Waiting for the camera's USB serial port to appear (115200 8N1)...")
    # Track existing ports before the shell comes up so we can prefer new devices.
    baseline_ports = {p.device for p in list_ports.comports()}
    port_path = wait_for_serial_device_with_baseline(
        args.serial_port, timeout=args.wait, baseline=baseline_ports, force_prompt=args.force_prompt
    )
    if not port_path:
        log.error("No serial device found. Replug USB or pass --serial-port explicitly.")
        sys.exit(1)

    listen_for_shutter(port_path, poll_interval=args.poll, verbose_shell=args.debug_shell)


if __name__ == "__main__":
    main()
