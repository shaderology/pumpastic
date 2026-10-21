# 05 — Safety

The system runs a mains pump with nobody watching. Two outcomes matter: **the pump runs
dry** (Tank A empty, seals destroyed within minutes) and **something overflows** (Tank B
full, or Tank A not emptied in a storm). Every rule below exists to stop one of them, and
each assumes the others might fail.

## The rules

| # | Rule | Setting (reference value) | Guards against |
|---|---|---|---|
| 1 | Never pump if Tank A is below its floor | `absolute_min_pct: 20` | dry run |
| 2 | Never pump if Tank B is above its ceiling | `absolute_max_pct: 95` | overflow |
| 3 | Force OFF and FAULT after a maximum runtime | `max_run_seconds: 5400` | frozen or lying sensor, stuck plug |
| 4 | FAULT if a sensor goes quiet | `sensor_timeout_seconds: 120` | dead node, broken bridge |
| 5 | Plug unreachable → pump OFF / FAULT | built in | unknown state |
| 6 | Hysteresis, minimum run, cooldown | start 94 / stop 60, `min_run_seconds: 300`, `cooldown_seconds: 600` | rapid cycling, motor wear |
| 7 | All thresholds in config | `config.yaml` | silent drift between code and reality |
| 8 | Starting needs agreeing readings | `start_confirm_readings: 3` | a single bad echo starting the pump |
| — | Hardware float switch in series with the pump | wiring | everything above failing at once |

### Sizing the runtime cap (rule 3)

The cap is the backstop for the case that matters most: the sensor freezes on a high
reading while the tank drains. Size it from the **measured** flow rate:

```
max water moved = flow_rate × max_run_seconds
lowest level    = start level − max water moved / capacity
```

It must leave Tank A above rule 1's floor. Reference: 1,540 L/h × 90 min ≈ 2,300 L, so a
4,820 L tank starting at 94% can't go below ~46%, comfortably above 20%. A full 94→60%
cycle takes ~64 min, so 90 min also leaves headroom. **Re-check this every time you change
the cap, the start threshold or the pump.**

## The state machine

```
          start condition held for N distinct readings
  IDLE ─────────────────────────────────────────────▶ MIN_RUN ──(min_run_seconds)──▶ RUNNING
   ▲                                                     │                              │
   │ cooldown_seconds                                    │ hard limits only             │ A ≤ stop_low or B ≥ stop_high
   │                                                     ▼                              ▼
 COOLDOWN ◀──────────────────────────────────────────────────────────────────────── (pump OFF)

 any state ──(rules 1–5, dry-run, stuck sensor, plug failure)──▶ FAULT (pump OFF, alert)
 FAULT ──(fault_clear_seconds with safe readings, or "Clear fault")──▶ IDLE
```

- **IDLE → MIN_RUN:** Tank A ≥ `start_threshold_pct` and Tank B < `stop_high_threshold_pct`
  on `start_confirm_readings` *distinct* readings. One disagreeing reading resets the count.
  Readings arrive every 30 s and the loop ticks every 5 s, so the count is of readings,
  not ticks.
- **MIN_RUN:** the pump runs at least `min_run_seconds` unless a hard limit trips.
- **RUNNING → COOLDOWN:** Tank A ≤ `stop_low_threshold_pct` or Tank B ≥ `stop_high_threshold_pct`.
- **FAULT:** pump off, alert sent once. It clears itself after `fault_clear_seconds` if the
  levels are inside the absolute limits; a fault that still holds re-raises immediately.

## Optional detectors (off until tuned)

- **Dry-run by power:** `dry_run_watt_threshold` (e.g. 30% of measured running watts) and
  `dry_run_timeout_seconds`. A pump that has lost suction draws noticeably less.
- **Stuck sensor:** `stuck_detection_count` readings within `stuck_detection_delta_cm` →
  FAULT. Set it from your measured noise floor, or it trips on a calm tank.
- **Drawdown check** (not in the reference code; recommended): while pumping, FAULT if Tank A
  hasn't dropped ~2 cm in 5 min. It catches a sensor latched onto a fixed echo.

## Failure modes and what happens

| Failure | Result |
|---|---|
| Sensor node dies or loses WiFi | LWT → `offline`; after 120 s, FAULT, pump off |
| Bridge process dies | its LWT publishes `offline`; same as above |
| Sensor reports a dead-zone echo (reads "100%") | rejected by `SENSOR_MIN_CM`; and 3 readings must agree before a start |
| Sensor latches onto a fixed echo while pumping | stop threshold never reached → runtime cap → FAULT. Size the cap for this |
| Water above the overflow in a storm | published as 100% with `overfull`; pumping continues |
| Plug unreachable / command fails | retries, then FAULT; the plug's own state is unknown, so alert |
| Controller crashes | systemd restarts it; on start it commands the plug OFF |
| Pi loses power | plug keeps its last state. **The float switch is the protection here** |
| Mains power cut | plug returns OFF |

## Testing safely

- The hardware-free test suite (`reference/controller/tests`) covers every transition,
  including a replay of the dead-zone false start. Run it before every deploy.
- **Anything that publishes to the live broker can start the pump**, including the
  dashboard's "simulate sensor" control. Do it only while watching, with Tank A well above
  its floor, and with Force OFF ready.
- To exercise a real start end to end, use `reference/tools/flow_test.py`. It starts the
  pump through the normal state machine by temporarily lowering the start threshold, with
  every limit live, and restores the threshold as soon as the pump starts.
