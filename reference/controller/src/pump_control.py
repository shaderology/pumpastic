"""
Pump state machine.

Safety rules (CLAUDE.md — non-negotiable):
  1. NEVER pump if Tank A < 20%      — dry-run destroys pump seals
  2. NEVER pump if Tank B > 95%      — overflow
  3. Max pump runtime (max_run_seconds, 90 min) — force FAULT + alert if exceeded
  4. Sensor timeout 120s → FAULT     — no data = unknown = dangerous
  5. Plug turn-on failure → FAULT    — unknown state is dangerous
  6. Hysteresis: start ≥90%, stop ≤60% for Tank A
  7. Sensor stuck (identical readings) → FAULT
  8. Dry-run detection: pump drawing <threshold watts → FAULT

States:
  IDLE      → waiting. Starts pump when conditions met.
  MIN_RUN   → pump ON, enforcing minimum run (prevents short-cycling).
              Only safety faults can stop the pump here.
  RUNNING   → pump ON, normal operation. Stop conditions apply.
  COOLDOWN  → pump OFF, resting before next run.
  FAULT     → pump OFF, alert sent. Auto-clears after fault_clear_seconds
              once all conditions are safe.
"""
import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Deque, Optional

log = logging.getLogger("pumpastic.pump")


class PumpState(str, Enum):
    IDLE = "IDLE"
    MIN_RUN = "MIN_RUN"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    FAULT = "FAULT"


@dataclass
class _StartConfirm:
    """Consecutive distinct readings that have satisfied the start condition."""
    count: int = 0
    last_seen: int = -1        # tank_a reading sequence number last counted

    def reset(self) -> None:
        self.count = 0
        self.last_seen = -1


@dataclass
class TankReading:
    level_pct: float
    distance_cm: float
    volume_l: float
    received_at: datetime = field(default_factory=datetime.now)


class PumpController:
    """
    Thread-safe pump state machine.

    Sensor data arrives from the MQTT callback thread via on_sensor_data().
    The run() coroutine drives the state machine from the asyncio event loop.
    """

    TICK_INTERVAL_S = 5

    def __init__(
        self,
        config: dict,
        plug,
        alerter,
        clock: Callable[[], datetime] = None,
        on_state_change: Callable[[dict], None] = None,
    ):
        self._cfg = config
        self._plug = plug
        self._alerter = alerter
        self._clock = clock or datetime.now
        self._on_state_change = on_state_change

        self._lock = threading.Lock()
        self._state = PumpState.IDLE
        self._state_entered_at: datetime = self._clock()
        self._pump_started_at: Optional[datetime] = None
        self._fault_reason: str = ""
        self._tank_a: Optional[TankReading] = None
        self._tank_b: Optional[TankReading] = None

        # Thresholds (all from config — never hardcoded)
        p = config["pump"]
        self._start_pct = float(p["start_threshold_pct"])
        # Distinct sensor readings that must all satisfy the start condition before the
        # pump runs. One reading is not enough: on 2026-09-24 the JSN-SR04T reported its
        # dead-zone floor three times, which read as 100% and ran the pump for 26 minutes
        # on a tank that was really at ~68%.
        self._start_confirm_n = int(p.get("start_confirm_readings", 3))
        self._start_confirm = _StartConfirm()
        # Counts tank_a readings as they arrive. Used instead of received_at to tell
        # readings apart: two readings can share a timestamp if the clock is coarse,
        # and a counter says "this is new evidence" without depending on that.
        self._tank_a_seq = 0
        self._stop_low_pct = float(p["stop_low_threshold_pct"])
        self._stop_high_pct = float(p["stop_high_threshold_pct"])
        self._min_run_s = float(p["min_run_seconds"])
        self._cooldown_s = float(p["cooldown_seconds"])
        self._max_run_s = float(p["max_run_seconds"])
        self._sensor_timeout_s = float(p["sensor_timeout_seconds"])
        self._abs_min_pct = float(p.get("absolute_min_pct", 20.0))
        self._abs_max_pct = float(p.get("absolute_max_pct", 95.0))
        self._fault_clear_s = float(p.get("fault_clear_seconds", 300.0))

        # Sensor stuck detection — flag if last N readings are within delta_cm of each other.
        # Set stuck_detection_count: 0 to disable.
        stuck_n = int(p.get("stuck_detection_count", 0))
        self._stuck_delta_cm = float(p.get("stuck_detection_delta_cm", 0.5))
        if stuck_n > 0:
            self._tank_a_dist_hist: Optional[Deque[float]] = deque(maxlen=stuck_n)
            self._tank_b_dist_hist: Optional[Deque[float]] = deque(maxlen=stuck_n)
            self._stuck_n = stuck_n
        else:
            self._tank_a_dist_hist = None
            self._tank_b_dist_hist = None
            self._stuck_n = 0

        # Dry-run detection via Tapo power monitoring.
        # Set dry_run_watt_threshold: 0 to disable.
        self._dry_run_threshold_w = float(p.get("dry_run_watt_threshold", 0.0))
        self._dry_run_timeout_s = float(p.get("dry_run_timeout_seconds", 30.0))
        self._low_power_since: Optional[datetime] = None

    # ── Public API (called from MQTT thread) ──────────────────────────────────

    def on_sensor_data(self, sensor_id: str, data: dict) -> None:
        """
        Update tank state from an incoming MQTT sensor payload.
        Thread-safe — called from the paho-mqtt callback thread.
        """
        try:
            reading = TankReading(
                level_pct=float(data["level_pct"]),
                distance_cm=float(data["distance_cm"]),
                volume_l=float(data.get("volume_l", 0)),
                received_at=self._clock(),
            )
            with self._lock:
                if sensor_id == "tank_a":
                    self._tank_a = reading
                    self._tank_a_seq += 1
                    if self._tank_a_dist_hist is not None:
                        self._tank_a_dist_hist.append(reading.distance_cm)
                elif sensor_id == "tank_b":
                    self._tank_b = reading
                    if self._tank_b_dist_hist is not None:
                        self._tank_b_dist_hist.append(reading.distance_cm)
                else:
                    log.warning("Unknown sensor_id: %s", sensor_id)
                    return
            log.debug(
                "Sensor %s: %.1f%% (%.1f cm)",
                sensor_id, reading.level_pct, reading.distance_cm,
            )
        except (KeyError, ValueError) as e:
            log.warning("Bad sensor payload from %s: %s — %s", sensor_id, data, e)

    def on_power_reading(self, watts: float) -> None:
        """
        Called with current power draw from the plug.
        Tracks low-power condition for dry-run detection.
        Thread-safe — called from the power poll loop.
        """
        if self._dry_run_threshold_w <= 0:
            return  # Dry-run detection disabled
        with self._lock:
            pump_on = self._state in (PumpState.MIN_RUN, PumpState.RUNNING)
            if not pump_on:
                self._low_power_since = None
                return
            if watts < self._dry_run_threshold_w:
                if self._low_power_since is None:
                    self._low_power_since = self._clock()
                    log.debug(
                        "Low power detected: %.1fW (threshold %.1fW)", watts, self._dry_run_threshold_w
                    )
            else:
                self._low_power_since = None

    def reload_config(self, pump_cfg: dict) -> None:
        """Hot-reload pump thresholds from an updated config dict. Thread-safe.
        Note: stuck_detection_count changes require a service restart (deque resize)."""
        with self._lock:
            self._start_pct = float(pump_cfg["start_threshold_pct"])
            self._start_confirm_n = int(pump_cfg.get("start_confirm_readings",
                                                     self._start_confirm_n))
            self._stop_low_pct = float(pump_cfg["stop_low_threshold_pct"])
            self._stop_high_pct = float(pump_cfg["stop_high_threshold_pct"])
            self._min_run_s = float(pump_cfg["min_run_seconds"])
            self._cooldown_s = float(pump_cfg["cooldown_seconds"])
            self._max_run_s = float(pump_cfg["max_run_seconds"])
            self._sensor_timeout_s = float(pump_cfg["sensor_timeout_seconds"])
            self._abs_min_pct = float(pump_cfg.get("absolute_min_pct", 20.0))
            self._abs_max_pct = float(pump_cfg.get("absolute_max_pct", 95.0))
            self._fault_clear_s = float(pump_cfg.get("fault_clear_seconds", 300.0))
            self._dry_run_threshold_w = float(pump_cfg.get("dry_run_watt_threshold", 0.0))
            self._dry_run_timeout_s = float(pump_cfg.get("dry_run_timeout_seconds", 30.0))
        log.info(
            "Config hot-reloaded — start: %.0f%%, stop_low: %.0f%%, "
            "min_run: %.0fs, cooldown: %.0fs",
            self._start_pct, self._stop_low_pct, self._min_run_s, self._cooldown_s,
        )

    @property
    def state(self) -> PumpState:
        with self._lock:
            return self._state

    def get_status(self) -> dict:
        """Return a snapshot of controller status for the dashboard and MQTT publishing."""
        with self._lock:
            pump_on = self._state in (PumpState.MIN_RUN, PumpState.RUNNING)
            runtime_s = None
            if self._pump_started_at and pump_on:
                runtime_s = (self._clock() - self._pump_started_at).total_seconds()
            now = self._clock()

            def _tank_info(tank: Optional[TankReading]) -> Optional[dict]:
                if tank is None:
                    return None
                return {
                    "level_pct": tank.level_pct,
                    "distance_cm": tank.distance_cm,
                    "volume_l": tank.volume_l,
                    "age_s": (now - tank.received_at).total_seconds(),
                }

            return {
                "state": self._state.value,
                "pump_on": pump_on,
                "fault_reason": self._fault_reason,
                "state_since": self._state_entered_at.isoformat(),
                "runtime_s": runtime_s,
                "tank_a": _tank_info(self._tank_a),
                "tank_b": _tank_info(self._tank_b),
                # Flat keys retained for backward compatibility (MQTT publish, tests)
                "tank_a_pct": self._tank_a.level_pct if self._tank_a else None,
                "tank_b_pct": self._tank_b.level_pct if self._tank_b else None,
            }

    def clear_fault(self) -> bool:
        """
        Clear FAULT state and return to IDLE.
        Thread-safe. Returns True if state was FAULT and was cleared.
        """
        with self._lock:
            if self._state != PumpState.FAULT:
                return False
            self._state = PumpState.IDLE
            self._state_entered_at = self._clock()
            self._fault_reason = ""
        log.info("Fault cleared via dashboard — returning to IDLE")
        if self._on_state_change:
            try:
                self._on_state_change(self.get_status())
            except Exception:
                log.exception("on_state_change callback failed after clear_fault")
        return True

    async def emergency_stop(self) -> None:
        """
        Force pump OFF and enter COOLDOWN regardless of current state.
        Called from the dashboard control panel.
        """
        with self._lock:
            state = self._state
        if state not in (PumpState.COOLDOWN, PumpState.FAULT):
            log.warning("Emergency stop requested via dashboard (from state %s)", state.value)
            if state in (PumpState.MIN_RUN, PumpState.RUNNING):
                await self._plug.turn_off()
            await self._set_state(PumpState.COOLDOWN, pump_on=False)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Drive the state machine forever. Cancel to stop."""
        log.info("State machine started (tick every %ds)", self.TICK_INTERVAL_S)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected error in state machine tick")
            await asyncio.sleep(self.TICK_INTERVAL_S)

    # ── Tick logic ────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        now = self._clock()

        # Snapshot mutable state under lock
        with self._lock:
            state = self._state
            state_entered_at = self._state_entered_at
            pump_started_at = self._pump_started_at
            tank_a = self._tank_a
            tank_b = self._tank_b
            a_dist_hist = list(self._tank_a_dist_hist) if self._tank_a_dist_hist is not None else None
            b_dist_hist = list(self._tank_b_dist_hist) if self._tank_b_dist_hist is not None else None
            low_power_since = self._low_power_since

        elapsed_in_state = (now - state_entered_at).total_seconds()

        # ── Sensor freshness check ─────────────────────────────────────────
        a_age = (now - tank_a.received_at).total_seconds() if tank_a else float("inf")
        b_age = (now - tank_b.received_at).total_seconds() if tank_b else float("inf")
        sensors_timed_out = a_age >= self._sensor_timeout_s or b_age >= self._sensor_timeout_s

        if sensors_timed_out:
            reason = (
                f"Tank A silent {a_age:.0f}s" if a_age >= self._sensor_timeout_s
                else f"Tank B silent {b_age:.0f}s"
            )
            await self._enter_fault(reason, state)
            return

        a_pct = tank_a.level_pct
        b_pct = tank_b.level_pct

        # ── Sensor stuck detection ─────────────────────────────────────────
        if self._stuck_n > 0:
            for label, hist in (("Tank A", a_dist_hist), ("Tank B", b_dist_hist)):
                if hist is not None and len(hist) >= self._stuck_n:
                    spread = max(hist) - min(hist)
                    if spread <= self._stuck_delta_cm:
                        await self._enter_fault(
                            f"{label} sensor stuck: {self._stuck_n} readings within {spread:.2f} cm",
                            state,
                        )
                        return

        # ── Dry-run detection ──────────────────────────────────────────────
        if self._dry_run_threshold_w > 0 and state in (PumpState.MIN_RUN, PumpState.RUNNING):
            if low_power_since is not None:
                low_duration = (now - low_power_since).total_seconds()
                if low_duration >= self._dry_run_timeout_s:
                    await self._enter_fault(
                        f"Dry-run detected: pump drawing <{self._dry_run_threshold_w:.0f}W "
                        f"for {low_duration:.0f}s",
                        state,
                    )
                    return

        # ── Absolute safety limits (checked whenever pump is on) ───────────
        if state in (PumpState.MIN_RUN, PumpState.RUNNING):
            if a_pct < self._abs_min_pct:
                await self._enter_fault(
                    f"Tank A critically low: {a_pct:.1f}% < {self._abs_min_pct}%", state
                )
                return
            if b_pct > self._abs_max_pct:
                await self._enter_fault(
                    f"Tank B critically full: {b_pct:.1f}% > {self._abs_max_pct}%", state
                )
                return

        # ── Max runtime (safety rule 3) ────────────────────────────────────
        if pump_started_at and state in (PumpState.MIN_RUN, PumpState.RUNNING):
            pump_runtime_s = (now - pump_started_at).total_seconds()
            if pump_runtime_s >= self._max_run_s:
                await self._enter_fault(
                    f"Max runtime exceeded: {pump_runtime_s:.0f}s / {self._max_run_s:.0f}s",
                    state,
                )
                return

        # ── State transitions ──────────────────────────────────────────────
        if state == PumpState.IDLE:
            if not (a_pct >= self._start_pct and b_pct < self._stop_high_pct):
                # One disagreeing reading clears the streak, so only a sustained
                # condition can start the pump.
                if self._start_confirm.count:
                    log.info(
                        "Start condition no longer met (Tank A=%.1f%%) — confirmation "
                        "reset after %d/%d readings",
                        a_pct, self._start_confirm.count, self._start_confirm_n,
                    )
                    self._start_confirm.reset()
                return

            # Count distinct readings, not ticks. The state machine ticks every 5 s but
            # readings arrive every 30 s, so the same reading seen on six ticks is still
            # only one piece of evidence.
            if self._tank_a_seq != self._start_confirm.last_seen:
                self._start_confirm.last_seen = self._tank_a_seq
                self._start_confirm.count += 1

            if self._start_confirm.count < self._start_confirm_n:
                log.info(
                    "Start condition met on reading %d/%d (Tank A=%.1f%%) — waiting for "
                    "confirmation before starting",
                    self._start_confirm.count, self._start_confirm_n, a_pct,
                )
                return

            log.info(
                "Start condition confirmed on %d readings: Tank A=%.1f%% Tank B=%.1f%% "
                "— turning pump ON",
                self._start_confirm.count, a_pct, b_pct,
            )
            success = await self._plug.turn_on()
            if success:
                await self._set_state(PumpState.MIN_RUN, pump_on=True)
                self._alerter.send(
                    "pump_start",
                    f"Pump started. Tank A: {a_pct:.1f}%, Tank B: {b_pct:.1f}%",
                )
            else:
                await self._enter_fault("Plug failed to turn ON", state)

        elif state == PumpState.MIN_RUN:
            if elapsed_in_state >= self._min_run_s:
                log.info("Min run time elapsed (%.0fs) — entering RUNNING", elapsed_in_state)
                await self._set_state(PumpState.RUNNING)

        elif state == PumpState.RUNNING:
            if a_pct <= self._stop_low_pct:
                reason = f"Tank A drained to {a_pct:.1f}% (stop threshold {self._stop_low_pct}%)"
                await self._stop_pump_to_cooldown(reason, a_pct, b_pct)
            elif b_pct >= self._stop_high_pct:
                reason = f"Tank B reached {b_pct:.1f}% (stop threshold {self._stop_high_pct}%)"
                await self._stop_pump_to_cooldown(reason, a_pct, b_pct)

        elif state == PumpState.COOLDOWN:
            if elapsed_in_state >= self._cooldown_s:
                log.info("Cooldown complete — returning to IDLE")
                await self._set_state(PumpState.IDLE)

        elif state == PumpState.FAULT:
            # Auto-clear when safe conditions hold for fault_clear_seconds
            if elapsed_in_state >= self._fault_clear_s:
                if (a_pct >= self._abs_min_pct and b_pct <= self._abs_max_pct):
                    log.info("Fault cleared after %.0fs — returning to IDLE", elapsed_in_state)
                    self._fault_reason = ""
                    await self._set_state(PumpState.IDLE)

    # ── Internal state helpers ─────────────────────────────────────────────────

    async def _stop_pump_to_cooldown(self, reason: str, a_pct: float, b_pct: float) -> None:
        log.info("Stopping pump: %s", reason)
        await self._plug.turn_off()
        await self._set_state(PumpState.COOLDOWN, pump_on=False)
        self._alerter.send(
            "pump_stop",
            f"Pump stopped: {reason}. Tank A: {a_pct:.1f}%, Tank B: {b_pct:.1f}%",
        )

    async def _enter_fault(self, reason: str, from_state: PumpState) -> None:
        """Turn pump off and enter FAULT. De-duplicates repeated fault alerts."""
        if from_state in (PumpState.MIN_RUN, PumpState.RUNNING):
            log.error("FAULT (pump ON → turning OFF): %s", reason)
            await self._plug.turn_off()
        else:
            log.error("FAULT: %s", reason)

        already_in_fault = (from_state == PumpState.FAULT)
        await self._set_state(PumpState.FAULT, pump_on=False)
        with self._lock:
            self._fault_reason = reason

        if not already_in_fault:
            self._alerter.send("fault", f"FAULT: {reason}")

    async def _set_state(
        self, new_state: PumpState, pump_on: Optional[bool] = None
    ) -> None:
        # A partial start-confirmation streak is only meaningful within one uninterrupted
        # stretch of IDLE; any transition invalidates it.
        self._start_confirm.reset()
        with self._lock:
            old_state = self._state
            self._state = new_state
            self._state_entered_at = self._clock()
            if pump_on is True:
                self._pump_started_at = self._clock()
            elif pump_on is False:
                self._pump_started_at = None

        if old_state != new_state:
            log.info("State: %s → %s", old_state.value, new_state.value)
            if self._on_state_change:
                try:
                    self._on_state_change(self.get_status())
                except Exception:
                    log.exception("on_state_change callback failed")
