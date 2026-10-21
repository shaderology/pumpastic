# 03 — Software

## Requirements

**Workstation**
- Python 3.11+ (`pip install -r reference/controller/requirements.txt`)
- PlatformIO Core (CLI or the VS Code extension)
- An SSH client and a key pair for the Pi
- Claude Code (optional but assumed by `CLAUDE.md` and the skills)

**Raspberry Pi** (64-bit Raspberry Pi OS or Debian 12+)
- Mosquitto broker and clients
- Python 3.11+ with: `paho-mqtt>=2.0`, `tapo>=0.8`, `flask>=3.0`, `pyyaml`, `requests`,
  `pillow` (e-ink renderer), `pyserial` (USB bridge), `pytest` and `pytest-asyncio`
- `esptool` in a venv, if a node is on the Pi's USB (to flash it in place)
- Time zone set correctly (`timedatectl`): schedules and history use local time
- Persistent journal recommended (`reference/controller/systemd/journald-persistent.conf`)
  so the logs from before a crash or power cut survive the reboot

**Firmware libraries** (PlatformIO pulls them in): PubSubClient, ArduinoJson 7, Adafruit
NeoPixel; XPowersLib for the e-ink board.

## Why these choices

| Choice | Reason |
|---|---|
| MQTT (Mosquitto) | Retained messages and last-will give "latest value" and "node went offline" for free; any tool can subscribe |
| `tapo` Python library | Talks to Tapo plugs locally. `python-kasa` does not support the P110M's newer firmware |
| asyncio controller | One process, several independent loops, no threads to reason about in the safety path |
| Flask + a static HTML page | No build step; the page polls a JSON API |
| Server-rendered e-ink frames | Layout changes need no reflash; the device stays a thin client |

## Layout on the Pi

```
/home/pi/pumpastic/controller/
├── src/              controller.py and modules (see reference/README.md)
├── config/config.yaml        ← your live config, never in git
├── assets/fonts/
├── data/             runtime state: history, weather cache, display telemetry
└── tests/
/etc/pumpastic.env            ← plug credentials, root-only
/etc/systemd/system/pumpastic.service
/etc/systemd/system/pumpastic-tank-a-bridge.service   (only for a USB-connected node)
```

## Services

| Unit | What it runs | Notes |
|---|---|---|
| `mosquitto` | MQTT broker on :1883 | Allow LAN clients: `listener 1883` |
| `pumpastic` | `src/controller.py`: state machine, MQTT, dashboard on :8080, informational loops | `EnvironmentFile=/etc/pumpastic.env` |
| `pumpastic-tank-a-bridge` | `src/serial_bridge.py` | Deliberately *not* tied to `pumpastic`: if the bridge dies, the controller's sensor timeout must be what reacts |

## Configuration

Everything tunable is in `config.yaml` (template:
`reference/controller/config/config.yaml.example`): tank geometry, every pump threshold,
the plug address, alerts, the dashboard port, weather location, the display schedule,
solar. The dashboard's Config tab edits and hot-applies the pump and tank sections.

## Deploying

1. Change code on the workstation; run the hardware-free tests (`cd reference/controller && pytest`).
2. Copy the changed files to `/home/pi/pumpastic/controller/` (scp or rsync).
3. Run the same tests on the Pi.
4. Check `GET /api/status` shows `"pump_on": false` (or accept that a restart stops a run).
5. `sudo systemctl restart pumpastic`, then watch `journalctl -u pumpastic -f`.

The rpi-controller skill has the first-time setup commands.

## Firmware

```bash
cd reference/firmware/tank-sensor
cp include/secrets.h.example include/secrets.h      # then fill in WiFi (and OTA) details
pio run -e esp32s3                                   # build
pio run -e esp32s3 -t upload                         # flash over USB
pio device monitor -b 115200
```

Set `upload_port` / `monitor_port` in `platformio.ini` for your machine, and `SENSOR_ID`,
tank dimensions and addresses in `include/config.h`. For a node on the Pi's USB, build on
the workstation, copy `firmware.bin` to the Pi, stop the bridge, flash with esptool at
0x10000, start the bridge (esp32-firmware skill).
