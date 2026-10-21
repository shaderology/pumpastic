#!/usr/bin/env python3
"""
Serial-to-MQTT bridge for a USB-connected tank sensor node.

Tank A shares a weatherproof box with the Raspberry Pi on a ~10 cm USB lead. Its WiFi
link to the AP is poor enough to be unusable (-78 dBm, ~72% of frames retried, OTA
uploads never finish), so the node runs with its radio off and prints each reading to
USB serial instead. This service republishes those readings onto MQTT so the controller
and every other subscriber see exactly what they saw before.

The published payload and topics are unchanged, so nothing downstream needs to know a
node is on USB rather than WiFi. Safety rule 4 still applies end to end: if readings
stop arriving the status topic goes "offline", the controller's sensor timeout fires,
and the pump is turned off.

Usage:
    python3 src/serial_bridge.py [--config config/config.yaml] [--once]
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

import paho.mqtt.client as mqtt
import serial
import yaml

log = logging.getLogger("pumpastic.bridge")

# Lines the firmware marks with this prefix are readings; everything else is log noise.
BRIDGE_PREFIX = "#PUMPASTIC "

# The by-id path is stable across re-enumeration; /dev/ttyACM0 is not, because the S3's
# native USB port disappears and comes back on every reset and can land on a new number.
DEFAULT_PORT_GLOB = "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*-if00"

RECONNECT_DELAY_S = 5.0

# Re-assert the retained status this often, even when unchanged. The node's own WiFi
# LWT ("offline", retained) can be delivered by the broker *after* this bridge has
# published "online", because the broker only notices the dead session a keepalive
# later — which leaves the topic retained "offline" while readings are flowing. The
# controller had the identical race; see docs/08-lessons-learned.md.
STATUS_REASSERT_S = 60.0


def parse_reading(line: str) -> Optional[dict[str, Any]]:
    """Return the reading encoded in one serial line, or None if it is not one.

    Kept free of I/O so it can be tested without a serial port or a broker. Anything
    malformed returns None rather than raising: a garbled line on a live sensor feed
    must never take the bridge down.
    """
    if not line:
        return None
    idx = line.find(BRIDGE_PREFIX)
    if idx < 0:
        return None
    payload = line[idx + len(BRIDGE_PREFIX):].strip()
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        log.warning("Ignoring unparseable bridge line: %.120s", payload)
        return None
    if not isinstance(data, dict):
        return None
    # A reading without a level is not usable, and publishing it would let the
    # controller act on a partial record.
    if "level_pct" not in data or "sensor_id" not in data:
        log.warning("Ignoring bridge line missing sensor_id/level_pct: %.120s", payload)
        return None
    return data


class LineAssembler:
    """Reassemble newline-terminated records from arbitrary byte chunks.

    USB delivers whatever happens to be buffered, so one record can arrive split across
    reads and two records can arrive in one. Reading with readline() and a timeout meant
    a split record came back truncated and the reading was dropped; at a 30 s cadence two
    of those in a row would trip the offline timeout and FAULT the pump.
    """

    # A framed reading is ~150 bytes. Anything far past that is a device spewing
    # unterminated output, so drop it rather than grow without bound.
    MAX_BUFFER = 4096

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[str]:
        if not chunk:
            return []
        self._buf.extend(chunk)
        lines: list[str] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            lines.append(raw.decode("utf-8", "replace").strip())
        if len(self._buf) > self.MAX_BUFFER:
            log.warning("Dropping %d bytes of unterminated serial data", len(self._buf))
            self._buf.clear()
        return lines


def resolve_port(configured: Optional[str]) -> Optional[str]:
    """Pick the serial device, preferring an explicit config value."""
    if configured:
        return configured if Path(configured).exists() else None
    matches = sorted(glob.glob(DEFAULT_PORT_GLOB))
    return matches[0] if matches else None


class SerialBridge:
    def __init__(self, config: dict[str, Any]) -> None:
        bridge_cfg = config.get("serial_bridge", {}) or {}
        self._sensor_id: str = bridge_cfg.get("sensor_id", "tank_a")
        self._port_cfg: Optional[str] = bridge_cfg.get("port")
        self._baud: int = int(bridge_cfg.get("baud", 115200))
        # Silence longer than this marks the node offline. Keep it below the
        # controller's sensor_timeout_seconds so the bridge reports first.
        self._silence_timeout_s: float = float(bridge_cfg.get("silence_timeout_seconds", 90))

        mqtt_cfg = config.get("mqtt", {}) or {}
        self._broker: str = mqtt_cfg.get("broker", "localhost")
        self._port: int = int(mqtt_cfg.get("port", 1883))

        tank_cfg = (config.get("tanks", {}) or {}).get(self._sensor_id, {}) or {}
        self._topic_level: str = tank_cfg.get(
            "sensor_topic", f"rainwater/{self._sensor_id}/level/state"
        )
        self._topic_status: str = tank_cfg.get(
            "status_topic", f"rainwater/{self._sensor_id}/status"
        )

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"pumpastic-bridge-{self._sensor_id}",
            clean_session=True,
        )
        # If this process dies the broker marks the sensor offline for us, which is what
        # drives the controller into FAULT rather than leaving a stale reading standing.
        self._client.will_set(self._topic_status, "offline", qos=1, retain=True)

        self._running = True
        self._status: Optional[str] = None
        self._last_reading_at: float = 0.0
        self._last_status_pub: float = 0.0

    # ── MQTT ──────────────────────────────────────────────────────────────────

    def connect_mqtt(self) -> None:
        log.info("Connecting to MQTT broker %s:%d", self._broker, self._port)
        self._client.connect(self._broker, self._port, keepalive=60)
        self._client.loop_start()

    def _publish_status(self, status: str) -> None:
        now = time.monotonic()
        unchanged = status == self._status
        if unchanged and (now - self._last_status_pub) < STATUS_REASSERT_S:
            return
        self._status = status
        self._last_status_pub = now
        self._client.publish(self._topic_status, status, qos=1, retain=True)
        if not unchanged:
            log.info("%s status -> %s", self._sensor_id, status)

    def _publish_reading(self, data: dict[str, Any]) -> None:
        # Retained, matching what the ESP32 published over WiFi, so a subscriber that
        # connects late still sees the most recent level.
        self._client.publish(self._topic_level, json.dumps(data), qos=0, retain=True)
        self._last_reading_at = time.monotonic()
        self._publish_status("online")
        log.info(
            "%s %.1f%% (%.1f cm, %s L)",
            data.get("sensor_id", self._sensor_id),
            float(data.get("level_pct", 0.0)),
            float(data.get("distance_cm", 0.0)),
            data.get("volume_l", "?"),
        )

    # ── Serial ────────────────────────────────────────────────────────────────

    def _check_silence(self) -> None:
        if self._last_reading_at and self._status == "online":
            if time.monotonic() - self._last_reading_at > self._silence_timeout_s:
                log.warning(
                    "No reading for %.0fs — marking %s offline",
                    self._silence_timeout_s, self._sensor_id,
                )
                self._publish_status("offline")

    def _read_forever(self, port: str) -> None:
        log.info("Opening %s at %d baud", port, self._baud)
        assembler = LineAssembler()
        with serial.Serial(port, self._baud, timeout=1) as ser:
            while self._running:
                try:
                    # Read whatever is buffered (blocking up to the timeout for at least
                    # one byte) and let the assembler decide where records end.
                    chunk = ser.read(max(1, ser.in_waiting))
                except serial.SerialException:
                    log.warning("Serial read failed — device likely re-enumerated")
                    return
                for line in assembler.feed(chunk):
                    reading = parse_reading(line)
                    if reading is not None:
                        self._publish_reading(reading)
                self._check_silence()

    def run(self) -> None:
        self.connect_mqtt()
        while self._running:
            port = resolve_port(self._port_cfg)
            if port is None:
                # Unplugged or still enumerating. Say so once the node looks stale.
                self._check_silence()
                if self._status != "offline" and self._last_reading_at:
                    self._publish_status("offline")
                log.warning("Serial device not present; retrying in %.0fs", RECONNECT_DELAY_S)
                time.sleep(RECONNECT_DELAY_S)
                continue
            try:
                self._read_forever(port)
            except (serial.SerialException, OSError) as exc:
                log.warning("Serial error on %s: %s", port, exc)
            if self._running:
                time.sleep(RECONNECT_DELAY_S)

    def stop(self, *_: Any) -> None:
        log.info("Shutting down")
        self._running = False
        try:
            self._publish_status("offline")
            time.sleep(0.3)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description="Serial-to-MQTT bridge for a USB tank node")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--once", action="store_true",
                    help="Print the first reading seen and exit (for checking wiring)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path(__file__).parent.parent / config_path
    with open(config_path) as fh:
        config = yaml.safe_load(fh) or {}

    if args.once:
        port = resolve_port((config.get("serial_bridge", {}) or {}).get("port"))
        if port is None:
            print("No serial device found", file=sys.stderr)
            return 1
        print(f"Reading from {port} …")
        with serial.Serial(port, 115200, timeout=1) as ser:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                reading = parse_reading(ser.readline().decode("utf-8", "replace").strip())
                if reading is not None:
                    print(json.dumps(reading, indent=2))
                    return 0
        print("No reading seen in 60s", file=sys.stderr)
        return 1

    bridge = SerialBridge(config)
    signal.signal(signal.SIGTERM, bridge.stop)
    signal.signal(signal.SIGINT, bridge.stop)
    try:
        bridge.run()
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
