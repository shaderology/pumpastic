# Pumpastic blueprint: build your own automated rainwater transfer system

A starting kit for building a system that **moves water from a small tank that keeps
overflowing to a bigger one, automatically and safely**: ultrasonic level sensors on
ESP32-S3 boards, a Raspberry Pi running the control loop, and a smart plug switching
the pump. Optional extras: a phone-friendly dashboard, a rain forecast, solar monitoring,
and a battery e-ink wall display.

It was distilled from a real installation that has been running since 2026. Everything
here is generic: example addresses, a placeholder user, no accounts. The
**reference implementation** is that installation's working code (107 hardware-free
tests), and the docs record what was measured and what went wrong, which is the most
useful part.

## How to use this repo with Claude Code

This is written to be handed to a [Claude Code](https://claude.com/claude-code) agent
along with you, the builder.

1. **Clone it** as the start of your own project repo.
2. **Fill in your site profile.** Copy `CLAUDE.local.md.example` to `CLAUDE.local.md`
   (gitignored) and fill in your addresses, tank dimensions, pump and accounts. Claude
   Code loads it automatically next to `CLAUDE.md`.
3. **Start Claude Code in the repo** and ask it to follow
   [`docs/09-build-plan.md`](docs/09-build-plan.md). The plan goes phase by phase, from
   bench sensor to a live pump, and each phase has acceptance checks. The agent should
   stop and ask you at every step that touches mains power, water or your network.
4. **Keep the safety rules** in [`CLAUDE.md`](CLAUDE.md) and
   [`docs/05-safety.md`](docs/05-safety.md) as they are, whatever your thresholds turn out to be.

You don't need Claude Code to use it; the docs and code stand on their own.

## What's inside

| Path | What it is |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Instructions for your agent: safety rules, conventions, workflow, hazards |
| `CLAUDE.local.md.example` | Template for your private site profile (addresses, dimensions, credentials) |
| [`docs/01-overview.md`](docs/01-overview.md) | What the system does and how the parts fit |
| [`docs/02-hardware.md`](docs/02-hardware.md) | Bill of materials, wiring, mounting, power |
| [`docs/03-software.md`](docs/03-software.md) | Software requirements, stack, deployment layout |
| [`docs/04-protocols.md`](docs/04-protocols.md) | MQTT topics and payloads, serial framing, HTTP APIs, Modbus registers |
| [`docs/05-safety.md`](docs/05-safety.md) | The safety rules, the state machine, failure modes |
| [`docs/06-security.md`](docs/06-security.md) | Secrets, network exposure, publishing hygiene |
| [`docs/07-calibration.md`](docs/07-calibration.md) | Defining "full", sensor dead zones, flow testing |
| [`docs/08-lessons-learned.md`](docs/08-lessons-learned.md) | Real incidents and what changed because of them |
| [`docs/09-build-plan.md`](docs/09-build-plan.md) | Phased build plan with acceptance checks |
| [`docs/10-roadmap.md`](docs/10-roadmap.md) | Known issues and planned upgrades: float switch, Shelly Pro 1PM, network isolation, hydrostatic sensor |
| [`presentation/`](presentation/index.html) | A 15-slide talk about the project (one HTML file, works offline) |
| [`.claude/skills/`](.claude/skills) | Claude Code skills per subsystem: firmware, controller, smart plug, testing, webcam |
| [`reference/`](reference) | The working implementation: controller, firmware, tools, tests |

## The system in one picture

```
 Tank A sensor (ESP32-S3 + JSN-SR04T) ──USB serial or WiFi──┐
 Tank B sensor (ESP32-S3 + JSN-SR04T) ──WiFi────────────────┤ MQTT
                                                            ▼
                     ┌──────────────────────────────┐   local API   ┌────────────┐
                     │ Raspberry Pi                  │─────────────▶│ smart plug │──▶ PUMP (A → B)
                     │  Mosquitto · controller.py    │              └────────────┘
                     │  state machine · dashboard    │◀── Modbus TCP ── solar inverter (optional)
                     │  weather · e-ink renderer     │◀── HTTPS ─────── Open-Meteo (optional)
                     └──────────────┬───────────────┘
                                    │ HTTP
                                    ▼
                      e-ink wall display (optional, battery)
```

Only the pump state machine can switch the pump. Everything else only reads its state.

## Requirements at a glance

- Two ESP32-S3 dev boards, two JSN-SR04T V3.x waterproof ultrasonic sensors
- A Raspberry Pi 3 or newer (64-bit Raspberry Pi OS / Debian), with a good power supply
- A smart plug with a local API and energy monitoring (reference: TP-Link Tapo P110M), or
  a relay module rated for your pump
- A transfer pump suited to your lift and distance, plus a float switch as a hardware backstop
- A workstation with Python 3.11+, PlatformIO and Claude Code

Details in [`docs/02-hardware.md`](docs/02-hardware.md) and [`docs/03-software.md`](docs/03-software.md).

## A word on safety

This system switches a mains-powered pump without anyone watching. A pump that runs dry
destroys its seals in minutes, and an overfilled tank floods something. The design
answers that with layered limits (thresholds, runtime caps, sensor timeouts, fail-off on
unknown state), and the docs explain each one. Fixed 240 V / 120 V wiring is a job for a
licensed electrician. Shared as is, under the [MIT License](LICENSE).
