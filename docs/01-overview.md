# 01 — System overview

## The problem

A small tank (**Tank A**) collects from part of a roof and overflows in every decent
rain, while a much larger tank (**Tank B**) has room to spare. A transfer pump between
them fixes that, but only if someone switches it on at the right time and, more
importantly, off again before Tank A runs dry or Tank B overflows. This system does that
unattended.

The reference site: Tank A 4,820 L, Tank B 35,097 L (two tanks plumbed together), 16 m of
lift over a ~60 m pipe run, and a 600 W transfer pump that moves ~1,540 L/h.

## Components

| Component | Role | Required? |
|---|---|---|
| **Tank sensor node** ×2 | ESP32-S3 + JSN-SR04T ultrasonic sensor on each tank lid. Measures distance to the water, converts it to level %, reports every 30 s | Yes |
| **Raspberry Pi** | Mosquitto MQTT broker, the controller (state machine), the dashboard | Yes |
| **Smart plug** | Switches the pump; reports power draw | Yes (or a relay) |
| **Float switch** | Hardware low-level cut-off wired in series with the pump | Strongly recommended |
| Dashboard | Phone-friendly web UI on the Pi: levels, pump state, history, live config, controls | Included |
| Alerts | Push notifications via ntfy on start, stop and faults | Included |
| Weather | Open-Meteo forecast for context ("rain coming") | Optional |
| Solar | Reads a Huawei SUN2000-family inverter via Modbus TCP | Optional |
| E-ink display | Battery wall display, renders on the Pi, refreshes every 2 h | Optional |

## How data flows

```
sensor node ──(MQTT or USB serial)──▶ rainwater/tank_x/level/state
                                              │
                                   controller (subscribes)
                                              │  every 5 s: evaluate state machine
                                              ▼
                          smart plug ON / OFF ◀── only the state machine does this
                                              │
                         rainwater/pump/state (full snapshot, every 15 s, retained)
                                              │
                    dashboard · e-ink · alerts · anything else (read-only consumers)
```

1. Each node takes 5 echo readings 50 ms apart, keeps the median, converts distance to
   level % and volume, and publishes JSON. A node with a weak WiFi signal can instead
   print the same JSON over USB to the Pi, where a bridge service republishes it.
2. The controller keeps the latest reading per tank and evaluates the state machine every
   5 seconds (`docs/05-safety.md`).
3. When Tank A is high and Tank B has room, held across 3 distinct readings, the
   controller switches the plug on. It switches off at the stop threshold, at the runtime
   cap, or on any fault.
4. A complete status snapshot is published to one retained topic. Every other feature
   reads that, so none of them can interfere with pumping.

## Design principles

- **One writer.** Only the state machine touches the pump.
- **Fail off.** Missing data, an unreachable plug or a crash all end with the pump off.
- **Layered limits.** Thresholds, then confirmation, then a runtime cap, then a hardware
  float switch. Any one layer failing must not be enough to cause damage.
- **Config, not code.** Every threshold is in `config.yaml` and editable from the dashboard.
- **Measure before trusting.** Flow rate, dead zone and noise are measured on site
  (`docs/07-calibration.md`); the defaults are only a starting point.

## Where the reference implementation is

`reference/controller/` (Python), `reference/firmware/tank-sensor/` and
`reference/firmware/eink-display/` (PlatformIO), and `reference/tools/`. See
`reference/README.md` for how to adapt it.
