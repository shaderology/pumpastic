# 04 — Protocols and interfaces

## MQTT topics

| Topic | Publisher | Payload | QoS | Retained |
|---|---|---|---|---|
| `rainwater/<tank>/level/state` | sensor node (or bridge) | sensor reading JSON | 0 | yes |
| `rainwater/<tank>/status` | node LWT / bridge | `online` / `offline` | 1 | yes |
| `rainwater/pump/state` | controller, on change and every 15 s | full system snapshot | 1 | yes |
| `rainwater/pump/power` | controller | `{"watts": 603.0}` | 0 | no |
| `rainwater/controller/status` | controller LWT | `online` / `offline` | 1 | yes |
| `rainwater/controller/heartbeat` | controller | `{"ts": "<ISO time>"}` | 0 | no |
| `home/solar/state` | controller (optional) | inverter snapshot | 0 | yes |

`<tank>` is `tank_a` or `tank_b`.

### Sensor reading

```json
{"sensor_id": "tank_a", "distance_cm": 61.1, "level_pct": 93.5, "volume_l": 4505,
 "rssi": -65, "uptime_s": 3898}
```

- `level_pct` is relative to the **overflow outlet** (100% = at the outlet), clamped 0–100.
  See `docs/07-calibration.md`.
- A USB-bridged node sends `"link": "usb"` instead of `rssi`.
- `"overfull": true` is added when water is above the outlet (storm inflow beating the outlet).
- Simulated readings (the controller's `simulated_pct` option) carry `"simulated": true`.

### System snapshot (`rainwater/pump/state`)

This is the one topic a consumer needs.

```json
{"state": "IDLE", "pump_on": false, "fault_reason": "", "state_since": "2026-10-04T09:05:39",
 "runtime_s": null,
 "tank_a": {"level_pct": 65.0, "distance_cm": 109.5, "volume_l": 3132.0, "age_s": 4.9},
 "tank_b": {"level_pct": 50.0, "distance_cm": 135.0, "volume_l": 17548.0, "age_s": 3.0},
 "tank_a_pct": 65.0, "tank_b_pct": 50.0}
```

`state` is one of `IDLE`, `MIN_RUN`, `RUNNING`, `COOLDOWN`, `FAULT`.

### Solar snapshot (`home/solar/state`)

```json
{"power_kw": 4.21, "today_kwh": 15.04, "grid_kw": 1.3, "load_kw": 2.91,
 "grid_export_total_kwh": 1234.5, "timestamp": 1760000000}
```

`grid_kw` is positive when exporting, negative when importing.

## USB serial bridge framing

A node built with `SERIAL_BRIDGE_MODE` prints one line per reading at 115200 baud:

```
#PUMPASTIC {"sensor_id":"tank_a","distance_cm":111.5,"level_pct":63.8,"volume_l":3076,"link":"usb","uptime_s":28}
```

- The `#PUMPASTIC ` prefix separates readings from log output, so the bridge ignores
  everything else.
- The firmware emits each record in **one** write, and the bridge reassembles lines from
  raw chunks rather than using `readline()` with a timeout. Both halves are needed: a
  record split across USB packets was once read truncated and dropped.
- The bridge publishes `offline` after `silence_timeout_seconds` (90 s), deliberately under
  the controller's 120 s sensor timeout.
- Use `/dev/serial/by-id/...` paths, never `/dev/ttyACM0`: the S3's native USB
  re-enumerates on every reset.

## Dashboard REST API (port 8080, no auth: LAN only)

| Method | Path | Returns / does |
|---|---|---|
| GET | `/api/status` | the full status snapshot (what the Overview tab draws) |
| GET | `/api/devices` | connectivity of broker, plug, nodes (cached 15 s) |
| GET | `/api/config` | sanitised config (no secrets) |
| POST | `/api/config` | save and hot-apply pump/tank settings |
| GET | `/api/history?days=30` | hourly level history |
| POST | `/api/control` | `{"action": "force_pump_off"}`, `{"action": "clear_fault"}`, `{"action": "simulate_sensor", "sensor": "tank_a", "level_pct": 50}` |
| GET | `/api/weather`, `/api/solar`, `/api/system`, `/api/display` | informational feeds, Pi health, display schedule |

`simulate_sensor` publishes a fake reading to the live topic. **It can start the pump.**

## E-ink display protocol (HTTP)

```
device wakes
POST /display/telemetry   {"fw":"1.0.0","wake_cause":"timer","rssi":-61,"ip":"…",
                           "failures":0,"awake_ms":31000,"vbus":false,"charging":false,
                           "battery_mv":3900,"battery_pct":64,"temp_c":21.5,"rh":55}
GET  /display/frame.bin   headers: If-None-Match: "<etag>", X-Force: 1 (button wake)
  ← 200 + 192,000-byte frame, ETag, X-Next-Wake-S: 7200
  ← 304 (unchanged), X-Next-Wake-S
  ← 503 on render error, X-Next-Wake-S: 900
device refreshes the panel, deep-sleeps X-Next-Wake-S seconds
```

The frame is the panel's native 4 bits per pixel, even-x pixel in the high nibble, colour
indices black 0, white 1, yellow 2, red 3, blue 5, green 6. `GET /display/frame.png`
previews it without counting as a device check-in. The server owns the clock and the
schedule; the device just sleeps for as long as it's told.

## Smart plug (Tapo P110M, local)

`tapo` library: `ApiClient(user, password).p110(host)`, then `on()`, `off()`,
`get_device_info().device_on`, `get_current_power().current_power` (watts). Requires the
plug's owning account, and *Third-Party Services* enabled in the Tapo app.

## Solar (Modbus TCP, optional)

Huawei SUN2000-family inverter through its SDongleA-05, port 502, unit 1, function 0x03
reads only. Modbus TCP must first be enabled on the inverter by the installer account.

| Register | Words | Type | Meaning |
|---|---|---|---|
| 32064 | 2 | i32 | PV input power, W |
| 32080 | 2 | i32 | inverter AC active power, W |
| 32089 | 1 | u16 | device status |
| 32106 | 2 | u32/100 | total yield, kWh |
| 32114 | 2 | u32/100 | daily yield, kWh |
| 37113 | 2 | i32 | meter power, W (+ export, − import) |
| 37119 / 37121 | 2 | i32/100 | grid exported / imported, lifetime kWh |

The dongle serves **one client at a time** and frees the slot slowly: keep one persistent
connection, pause ~1.5 s after connecting, and poll from one place only.

## Alerts (ntfy)

`POST https://ntfy.sh/<topic>` with headers `Title`, `Priority` (high for faults) and
`Tags`. Topics are public: anyone who knows the name can read and post. Use a long random
name.
