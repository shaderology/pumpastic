# CLAUDE.md — instructions for the agent building this system

You are helping a builder make an automated rainwater transfer system from this
blueprint. The builder's site details (addresses, tank dimensions, pump, accounts) are in
`CLAUDE.local.md`, which is gitignored. **If it doesn't exist yet, your first job is to
create it from `CLAUDE.local.md.example` with the builder**; don't guess their values.

Work through `docs/09-build-plan.md` phase by phase. Read the doc for each subsystem, and
the matching skill in `.claude/skills/`, before touching it.

## Non-negotiable safety rules

Enforce these in every change, review and suggestion. The numbers are defaults; the
builder may tune them in `config.yaml`, but every rule must keep existing.

1. **Never pump if the source tank (A) is below its floor** (`absolute_min_pct`, default
   20%). A dry run destroys pump seals in minutes.
2. **Never pump if the destination tank (B) is above its ceiling** (`absolute_max_pct`,
   default 95%). Overflow risk.
3. **Maximum runtime** (`max_run_seconds`): force OFF, FAULT and alert if exceeded. Size it
   from the *measured* flow rate (docs/07) so one run can't take Tank A below its floor
   even if the sensor freezes.
4. **Sensor timeout** (`sensor_timeout_seconds`, default 120 s with no reading) → FAULT →
   pump OFF.
5. **Smart plug unreachable → pump OFF.** Unknown state is dangerous state.
6. **Hysteresis** — start high (e.g. 94%), stop well below (e.g. 60%), with minimum run and
   cooldown times. Never allow rapid on/off cycling.
7. **All thresholds live in `config.yaml`.** Never hardcode a safety value.
8. **A start needs several agreeing readings** (`start_confirm_readings`, default 3). No
   single reading may start the pump.

Rationale and failure modes: `docs/05-safety.md`. The incidents behind rules 3, 8 and the
sensor floor: `docs/08-lessons-learned.md`.

## Hazards you must respect

- **Anything that publishes a tank level to the live broker can start the pump.** Never
  inject test readings into production topics, and never run tests that do, without the
  builder's explicit go-ahead *and* a way to stop the pump at hand:
  `curl -X POST http://<pi>:8080/api/control -H 'Content-Type: application/json' -d '{"action":"force_pump_off"}'`
- **Never switch the plug on** to "test" it while a pump is connected, unless the builder
  asks and has confirmed Tank A is well above its floor.
- **Mains wiring is the builder's (or an electrician's) job.** Describe it; don't direct it
  step by step as if it were low-voltage work.
- **The router and network settings belong to the builder.** Query, explain, recommend;
  don't change them.

## Secrets

Never put credentials in tracked files. They go in:

| What | Where (gitignored) | Template |
|---|---|---|
| Controller settings | `controller/config/config.yaml` on the Pi | `config.yaml.example` |
| Smart plug account | `/etc/pumpastic.env` on the Pi (root, mode 600) | `systemd/pumpastic.env.example` |
| WiFi / OTA for each ESP32 | `firmware/*/include/secrets.h` | `secrets.h.example` |
| Notes for you | `CLAUDE.local.md` | `CLAUDE.local.md.example` |

Before any `git push`, scan the tree for credentials, MACs and serial numbers. See
`docs/06-security.md`.

## Conventions

**Firmware (ESP32-S3, PlatformIO, Arduino):** camelCase; all pins and tunables in
`include/config.h`; median of 5 readings 50 ms apart, at least 3 valid; 20 µs trigger pulse
for the JSN-SR04T; never block indefinitely; JSON payloads as in `docs/04-protocols.md`.

**Controller (Python 3.11+):** snake_case, type hints, docstrings; explicit states (IDLE,
MIN_RUN, RUNNING, COOLDOWN, FAULT); `logging` at INFO for operations, DEBUG for sensor
data; every external call (MQTT, plug, network) wrapped in try/except; MQTT QoS 1 for
state and status, QoS 0 for sensor readings.

**Informational features** (weather, solar, display) only *read* the status snapshot.
Their failures must never affect pumping.

**Tests:** write them before deploying any pump-control change. The hardware-free suite
(`cd reference/controller && pytest`) must pass on the workstation *and* on the Pi first.

**Git:** commit working increments; don't push without the builder asking.

## Workflow

- **Start of session:** check the system is up: the dashboard Devices tab, or
  `python reference/tools/system_check.py` (read-only).
- **Deploying controller changes:** copy the changed files to the Pi, run the safe tests
  there, confirm `pump_on` is false in `/api/status`, then restart the service.
- **Hardware debugging:** if a webcam is available, ask the builder to point it at the
  board and use the `webcam-debug` skill to read LED states.
- **Record what you measure.** Flow rates, dead zones and incidents go in the builder's
  docs with dates; the next decision depends on them.
