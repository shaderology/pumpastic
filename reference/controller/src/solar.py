"""
Solar production from a Huawei SUN2000-family inverter, read over Modbus TCP via its
SDongleA-05 (unit 1: the dongle forwards it to the inverter). Modbus TCP has to be
enabled on the inverter first. See docs/04-protocols.md.

Dongle quirks this module works around:
  - It allows ONE client at a time and is slow to free a connection, so we keep a single
    persistent connection and never open one per read (that caused every read after the
    first to time out).
  - It wants a short pause after connecting before the first request.
  - Reads take 0.3–1.2 s each. We poll every `poll_seconds` (default 30).

Read-only: this module only ever sends function 0x03 (read holding registers).
Informational only: nothing here feeds pump control. Failures leave the last values in
place, marked stale.

Config (config.yaml):
  solar:
    host: 192.168.1.30
    port: 502
    unit_id: 1
    poll_seconds: 30
"""
from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger("pumpastic.solar")

_DEFAULTS = {"host": "192.168.1.30", "port": 502, "unit_id": 1, "poll_seconds": 30}
_CONNECT_SETTLE_S = 1.5
_READ_TIMEOUT_S = 15.0
_STALE_AFTER_S = 300

# name → (register, count, decoder). Gains from Huawei's Modbus interface definitions.
_REGISTERS = {
    "pv_input_w":     (32064, 2, "i32"),        # DC input; on hybrids includes battery DC flow
    "active_power_w": (32080, 2, "i32"),        # AC output of the inverter
    "status":         (32089, 1, "u16"),
    "total_kwh":      (32106, 2, "u32/100"),
    "today_kwh":      (32114, 2, "u32/100"),
    "meter_power_w":  (37113, 2, "i32"),        # >0 exporting to grid, <0 importing
    "grid_export_kwh": (37119, 2, "i32/100"),   # lifetime
    "grid_import_kwh": (37121, 2, "i32/100"),   # lifetime
}


def _decode(regs: list[int], kind: str):
    if kind == "u16":
        return regs[0]
    raw = (regs[0] << 16) | regs[1]
    if kind.startswith("i32") and raw >= 0x80000000:
        raw -= 0x100000000
    return raw / 100 if kind.endswith("/100") else raw


class ModbusReader:
    """Minimal Modbus TCP client, FC3 only, one persistent socket."""

    def __init__(self, host: str, port: int, unit: int) -> None:
        self._host, self._port, self._unit = host, port, unit
        self._sock: Optional[socket.socket] = None
        self._tid = 0

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect(self) -> None:
        self.close()
        s = socket.create_connection((self._host, self._port), 10)
        s.settimeout(_READ_TIMEOUT_S)
        time.sleep(_CONNECT_SETTLE_S)
        self._sock = s
        log.info("Connected to inverter Modbus at %s:%d unit %d", self._host, self._port, self._unit)

    def read(self, reg: int, count: int) -> list[int]:
        if self._sock is None:
            self._connect()
        self._tid = (self._tid + 1) & 0xFFFF
        try:
            self._sock.sendall(struct.pack(">HHHBBHH", self._tid, 0, 6, self._unit, 0x03, reg, count))
            buf = b""
            while len(buf) < 6 or len(buf) < 6 + struct.unpack(">H", buf[4:6])[0]:
                chunk = self._sock.recv(1024)
                if not chunk:
                    raise ConnectionError("dongle closed the connection")
                buf += chunk
        except Exception:
            self.close()  # force a clean reconnect next time
            raise
        if struct.unpack(">H", buf[0:2])[0] != self._tid:
            self.close()
            raise ConnectionError("transaction id mismatch (stale reply)")
        if buf[7] & 0x80:
            raise IOError(f"Modbus exception {buf[8]} reading {reg}")
        data = buf[9:9 + buf[8]]
        return [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data) - 1, 2)]


class SolarService:
    """Background poller. `get()` returns the latest snapshot for the dashboard and e-ink."""

    def __init__(self, config: dict, publish: Optional[Callable[[str, str], None]] = None) -> None:
        cfg = {**_DEFAULTS, **(config.get("solar") or {})}
        self._reader = ModbusReader(str(cfg["host"]), int(cfg["port"]), int(cfg["unit_id"]))
        self._interval = max(10, int(cfg["poll_seconds"]))
        self._publish = publish
        self._lock = threading.Lock()
        self._data: Optional[dict] = None
        self._updated: Optional[float] = None
        self._error: Optional[str] = None
        self._failures = 0

    def start(self) -> None:
        threading.Thread(target=self._run, name="solar", daemon=True).start()
        log.info("Solar service started (poll every %d s)", self._interval)

    def get(self) -> dict:
        with self._lock:
            data, updated, err = self._data, self._updated, self._error
        age = time.time() - updated if updated else None
        return {
            "available": data is not None,
            "stale": age is None or age > _STALE_AFTER_S,
            "age_s": round(age) if age is not None else None,
            "updated_at": datetime.fromtimestamp(updated).isoformat(timespec="seconds") if updated else None,
            "error": err,
            **(data or {}),
        }

    def display_data(self) -> Optional[dict]:
        """Shape for eink_display.render(solar=...); None hides live values (placeholder shown)."""
        s = self.get()
        if not s["available"] or s["stale"]:
            return None
        return {"power_kw": s["power_kw"], "today_kwh": s["today_kwh"], "grid_kw": s["grid_kw"]}

    def _run(self) -> None:
        while True:
            self._poll()
            # Back off while the dongle is unreachable; it reboots itself when its cloud link drops.
            wait = self._interval if self._failures == 0 else min(300, self._interval * (1 + self._failures))
            time.sleep(wait)

    def _poll(self) -> None:
        raw: dict = {}
        try:
            for name, (reg, count, kind) in _REGISTERS.items():
                raw[name] = _decode(self._reader.read(reg, count), kind)
                time.sleep(0.2)
        except Exception as exc:
            self._failures += 1
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"[:200]
            # The dongle drops clients often; only shout once it has been failing a while.
            (log.warning if self._failures in (3, 10) or self._failures % 60 == 0 else log.debug)(
                "Solar poll failed (%d in a row): %s", self._failures, exc)
            return

        snapshot = {
            "power_kw": round(raw["active_power_w"] / 1000, 3),
            "pv_input_kw": round(raw["pv_input_w"] / 1000, 3),
            "today_kwh": round(raw["today_kwh"], 2),
            "total_kwh": round(raw["total_kwh"], 2),
            "grid_kw": round(raw["meter_power_w"] / 1000, 3),   # + export, − import
            "grid_export_total_kwh": round(raw["grid_export_kwh"], 2),
            "grid_import_total_kwh": round(raw["grid_import_kwh"], 2),
            "status_code": raw["status"],
        }
        # House load = what the inverter makes minus what goes to the grid.
        snapshot["load_kw"] = round(snapshot["power_kw"] - snapshot["grid_kw"], 3)
        now = time.time()
        if self._failures >= 3:
            log.info("Solar poll recovered after %d failures", self._failures)
        self._failures = 0
        with self._lock:
            self._data, self._updated, self._error = snapshot, now, None
        log.debug("Solar: %s", snapshot)
        if self._publish:
            try:
                self._publish("home/solar/state", json.dumps({**snapshot, "timestamp": int(now)}))
            except Exception as exc:
                log.debug("Solar MQTT publish failed: %s", exc)
