# Reference implementation

The working code from the installation this blueprint describes, with site details
replaced by example values. Treat it as a starting point: read it, run its tests, then
adapt it to your tanks.

```
reference/
├── controller/                 Python, runs on the Pi
│   ├── src/
│   │   ├── controller.py       entry point: config, logging, wires modules, background loops
│   │   ├── pump_control.py     the state machine; the only code that switches the pump
│   │   ├── plug_control.py     Tapo P110M wrapper (tapo library), retrying, never raising
│   │   ├── mqtt_client.py      subscriptions, LWT, retained snapshot publishing
│   │   ├── serial_bridge.py    USB serial → MQTT for a node without WiFi (own service)
│   │   ├── alerting.py         ntfy push alerts
│   │   ├── level_history.py    hourly levels for the dashboard chart
│   │   ├── weather.py          Open-Meteo forecast, cached (informational)
│   │   ├── solar.py            Huawei SUN2000 via SDongle Modbus TCP (informational)
│   │   ├── eink_display.py     renders 6-colour e-ink frames and the wake schedule
│   │   ├── web_dashboard.py    Flask REST API on :8080
│   │   └── web/index.html      the dashboard (static, mobile-first, light/dark)
│   ├── config/config.yaml.example
│   ├── systemd/                both units, pumpastic.env.example, persistent-journal drop-in
│   ├── assets/fonts/           Atkinson Hyperlegible (SIL OFL) for the e-ink frame
│   └── tests/                  107 hardware-free tests: `cd controller && pytest`
├── firmware/
│   ├── tank-sensor/            ESP32-S3 + JSN-SR04T node: WiFi/MQTT or USB serial mode
│   └── eink-display/           Waveshare PhotoPainter thin client
└── tools/
    ├── system_check.py         read-only check of every component (env: PUMPASTIC_PI_HOST …)
    ├── mqtt_check.py           broker round-trip on a throwaway topic
    ├── plug_check.py           plug state/power; --toggle switches it (⚠ runs the pump)
    ├── flow_test.py            measures flow and power through the real state machine (⚠ runs the pump)
    └── solar_probe.py          read-only Modbus probe for the inverter dongle
```

## Adapting it

| What | Where |
|---|---|
| Tank geometry, thresholds, plug address, alerts, weather, display, solar | `controller/config/config.yaml` (from the example) |
| Plug account | `/etc/pumpastic.env` (from `systemd/pumpastic.env.example`) |
| User and install path on the Pi | `User=` / `WorkingDirectory=` in `systemd/*.service` (examples: `pi`, `/home/pi/pumpastic/controller`) |
| Node identity, pins, tank dimensions, broker address, transport mode | `firmware/tank-sensor/include/config.h` |
| WiFi and OTA passwords | `firmware/*/include/secrets.h` (from `secrets.h.example`) |
| Upload/monitor ports | `firmware/*/platformio.ini` |
| Display server address | `SERVER_BASE` in `firmware/eink-display/include/config.h` |

Example addresses used throughout: Pi `192.168.1.10`, plug `.20`, solar dongle `.30`,
WiFi tank nodes `.40` / `.41`.

Things that are specific to the reference hardware and may need changing:
- **Topic and sensor names** assume two tanks, `tank_a` (source) and `tank_b`
  (destination).
- **A different plug or relay:** replace `plug_control.py` with a class offering the same
  async methods (`connect`, `turn_on`, `turn_off`, `is_online`, `get_power_watts`,
  `get_device_status`). The state machine doesn't care what's behind it.
- **A different inverter:** `solar.py` decodes Huawei SUN2000 registers. Set
  `solar.enabled: false` if you have none.
- **A different display:** `eink_display.py` packs frames for a 7.3" Spectra E6 panel; the
  layout code is reusable, the packing isn't.

## Running the tests

```bash
cd reference/controller
pip install -r requirements.txt
python -m pytest -q
```

They need no broker, plug or sensor. Keep it that way: never add a test that publishes to
a live broker or switches a plug.
