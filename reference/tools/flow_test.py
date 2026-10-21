#!/usr/bin/env python3
"""Measure Tank A -> Tank B transfer rate and pump power. Run on the Raspberry Pi.

Method: the level is only trustworthy with the pump off (std dev ~1 cm settled versus
~17 cm mid-transfer), so volume is taken from settled medians before and after the run,
never from readings during it. Runtime comes from the controller's own pump on/off
transitions.

The pump is started through the normal state machine by temporarily lowering
start_threshold_pct, so every safety check stays active: the 60% stop threshold, the 20%
absolute floor, the max runtime (max_run_seconds) and the 3-reading start confirmation are
all untouched. The threshold is restored as soon as the pump starts, and again in a finally
block.

During the run every Tank A reading, pump power sample and state change is logged to a CSV
(--log), so the transfer-rate curve can be examined afterwards. In-run levels are noisy, so
the headline rate still comes from the settled before/after medians.

Usage: python3 flow_test.py [--start-threshold 65] [--settle 300] [--log run.csv]
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime

API = "http://localhost:8080/api"
MAX_WAIT_FOR_START_S = 420
CAP_MARGIN_S = 60                                  # local cap = max_run_seconds - this


def sh(cmd: str, timeout: int = 90) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          timeout=timeout).stdout.strip()


def get_config() -> dict:
    return json.loads(sh(f"curl -s {API}/config"))


def post_config(cfg: dict, start_pct: float) -> str:
    """Re-post the whole config with one threshold changed (the endpoint wants all keys)."""
    p, t = cfg["pump"], cfg["tanks"]
    body = {k: p[k] for k in (
        "start_threshold_pct", "stop_low_threshold_pct", "stop_high_threshold_pct",
        "absolute_min_pct", "absolute_max_pct", "max_run_seconds",
        "sensor_timeout_seconds", "min_run_seconds", "cooldown_seconds",
        "fault_clear_seconds")}
    # These are absent from config.yaml while both detectors are disabled, but the
    # endpoint range-checks them regardless. Supply in-range placeholders that keep the
    # detectors off: stuck detection is gated by count=0, dry-run by threshold=0, so
    # the delta and timeout values are inert.
    inert = {"stuck_detection_count": 0, "stuck_detection_delta_cm": 0.5,
             "dry_run_watt_threshold": 0, "dry_run_timeout_seconds": 30}
    for k, default in inert.items():
        v = p.get(k)
        body[k] = default if v is None else v
    body["start_threshold_pct"] = start_pct
    for tank, prefix in (("tank_a", "tank_a"), ("tank_b", "tank_b")):
        body[f"{prefix}_height_cm"] = t[tank]["height_cm"]
        body[f"{prefix}_overflow_cm"] = t[tank]["overflow_height_cm"]
        body[f"{prefix}_capacity_l"] = t[tank]["capacity_litres"]
    out = sh(f"curl -s -X POST {API}/config -H 'Content-Type: application/json' "
             f"-d '{json.dumps(body)}'")
    return out


def sample_level(seconds: int, label: str) -> float:
    """Median distance_cm over a window, with the pump off."""
    print(f"  sampling {label} for {seconds}s …", flush=True)
    raw = sh(f"timeout {seconds + 15} mosquitto_sub -h localhost "
             f"-t rainwater/tank_a/level/state -W {seconds}", timeout=seconds + 40)
    d = [json.loads(l)["distance_cm"] for l in raw.splitlines() if l.strip().startswith("{")]
    if not d:
        raise SystemExit(f"No readings while sampling {label}")
    med = statistics.median(d)
    print(f"  {label}: n={len(d)} median={med:.2f} cm "
          f"min={min(d):.1f} max={max(d):.1f} stdev={statistics.pstdev(d):.2f}", flush=True)
    return med


def pump_state() -> dict:
    raw = sh("timeout 20 mosquitto_sub -h localhost -t rainwater/pump/state -C 1 -W 15")
    return json.loads(raw) if raw.startswith("{") else {}


def power_stats(t0: datetime, t1: datetime) -> dict:
    since = t0.strftime("%Y-%m-%d %H:%M:%S")
    until = t1.strftime("%Y-%m-%d %H:%M:%S")
    out = sh(f"journalctl -u pumpastic --since '{since}' --until '{until}' --no-pager")
    w = [float(m) for m in re.findall(r"Pump power: ([0-9.]+) W", out)]
    if not w:
        return {}
    return {"n": len(w), "mean": statistics.mean(w), "min": min(w), "max": max(w)}


def in_run_rate(log_path: str, t_start: datetime, t_stop: datetime) -> tuple:
    """
    Fit level vs time over the run from the CSV log. Returns (cm_per_min, n, last_change_min).

    Cross-checks the settled before/after figure. On 2026-10-04 the sensor lost the surface
    at ~107 cm and latched onto fixed echoes, so 'after' was wrong and the before/after rate
    read 1039 L/h while the real rate was 1542 L/h. A flat tail in the log is the tell.
    """
    pts = []
    for line in open(log_path):
        if " rainwater/tank_a/level/state " not in line:
            continue
        ts, _, payload = line.split(" ", 2)
        t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        if t_start <= t <= t_stop:
            pts.append(((t - t_start).total_seconds() / 60, json.loads(payload)["distance_cm"]))
    if len(pts) < 10:
        return None, len(pts), None
    # Use the first 15 minutes after min-run settling, where tracking is most reliable.
    early = [p for p in pts if 0.5 <= p[0] <= 15] or pts
    xs, ys = [p[0] for p in early], [p[1] for p in early]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in early) / sum((x - mx) ** 2 for x in xs)
    # When did the reading last move by more than 1.5 cm from the final value?
    final = pts[-1][1]
    last_change = max((m for m, c in pts if abs(c - final) > 1.5), default=0.0)
    return slope, len(pts), last_change


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-threshold", type=float, default=65.0)
    ap.add_argument("--settle", type=int, default=300)
    ap.add_argument("--log", default=f"flow_test_{datetime.now():%Y%m%d_%H%M}.csv")
    args = ap.parse_args()

    cfg = get_config()
    stop_low = cfg["pump"]["stop_low_threshold_pct"]
    cap_l = cfg["tanks"]["tank_a"]["capacity_litres"]
    # Vertical-walled tank: capacity is defined at the overflow, so this is exact.
    litres_per_cm = cap_l / cfg["tanks"]["tank_a"]["overflow_height_cm"]
    # Restore whatever was configured before the test, never a hardcoded value.
    production_start_pct = cfg["pump"]["start_threshold_pct"]
    print(f"Config: start->{args.start_threshold}% (was {cfg['pump']['start_threshold_pct']}%), "
          f"stop_low={stop_low}%, max_run={cfg['pump']['max_run_seconds']}s")

    st = pump_state()
    if st.get("state") != "IDLE":
        print(f"Refusing to start: controller is {st.get('state')}, not IDLE")
        return 1

    # The controller ends the run itself (60% stop or max runtime). This local cap is
    # only a backstop, just under max_run_seconds so the run ends cleanly, not as a FAULT.
    hard_cap_s = float(cfg["pump"]["max_run_seconds"]) - CAP_MARGIN_S
    before_cm = sample_level(120, "baseline (pump off)")
    t_before = datetime.now()

    # Record everything from here to the end: level, power, state, with broker timestamps.
    log_f = open(args.log, "w")
    log_f.write("# mosquitto_sub -F '%I %t %p'  (ISO time, topic, payload)\n")
    log_f.flush()
    logger = subprocess.Popen(
        ["mosquitto_sub", "-h", "localhost", "-F", "%I %t %p",
         "-t", "rainwater/tank_a/level/state", "-t", "rainwater/pump/power",
         "-t", "rainwater/pump/state"],
        stdout=log_f, stderr=subprocess.DEVNULL)

    restored = False
    try:
        print(post_config(cfg, args.start_threshold))

        print("Waiting for the state machine to start the pump …", flush=True)
        t_start = None
        deadline = time.time() + MAX_WAIT_FOR_START_S
        while time.time() < deadline:
            s = pump_state()
            if s.get("pump_on"):
                t_start = datetime.now()
                print(f"  pump ON at {t_start:%H:%M:%S} (state {s['state']}, "
                      f"tank_a {s['tank_a']['level_pct']}%)", flush=True)
                break
            time.sleep(5)
        if t_start is None:
            print("Pump never started — aborting")
            return 1

        # Restore the production threshold immediately: the run is already under way and
        # a crash from here on must not leave a low trigger in place.
        print(post_config(get_config(), production_start_pct))
        restored = True

        while True:
            s = pump_state()
            if not s.get("pump_on"):
                t_stop = datetime.now()
                print(f"  pump OFF at {t_stop:%H:%M:%S} (state {s.get('state')})", flush=True)
                break
            run_s = (datetime.now() - t_start).total_seconds()
            if run_s > hard_cap_s:
                print("  hit local run cap — forcing off", flush=True)
                sh(f"curl -s -X POST {API}/control -H 'Content-Type: application/json' "
                   f"-d '{{\"action\":\"force_pump_off\"}}'")
                t_stop = datetime.now()
                break
            time.sleep(10)
    finally:
        if not restored:
            print("Restoring production start threshold …")
            print(post_config(get_config(), production_start_pct))

    runtime_s = (t_stop - t_start).total_seconds()
    print(f"\nRun time: {runtime_s:.0f}s ({runtime_s/60:.1f} min)")
    pw = power_stats(t_start, t_stop)

    print(f"Letting the surface settle for {args.settle}s …", flush=True)
    time.sleep(args.settle)
    after_cm = sample_level(120, "final (pump off, settled)")
    logger.terminate()
    log_f.close()

    drop_cm = after_cm - before_cm
    litres = drop_cm * litres_per_cm
    lph = litres / (runtime_s / 3600) if runtime_s else 0

    print("\n" + "=" * 62)
    print("TANK A -> TANK B TRANSFER TEST")
    print("=" * 62)
    print(f"  level      {before_cm:.2f} cm -> {after_cm:.2f} cm   (drop {drop_cm:.2f} cm)")
    print(f"  volume     {litres:.0f} L in {runtime_s/60:.1f} min")
    print(f"  FLOW RATE  {lph:.0f} L/h  ({litres/(runtime_s/60):.1f} L/min)")
    if pw:
        kwh = pw["mean"] * runtime_s / 3600 / 1000
        print(f"  power      mean {pw['mean']:.0f} W (min {pw['min']:.0f}, max {pw['max']:.0f}, "
              f"n={pw['n']})")
        print(f"  energy     {kwh:.3f} kWh for this run")
        if litres:
            print(f"  efficiency {pw['mean']*1000/lph/1000:.3f} kWh per 1000 L"
                  if lph else "")
    if lph:
        print("\n  Projections at this rate:")
        for label, vol in (("one cycle 94%->60%", cap_l * 0.34),
                           ("full tank 100%->20%", cap_l * 0.80),
                           ("1000 L", 1000.0)):
            print(f"    {label:22s} {vol:6.0f} L -> {vol/lph*60:5.1f} min")
    print("=" * 62)

    slope, n_pts, last_change = in_run_rate(args.log, t_start, t_stop)
    if slope:
        run_lph = slope * litres_per_cm * 60
        print(f"  in-run rate (first 15 min, n={n_pts}): {slope:.3f} cm/min = {run_lph:.0f} L/h")
        if lph and abs(run_lph - lph) / run_lph > 0.15:
            print(f"  ⚠ before/after rate differs from the in-run rate by "
                  f"{abs(run_lph - lph) / run_lph:.0%}. The sensor probably stopped tracking the "
                  f"surface (readings last moved at {last_change:.0f} min of {runtime_s/60:.0f}); "
                  f"trust the in-run rate and check the log.")

    # Measurement quality: the sensor resolves ~1 cm, which is 28 L on this tank.
    if drop_cm:
        print(f"  (level resolution 1 cm = {litres_per_cm:.0f} L, so this drop of "
              f"{drop_cm:.1f} cm carries about ±{100/drop_cm:.0f}% uncertainty)")
    summary = {
        "date": t_before.isoformat(timespec="seconds"),
        "pump_on": t_start.isoformat(timespec="seconds"),
        "pump_off": t_stop.isoformat(timespec="seconds"),
        "runtime_s": round(runtime_s),
        "before_cm": round(before_cm, 2), "after_cm": round(after_cm, 2),
        "drop_cm": round(drop_cm, 2), "litres": round(litres),
        "flow_lph": round(lph), "power": pw, "log": args.log,
        "stop_state": s.get("state"),
    }
    with open(args.log.replace(".csv", "_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  log: {args.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
