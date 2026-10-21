# 09 — Build plan

A phased plan for the builder and their agent. Each phase ends with acceptance checks;
don't start the next until they pass. **Phases marked ⚡ involve mains power or moving
water: the builder does or approves every step.**

## Phase 0 — Site profile

- Create `CLAUDE.local.md` from `CLAUDE.local.md.example`.
- Measure both tanks: diameter, sensor height above the floor, overflow height
  (`docs/07`). Note the lift and the pipe run between them.
- Choose the pump, plug or relay, and where the Pi lives (near a tank lets a node use USB).
- Reserve DHCP addresses for the Pi and the plug.

✅ Site profile filled in; a percent ↔ metres table for each tank; thresholds chosen.

## Phase 1 — Sensor on the bench

- Wire one ESP32-S3 + JSN-SR04T (`docs/02`). Copy `secrets.h.example` → `secrets.h`.
- Set `config.h`: pins, `SENSOR_ID`, tank dimensions; start with `SERIAL_BRIDGE_MODE`
  undefined (WiFi) or defined (USB), as planned.
- Build, flash, open the serial monitor.
- Measure the dead zone with a board; set `SENSOR_MIN_CM` (`docs/07`).

✅ Stable readings (±1 cm) against a tape at 3 distances; the dead zone is measured; the
LED shows green on each valid reading.

## Phase 2 — Pi, broker and controller (no pump)

- Install Mosquitto, Python packages, the controller (rpi-controller skill).
- Create `config.yaml` from the template with your geometry and thresholds; set
  `simulated_pct` for any tank without a node yet.
- Create `/etc/pumpastic.env`; install and start the services.
- Run the hardware-free tests on the Pi.

✅ All tests pass on the Pi; the dashboard shows both tanks; `mosquitto_sub -t 'rainwater/#' -v`
shows readings every 30 s; unplugging the node causes a FAULT within ~2 min.

## Phase 3 — Smart plug, with a lamp ⚡

- Pair the plug, enable its local API (Tapo: Third-Party Services), reserve its address.
- Plug in a **lamp**, not the pump.
- `python reference/tools/plug_check.py` (read-only), then use the dashboard's Simulate
  controls to drive a full cycle: start (after 3 readings), min run, stop, cooldown.
- Unplug the plug while "pumping": the controller must FAULT.

✅ The lamp follows every state transition; every fault path ends with the lamp off; an
alert arrives on your phone.

## Phase 4 — Sensors in the tanks

- Mount each sensor (`docs/02`): clear of the dead zone, beam clear, ideally in a stilling
  tube. Seal the cable entry.
- Compare the reading with a tape. Trim `height_cm` if needed.
- Log readings with the pump off for a day (noise floor, rain response).

✅ Readings agree with the tape within ~2 cm; still-surface noise is known; levels rise
when it rains.

## Phase 5 — The pump ⚡

- Electrician-approved wiring; float switch in series with the pump supply.
- Swap the lamp for the pump. Keep Force OFF open on your phone.
- Run `reference/tools/flow_test.py` and watch the whole run.
- Set `max_run_seconds` from the measured flow (`docs/05`). Enable dry-run detection at
  ~30% of measured running watts.
- Watch one full automatic cycle from start to stop.

✅ Measured flow and power recorded; the level falls smoothly to the stop threshold (if it
goes flat, fix the mounting first); the cap is sized; the float switch cuts the pump when
lifted by hand.

## Phase 6 — Unattended operation

- Persistent journal; `systemctl enable` both services; reboot the Pi and confirm it comes
  back up by itself.
- Walk through every failure in `docs/05` that you can simulate safely.
- Commit your config template and notes (not secrets); review `docs/06` before any push.

✅ A week of automatic operation with no surprises in the logs.

## Phase 7 — Extras (optional, any order)

If you're starting fresh, consider building in the upgrades from `docs/10-roadmap.md`
(float switch from day one, a DIN relay instead of a plug, an isolated network, a
hydrostatic sensor) rather than retrofitting them.

- Weather: set your coordinates and time zone.
- Solar: enable Modbus TCP on the inverter (installer), then `solar:` in config.
- E-ink display: back up the factory flash, flash `reference/firmware/eink-display`, set
  `SERVER_BASE`.
- Ideas: start pumping preferentially while exporting solar (as a gate only; safety rules
  stay authoritative), rain-aware pre-emptive pumping, a drawdown check.
