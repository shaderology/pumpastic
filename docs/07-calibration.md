# 07 — Calibration

## Define "full" as the overflow outlet

```
water_depth = SENSOR_HEIGHT_CM − distance_cm
level_pct   = water_depth / OVERFLOW_HEIGHT_CM × 100      (clamped 0–100)
volume_l    = capacity_litres × level_pct / 100
```

- `height_cm` / `SENSOR_HEIGHT_CM`: sensor face to tank floor.
- `overflow_height_cm` / `OVERFLOW_HEIGHT_CM`: floor to the overflow outlet.
- `capacity_litres`: volume **at the overflow**, not at the tank's top.

Why not scale against the sensor height? In the reference Tank A that would put the
overflow at 77%, so a 90% start threshold could never be reached and the pump would never
run. Overflow-relative scaling keeps every threshold meaningful.

For a vertical-walled round tank: `capacity_litres = π × (d/2)² × overflow_height × 1000`
(metres in). Two tanks plumbed together share one level: add their areas, and the lower
overflow governs.

Worked example (reference Tank A): Ø 1.90 m (2.835 m², 28.35 L per cm), sensor 220 cm up,
overflow at 170 cm → 4,820 L.

| Level | Water | Sensor distance |
|---|---|---|
| 100% (overflow) | 1.700 m | 50.0 cm |
| 94% (start) | 1.598 m | 60.2 cm |
| 60% (stop) | 1.020 m | 118.0 cm |
| 20% (floor) | 0.340 m | 186.0 cm |

Make the same table for your tanks before choosing thresholds.

## Measure the dead zone

With the sensor on a bench, move a flat board slowly toward it and log `distance_cm`. The
dead zone is where the reading stops tracking and sticks at a floor (22.7 cm on the
reference unit). When the sensor gets no usable echo it **reports that floor**, which
converts to "100%", so:

- `SENSOR_MIN_CM` must sit **clearly above** the dead zone (reference: 30 cm, a 7 cm margin).
- It must also sit **below the overflow distance by enough to read storm overfill**. The
  reference used 45 cm (just under the 50 cm overflow) until a storm pushed the water to
  ~40 cm and every reading was rejected (see `docs/08`).
- If the overflow distance is close to the dead zone, mount the sensor higher. Rule of
  thumb: overflow distance ≥ dead zone + 15 cm.

## Check against a tape

With the pump off and the surface still, compare the sensor's water depth against a tape
measurement. A few cm of difference is mounting tolerance: trim `height_cm`, not the formula.

## Know your noise

Log readings for an hour with the pump off (`mosquitto_sub -t rainwater/tank_a/level/state -v`).
The reference sensor was stable to ±1 cm on a still surface. Mid-transfer, readings were
anywhere from ±0.2 cm to ±17 cm depending on mounting. Use this to set
`stuck_detection_delta_cm`, and **never compute a flow rate from in-run readings alone**.

## Check the whole range

Watch at least one full transfer cycle and confirm the reading keeps falling smoothly to
the stop threshold. If it goes flat while the pump is clearly moving water (normal watts),
the beam has found a fixed echo; fix the mounting (stilling tube) before trusting the
stop threshold.

## Measure the flow rate

`reference/tools/flow_test.py`, run on the Pi:

1. Samples a settled baseline with the pump off.
2. Lowers `start_threshold_pct` so the state machine starts the pump normally, with every
   limit live, then restores it immediately.
3. Logs every reading, power sample and state change to a CSV.
4. After the run, lets the surface settle and samples again.
5. Reports flow from settled before/after medians, cross-checked against a regression of
   the in-run readings (they disagree when the sensor lost the surface).

Reference results: 1,538 L/h (settled medians) and 1,542 L/h (in-run regression), at
~620 W, i.e. 0.40 kWh per 1,000 L. Then size `max_run_seconds` (`docs/05`).

## Sanity-check against the pump curve

A pump rated 3,600 L/h at 0 m head and 0 at 40 m, measured at 1,540 L/h, sits at ~23 m of
total head: 16 m of static lift plus ~7 m of pipe friction. Large friction is a hint that
a wider pipe would buy flow for free.
