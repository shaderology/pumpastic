#!/usr/bin/env python3
"""
Read-only probe for local Modbus access to a Huawei SUN2000-family inverter.

Only ever sends Modbus function 0x03 (read holding registers). It never writes and
never sends credentials, so it is safe to run against the live inverter.
No dependencies beyond the standard library.

Usage:
  python tools/solar_probe.py                      # defaults: dongle 192.168.1.30, ports 502 + 6606
  python tools/solar_probe.py 192.168.200.1 6607   # laptop joined to the Inverter-<SN> hotspot
  python tools/solar_probe.py 192.168.200.1 502 6606 6607

For each port: is it open, does it answer a Modbus read, and for which unit ID.
If one answers, it prints model, AC power, daily yield and meter power.
See docs/04-protocols.md.
"""
import socket
import struct
import sys
import time

UNITS = (1, 0, 100, 16)  # 1 = inverter; 0 = broadcast/dongle default; 100 = dongle itself


class Conn:
    """One persistent connection. The dongle allows a single client and is slow to free a
    connection, so opening a fresh connection per read makes every read after the first
    time out (observed 2026-10-03). Reuse one connection for all reads."""

    def __init__(self, host: str, port: int):
        self.s = socket.create_connection((host, port), 5)
        self.s.settimeout(15)
        self.tid = 0
        time.sleep(1.5)  # the dongle wants a moment after connecting

    def read(self, unit: int, reg: int, count: int):
        """One FC3 read. Returns (registers | None, note)."""
        self.tid += 1
        t0 = time.time()
        try:
            self.s.sendall(struct.pack(">HHHBBHH", self.tid, 0, 6, unit, 0x03, reg, count))
            buf = b""
            while len(buf) < 9 or len(buf) < 6 + struct.unpack(">H", buf[4:6])[0]:
                chunk = self.s.recv(1024)
                if not chunk:
                    return None, f"closed without reply after {time.time() - t0:.1f}s"
                buf += chunk
        except socket.timeout:
            return None, "timeout after 15s"
        except OSError as e:
            return None, f"{e.__class__.__name__} after {time.time() - t0:.1f}s"
        if buf[7] & 0x80:
            return None, f"Modbus exception code {buf[8]} (it speaks Modbus, but refused this read)"
        data = buf[9:9 + buf[8]]
        return [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data) - 1, 2)], "ok"

    def close(self):
        self.s.close()


def as_str(regs):
    return b"".join(struct.pack(">H", r) for r in regs).split(b"\0")[0].decode(errors="replace")


def as_i32(regs):
    return struct.unpack(">i", struct.pack(">HH", regs[0], regs[1]))[0]


def as_u32(regs):
    return (regs[0] << 16) | regs[1]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.30"
    ports = [int(p) for p in sys.argv[2:]] or [502, 6606]
    for port in ports:
        print(f"\n== {host}:{port}")
        for unit in UNITS:
            try:
                c = Conn(host, port)
            except OSError as e:
                # Refused/timeout. The dongle also refuses while another client holds its slot.
                print(f"   closed or busy ({e.__class__.__name__})")
                break
            regs, note = c.read(unit, 30000, 15)
            print(f"   unit {unit:3d}: {note}" + (f" — model '{as_str(regs)}'" if regs else ""))
            if regs:
                for name, reg, n, fmt in (("AC active power", 32080, 2, lambda r: f"{as_i32(r)} W"),
                                          ("Daily yield", 32114, 2, lambda r: f"{as_u32(r) / 100:.2f} kWh"),
                                          ("Meter power (+export)", 37113, 2, lambda r: f"{as_i32(r)} W")):
                    v, n2 = c.read(unit, reg, n)
                    print(f"      {name:22s} {fmt(v) if v else n2}")
                c.close()
                print(f"\n   ✓ Local data available: {host}:{port} unit {unit}")
                return 0
            c.close()
            time.sleep(2)  # let the dongle free its single client slot
    print("\nNo port answered a Modbus read.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
