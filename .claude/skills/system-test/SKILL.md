---
name: system-test
description: Use when testing or diagnosing the system — connectivity checks, device discovery, "is everything up", MQTT, plug or sensor tests, running the test suite, or validating after setup or network changes.
---

# System test and diagnostics

## ⚠ Some tests move real water

Once the pump is plumbed in, anything that switches the plug, or publishes a high Tank A
level to the live broker, can start it. Ask the builder before any of the right-hand column.

| Safe (read-only) | Actuates hardware |
|---|---|
| `reference/tools/system_check.py` | `plug_check.py --toggle` |
| `reference/tools/plug_check.py` (no flag) | publishing to `rainwater/tank_*/level/state` |
| `reference/tools/mqtt_check.py` (test topic only) | dashboard Controls › Simulate sensor |
| `cd reference/controller && pytest` | `reference/tools/flow_test.py` (runs the pump on purpose) |
| dashboard and `/api/*` GETs | |

Escape hatch, always at hand:

```bash
curl -s -X POST http://192.168.1.10:8080/api/control -H 'Content-Type: application/json' \
     -d '{"action":"force_pump_off"}'
```

## Levels

```
1  network      ping the Pi and the plug
2  services     SSH, Mosquitto, controller API, plug authentication
3  devices      readings arriving and fresh, plug state and power readable
4  logic        the hardware-free pytest suite
```

## Whole-system check (levels 1–3)

```bash
export PUMPASTIC_PI_HOST=192.168.1.10 PUMPASTIC_PLUG_HOST=192.168.1.20 PUMPASTIC_PI_USER=pi
export TAPO_USERNAME=... TAPO_PASSWORD=...
python reference/tools/system_check.py
```

Checks ping, MQTT round-trip, plug authentication/state/power, the controller API, the age
of each tank reading, weather, solar, the e-ink check-in, SSH and local serial ports.

## Individual checks

```bash
ssh pi@192.168.1.10 'systemctl is-active mosquitto pumpastic pumpastic-tank-a-bridge; vcgencmd get_throttled'
mosquitto_sub -h 192.168.1.10 -t 'rainwater/#' -v
curl -s http://192.168.1.10:8080/api/status
python reference/tools/mqtt_check.py
python reference/tools/plug_check.py
```

`vcgencmd get_throttled` other than `0x0` means under-voltage or throttling: fix the power
supply before blaming USB or WiFi.

## Logic tests

```bash
cd reference/controller && python -m pytest -q
```

Covers every state transition, start confirmation (including a replay of a real dead-zone
false start), dry-run and stuck-sensor detection, the serial bridge framing, weather
parsing, display scheduling and solar decoding.
