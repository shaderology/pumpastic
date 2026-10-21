"""
Tests for solar.py against a fake Modbus TCP server on localhost. No real inverter,
broker or plug involved, so these are safe to run anywhere.
"""
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import solar  # noqa: E402
from solar import ModbusReader, SolarService, _decode  # noqa: E402


def _u32(v):
    v &= 0xFFFFFFFF
    return [v >> 16, v & 0xFFFF]


# Values seen on the real inverter on 2026-10-03, plus a negative grid flow (import).
REGS = {
    30000: [ord("I") << 8 | ord("S")] + [0] * 14,
    32064: _u32(651), 32080: _u32(633), 32089: [0x0402],
    32106: _u32(30 * 65536 + 2461), 32114: _u32(153),
    37113: _u32(-1250), 37119: _u32(812345), 37121: _u32(456789),
}


class FakeDongle:
    """One client at a time, like the real SDongle. Counts connections accepted."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.connections = 0
        self.function_codes = set()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            with c:
                while True:
                    hdr = c.recv(12)
                    if len(hdr) < 12:
                        break
                    tid, _, _, unit, fc, reg, count = struct.unpack(">HHHBBHH", hdr)
                    self.function_codes.add(fc)
                    regs = REGS.get(reg, [0] * count)[:count]
                    body = struct.pack(">BBB", unit, fc, 2 * count) + b"".join(struct.pack(">H", r) for r in regs)
                    c.sendall(struct.pack(">HHH", tid, 0, len(body)) + body)

    def close(self):
        self.srv.close()


@pytest.fixture
def dongle(monkeypatch):
    monkeypatch.setattr(solar, "_CONNECT_SETTLE_S", 0)
    d = FakeDongle()
    yield d
    d.close()


def test_decode():
    assert _decode(_u32(-1250), "i32") == -1250
    assert _decode(_u32(153), "u32/100") == 1.53
    assert _decode([0x0402], "u16") == 0x0402


def test_poll_builds_snapshot_on_one_connection(dongle, monkeypatch):
    monkeypatch.setattr(solar.time, "sleep", lambda s: None)
    published = []
    svc = SolarService({"solar": {"host": "127.0.0.1", "port": dongle.port}},
                       publish=lambda t, p: published.append(t))
    svc._poll()
    svc._poll()
    s = svc.get()
    assert s["available"] and not s["stale"]
    assert s["power_kw"] == 0.633 and s["today_kwh"] == 1.53
    assert s["total_kwh"] == pytest.approx(19685.41)
    assert s["grid_kw"] == -1.25                      # importing
    assert s["load_kw"] == pytest.approx(1.883)       # 0.633 generated + 1.25 imported
    # The dongle allows one client: everything must reuse a single connection.
    assert dongle.connections == 1
    assert dongle.function_codes == {0x03}            # read-only, never a write
    assert published == ["home/solar/state", "home/solar/state"]
    assert svc.display_data() == {"power_kw": 0.633, "today_kwh": 1.53, "grid_kw": -1.25}


def test_unreachable_dongle_keeps_service_alive():
    svc = SolarService({"solar": {"host": "127.0.0.1", "port": 1}})
    svc._poll()
    s = svc.get()
    assert not s["available"] and s["error"]
    assert svc.display_data() is None  # e-ink shows the placeholder instead of zeros


def test_reader_reconnects_after_drop(dongle):
    r = ModbusReader("127.0.0.1", dongle.port, 1)
    assert r.read(32080, 2) == _u32(633)
    r._sock.close()        # simulate the dongle dropping us
    r._sock = socket.socket()  # a dead socket: next read must fail, then recover
    with pytest.raises(OSError):
        r.read(32080, 2)
    assert r.read(32080, 2) == _u32(633)
