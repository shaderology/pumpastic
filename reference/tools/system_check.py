"""
Pumpastic — whole-system connectivity check (read-only)
=======================================================
Tests reachability and functionality of all system components. Never switches the plug.

Usage:
    python tools/system_check.py

Hosts come from the environment (defaults are the example addresses in docs/):
    PUMPASTIC_PI_HOST=192.168.1.10     PUMPASTIC_PI_USER=pi
    PUMPASTIC_PLUG_HOST=192.168.1.20   PUMPASTIC_SSH_KEY=~/.ssh/pumpastic

Requires:
    pip install tapo paho-mqtt pyserial   (pyserial optional)

Environment variables (for Tapo P110M; KASA_* names are also accepted):
    TAPO_USERNAME=you@example.com
    TAPO_PASSWORD=<your_password>

Note: Uses the `tapo` library (NOT python-kasa). The P110M firmware uses
TPAP encryption which python-kasa does not support. Third-Party Services
must be enabled in the Tapo app (Me > Third-Party Services).
"""

import subprocess
import sys
import os
import asyncio
import time
from datetime import datetime


# ── Configuration ──────────────────────────────────────

PI_HOST = os.environ.get("PUMPASTIC_PI_HOST", "192.168.1.10")
PI_USER = os.environ.get("PUMPASTIC_PI_USER", "pi")
SSH_KEY = os.path.expanduser(os.environ.get("PUMPASTIC_SSH_KEY", "~/.ssh/pumpastic"))

DEVICES = {
    "Raspberry Pi 3":  PI_HOST,
    "Tapo P110M":      os.environ.get("PUMPASTIC_PLUG_HOST", "192.168.1.20"),
}

MQTT_BROKER = PI_HOST
MQTT_PORT = 1883

RESULTS = {}


# ── Helpers ────────────────────────────────────────────

def log(category: str, message: str, status: str = "INFO"):
    icons = {"OK": "✓", "FAIL": "✗", "SKIP": "⊘", "INFO": "·", "WARN": "⚠"}
    icon = icons.get(status, "·")
    print(f"  {icon} [{category}] {message}")


def ping(host: str, count: int = 3) -> bool:
    """Ping a host. Works on Windows and Linux."""
    param = "-n" if sys.platform == "win32" else "-c"
    try:
        result = subprocess.run(
            ["ping", param, str(count), "-w", "3000" if sys.platform == "win32" else "3", host],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except Exception as e:
        log("PING", f"Error pinging {host}: {e}", "FAIL")
        return False


# ── Test: Network Reachability ─────────────────────────

def test_network():
    print("\n── Level 1: Network Reachability ──")
    all_ok = True
    for name, ip in DEVICES.items():
        ok = ping(ip)
        status = "OK" if ok else "FAIL"
        log("PING", f"{name} ({ip}): {status}", status)
        RESULTS[f"ping_{name}"] = status
        if not ok:
            all_ok = False
    return all_ok


# ── Test: MQTT Broker ─────────────────────────────────

def test_mqtt() -> bool:
    print("\n── Level 2a: MQTT Broker ──")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log("MQTT", "paho-mqtt not installed. Run: pip install paho-mqtt", "SKIP")
        RESULTS["mqtt"] = "SKIP"
        return False

    connected_flag = {"value": False}
    received_flag = {"value": False}
    test_topic = "pumpastic/connectivity_test"
    test_payload = f"test_{int(time.time())}"

    def on_connect(client, userdata, flags, rc, properties=None):
        connected_flag["value"] = (rc == 0)
        if rc == 0:
            client.subscribe(test_topic, qos=0)

    def on_message(client, userdata, msg):
        if msg.payload.decode() == test_payload:
            received_flag["value"] = True

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pumpastic-test")
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=5)
        client.loop_start()

        # Wait for connection
        for _ in range(20):
            if connected_flag["value"]:
                break
            time.sleep(0.25)

        if not connected_flag["value"]:
            log("MQTT", f"Could not connect to broker at {MQTT_BROKER}:{MQTT_PORT}", "FAIL")
            RESULTS["mqtt"] = "FAIL"
            client.loop_stop()
            return False

        log("MQTT", f"Connected to broker at {MQTT_BROKER}:{MQTT_PORT}", "OK")

        # Publish and check round-trip
        client.publish(test_topic, test_payload, qos=0)
        for _ in range(20):
            if received_flag["value"]:
                break
            time.sleep(0.25)

        if received_flag["value"]:
            log("MQTT", "Publish/subscribe round-trip: OK", "OK")
            RESULTS["mqtt"] = "OK"
        else:
            log("MQTT", "Published but did not receive message back", "WARN")
            RESULTS["mqtt"] = "PARTIAL"

        # Clean up test message
        client.publish(test_topic, "", retain=True)  # Clear retained
        client.loop_stop()
        client.disconnect()
        return True

    except Exception as e:
        log("MQTT", f"Error: {e}", "FAIL")
        RESULTS["mqtt"] = "FAIL"
        return False


# ── Test: Tapo P110M Smart Plug ───────────────────────

async def test_kasa() -> bool:
    print("\n── Level 2b: Tapo P110M Smart Plug ──")
    try:
        from tapo import ApiClient
    except ImportError:
        log("TAPO", "tapo not installed. Run: pip install tapo", "SKIP")
        RESULTS["kasa"] = "SKIP"
        return False

    username = os.environ.get("TAPO_USERNAME") or os.environ.get("KASA_USERNAME")
    password = os.environ.get("TAPO_PASSWORD") or os.environ.get("KASA_PASSWORD")

    if not username or not password:
        log("TAPO", "TAPO_USERNAME and TAPO_PASSWORD env vars not set", "FAIL")
        log("TAPO", "  Set with: $env:TAPO_USERNAME='...' (PowerShell)", "INFO")
        log("TAPO", "  Or:       export TAPO_USERNAME='...' (bash)", "INFO")
        RESULTS["kasa"] = "FAIL"
        return False

    tapo_ip = DEVICES["Tapo P110M"]

    try:
        client = ApiClient(username, password)
        device = await client.p110(tapo_ip)
        info = await device.get_device_info()

        log("TAPO", f"Connected to: {info.nickname} ({info.model})", "OK")
        log("TAPO", f"State: {'ON' if info.device_on else 'OFF'}", "INFO")

        energy = await device.get_current_power()
        power_w = (energy.current_power or 0) / 1000.0
        log("TAPO", f"Power: {power_w:.1f}W", "INFO")

        RESULTS["kasa"] = "OK"
        RESULTS["kasa_state"] = "ON" if info.device_on else "OFF"
        return True

    except Exception as e:
        log("TAPO", f"Failed to connect: {e}", "FAIL")
        RESULTS["kasa"] = "FAIL"
        return False


# ── Test: Controller API (dashboard) ───────────────────

def _get_json(path: str):
    import json
    import urllib.request
    with urllib.request.urlopen(f"http://{DEVICES['Raspberry Pi 3']}:8080{path}", timeout=8) as r:
        return json.load(r)


def test_controller_api() -> bool:
    """Everything the Pi aggregates: pump state, Tank A (USB bridge), weather, solar, e-ink."""
    print(f"\n── Level 2c: Controller API (http://{PI_HOST}:8080) ──")
    try:
        st = _get_json("/api/status")
    except Exception as e:
        log("API", f"Dashboard unreachable: {e}", "FAIL")
        RESULTS["controller_api"] = "FAIL"
        return False
    log("API", f"Pump state {st['state']}, plug {'ON' if st['pump_on'] else 'OFF'}"
        + (f" — FAULT: {st['fault_reason']}" if st.get("fault_reason") else ""),
        "WARN" if st["state"] == "FAULT" else "OK")
    RESULTS["controller_api"] = "OK"

    for key, name in (("tank_a", "Tank A"), ("tank_b", "Tank B")):
        t = st.get(key) or {}
        age = t.get("age_s")
        if t.get("level_pct") is None:
            log("TANK", f"{name}: no data", "FAIL")
            RESULTS[f"{key}_reading"] = "FAIL"
        else:
            fresh = age is not None and age < 120
            log("TANK", f"{name}: {t['level_pct']:.1f}%, seen {age:.0f}s ago", "OK" if fresh else "FAIL")
            RESULTS[f"{key}_reading"] = "OK" if fresh else "FAIL"

    # Informational feeds: a problem here is a warning, never a failure — they don't
    # affect pumping.
    for path, name, ok_fn, desc_fn in (
        ("/api/weather", "weather", lambda d: d.get("available") and not d.get("stale"),
         lambda d: f"{d.get('location')}, fetched {d.get('fetched_at')}"),
        ("/api/solar", "solar", lambda d: d.get("available") and not d.get("stale"),
         lambda d: f"{d.get('power_kw')} kW now, {d.get('today_kwh')} kWh today" if d.get("available") else d.get("error")),
        ("/api/display", "eink_display", lambda d: d.get("last_seen") and not d.get("overdue"),
         lambda d: f"last check-in {d.get('last_seen')}, battery {(d.get('telemetry') or {}).get('battery_pct')}%"),
    ):
        try:
            d = _get_json(path)
            ok = bool(ok_fn(d))
            log(name.upper(), desc_fn(d), "OK" if ok else "WARN")
            RESULTS[name] = "OK" if ok else "WARN"
        except Exception as e:
            log(name.upper(), f"Error: {e}", "WARN")
            RESULTS[name] = "WARN"
    return True


def list_serial_ports() -> None:
    """Informational only. Tank A is on the Pi's USB, so nothing is expected here."""
    try:
        import serial.tools.list_ports
        ports = [f"{p.device} ({p.description})" for p in serial.tools.list_ports.comports()]
        log("SERIAL", f"Local ports: {ports or 'none'} — an ESP32 appears only while plugged in (and awake)", "INFO")
    except ImportError:
        pass


# ── Test: SSH to Raspberry Pi ─────────────────────────

def test_ssh() -> bool:
    print("\n── Level 2d: SSH to Raspberry Pi ──")

    # Check if ssh is available
    key_path = SSH_KEY
    try:
        result = subprocess.run(
            ["ssh", "-i", key_path, "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
             f"{PI_USER}@{DEVICES['Raspberry Pi 3']}", "echo SSH_OK"],
            capture_output=True, text=True, timeout=10
        )
        if "SSH_OK" in result.stdout:
            log("SSH", f"Connected to RPi at {DEVICES['Raspberry Pi 3']}", "OK")
            RESULTS["ssh"] = "OK"
            return True
        else:
            log("SSH", "Key auth failed — check PUMPASTIC_SSH_KEY / PUMPASTIC_PI_USER", "FAIL")
            RESULTS["ssh"] = "FAIL"
            return False
    except FileNotFoundError:
        log("SSH", "ssh command not found — install OpenSSH client", "SKIP")
        RESULTS["ssh"] = "SKIP"
        return False
    except subprocess.TimeoutExpired:
        log("SSH", f"SSH connection timed out to {DEVICES['Raspberry Pi 3']}", "FAIL")
        RESULTS["ssh"] = "FAIL"
        return False
    except Exception as e:
        log("SSH", f"Error: {e}", "FAIL")
        RESULTS["ssh"] = "FAIL"
        return False


# ── Summary ────────────────────────────────────────────

def print_summary():
    print("\n" + "=" * 56)
    print("  PUMPASTIC CONNECTIVITY SUMMARY")
    print("=" * 56)

    all_ok = True
    for test_name, status in RESULTS.items():
        if status in ("FAIL",):
            all_ok = False
        icon = {"OK": "✓", "FAIL": "✗", "SKIP": "⊘", "PARTIAL": "~", "WARN": "⚠"}.get(status, "?")
        print(f"  {icon}  {test_name}: {status}")

    print("=" * 56)
    if all_ok:
        print("  Result: ALL TESTS PASSED")
    else:
        print("  Result: SOME FAILURES — review output above")
    print("=" * 56)


# ── Main ───────────────────────────────────────────────

async def main():
    print("=" * 56)
    print(f"  Pumpastic Device Discovery")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 56)

    test_network()
    test_mqtt()
    await test_kasa()
    test_controller_api()
    list_serial_ports()
    test_ssh()

    print_summary()


if __name__ == "__main__":
    asyncio.run(main())
