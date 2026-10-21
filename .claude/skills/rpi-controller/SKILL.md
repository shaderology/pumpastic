---
name: rpi-controller
description: Use when developing, deploying, or debugging the Raspberry Pi Python controller — the pump state machine, MQTT, Mosquitto, systemd services, the Flask dashboard, config.yaml, the USB serial bridge, or any code under reference/controller/. Also for SSH to the Pi, package installs and controller troubleshooting.
---

# Raspberry Pi controller

Host, user and deploy path come from `CLAUDE.local.md`. Examples below use
`pi@192.168.1.10` and `/home/pi/pumpastic/controller`.

## First-time setup

```bash
# on the Pi
sudo apt update && sudo apt install -y mosquitto mosquitto-clients python3-pip python3-venv
sudo tee /etc/mosquitto/conf.d/pumpastic.conf <<'EOF'
listener 1883
allow_anonymous true
EOF
sudo systemctl enable --now mosquitto

# copy reference/controller to /home/pi/pumpastic/controller (scp/rsync), then:
cd ~/pumpastic/controller
pip3 install -r requirements.txt --break-system-packages     # or use a venv
cp config/config.yaml.example config/config.yaml              # fill in with the builder

# plug credentials: root-only, read by systemd
sudo install -m 600 -o root -g root systemd/pumpastic.env.example /etc/pumpastic.env
sudoedit /etc/pumpastic.env

sudo cp systemd/pumpastic.service /etc/systemd/system/
sudo cp systemd/pumpastic-tank-a-bridge.service /etc/systemd/system/   # only for a USB node
sudo systemctl daemon-reload && sudo systemctl enable --now pumpastic
```

Edit `User=` and `WorkingDirectory=` in the units if your user or path differs.
Anonymous MQTT is fine only on a trusted LAN (`docs/06-security.md`).

## Architecture

`src/controller.py` runs one asyncio process: the state machine (every 5 s), heartbeat,
plug power poll, device poll, simulated sensors (`simulated_pct`), snapshot publish (every
15 s to `rainwater/pump/state`) and level history; plus Flask on :8080, weather, solar and
the e-ink renderer. `src/serial_bridge.py` is a separate service for a USB-connected node.

| Module | Role |
|---|---|
| `pump_control.py` | state machine; the **only** code that switches the pump |
| `plug_control.py` | `PlugControl` for the Tapo P110M; retries, never raises |
| `mqtt_client.py` | subscriptions, LWT, snapshot publishing |
| `alerting.py` | ntfy; disabled when `ntfy_topic` is empty |
| `weather.py`, `solar.py`, `eink_display.py` | informational; read the snapshot only |
| `web_dashboard.py`, `web/index.html` | REST API and the static dashboard |

State machine semantics: `docs/05-safety.md`. Topics and payloads: `docs/04-protocols.md`.

## Deploying a change

```bash
cd reference/controller && pytest -q                         # workstation first
scp src/<file>.py pi@192.168.1.10:~/pumpastic/controller/src/
ssh pi@192.168.1.10 'cd ~/pumpastic/controller && python3 -m pytest -q \
  && curl -s localhost:8080/api/status | head -c 120'        # check "pump_on": false
ssh pi@192.168.1.10 'sudo systemctl restart pumpastic && journalctl -u pumpastic -n 20 --no-pager'
```

The test suite in `reference/controller/tests` touches no hardware. Never add tests
that publish to the live broker or switch the plug.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| exits with `TAPO_USERNAME / TAPO_PASSWORD ... not set` | env file missing | create `/etc/pumpastic.env` |
| MQTT refused from other hosts | broker on localhost only | `listener 1883` |
| plug auth error | wrong account / Third-Party Services off | see smart-plug skill |
| pump never starts | below threshold, cooldown, or fewer than N agreeing readings | `journalctl -u pumpastic` shows "Start condition met on reading x/3" |
| FAULT "Tank A silent" | node or bridge down, or every reading rejected | `journalctl -u pumpastic-tank-a-bridge`; read the raw serial (esp32-firmware skill) |
| every cycle ends in a max-runtime FAULT | stop threshold never reached: sensor lost the surface | `docs/08` incident 4; fix the mounting |
| logs gone after a reboot | volatile journal | install `systemd/journald-persistent.conf` |
