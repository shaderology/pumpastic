"""
Unit tests for the pump state machine.

All tests run without hardware — plug and alerter are mocked.
Time is injected via a MockClock so transitions can be tested instantly.

Run:
    cd controller
    python -m pytest tests/test_pump_control.py -v
"""
from datetime import datetime, timedelta
from typing import List, Optional

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pump_control import PumpController, PumpState


# ── Fixtures and helpers ──────────────────────────────────────────────────────

class MockClock:
    """Controllable clock for deterministic time-based testing."""
    def __init__(self, start: datetime = None):
        self._now = start or datetime(2026, 1, 1, 12, 0, 0)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class MockPlug:
    """Async mock plug that records calls and tracks on/off state."""
    def __init__(self, online: bool = True, turn_on_ok: bool = True, turn_off_ok: bool = True):
        self._online = online
        self._turn_on_ok = turn_on_ok
        self._turn_off_ok = turn_off_ok
        self.is_on = False
        self.on_calls: int = 0
        self.off_calls: int = 0

    async def turn_on(self) -> bool:
        self.on_calls += 1
        if self._turn_on_ok:
            self.is_on = True
        return self._turn_on_ok

    async def turn_off(self) -> bool:
        self.off_calls += 1
        if self._turn_off_ok:
            self.is_on = False
        return self._turn_off_ok

    async def is_online(self) -> bool:
        return self._online

    async def get_power_watts(self) -> Optional[float]:
        return 550.0 if self.is_on else 0.0


class MockAlerter:
    """Records all alerts sent during the test."""
    def __init__(self):
        self.events: List[dict] = []

    def send(self, event: str, message: str, title: str = None) -> None:
        self.events.append({"event": event, "message": message})

    def has_event(self, event: str) -> bool:
        return any(e["event"] == event for e in self.events)


TEST_CONFIG = {
    "pump": {
        "start_threshold_pct": 90,
        "stop_low_threshold_pct": 60,
        "stop_high_threshold_pct": 90,
        "min_run_seconds": 300,
        "cooldown_seconds": 600,
        "max_run_seconds": 1800,
        "sensor_timeout_seconds": 120,
        "absolute_min_pct": 20,
        "absolute_max_pct": 95,
        "fault_clear_seconds": 300,
        # These tests target transitions other than start confirmation and predate it,
        # so they drive the pump with a single reading. The confirmation behaviour has
        # dedicated tests in test_start_confirmation.py, which also asserts that the
        # shipped config uses a safe value.
        "start_confirm_readings": 1,
    }
}


def make_controller(
    online: bool = True,
    turn_on_ok: bool = True,
    turn_off_ok: bool = True,
    clock: MockClock = None,
) -> tuple[PumpController, MockPlug, MockAlerter, MockClock]:
    clock = clock or MockClock()
    plug = MockPlug(online=online, turn_on_ok=turn_on_ok, turn_off_ok=turn_off_ok)
    alerter = MockAlerter()
    ctrl = PumpController(TEST_CONFIG, plug, alerter, clock=clock)
    return ctrl, plug, alerter, clock


def feed_sensors(ctrl: PumpController, clock: MockClock, a_pct: float, b_pct: float) -> None:
    """Inject sensor readings for both tanks."""
    ctrl.on_sensor_data("tank_a", {"level_pct": a_pct, "distance_cm": 100.0, "volume_l": a_pct * 200})
    ctrl.on_sensor_data("tank_b", {"level_pct": b_pct, "distance_cm": 100.0, "volume_l": b_pct * 200})


# ── Test: Initial state ───────────────────────────────────────────────────────

def test_initial_state_is_idle():
    ctrl, _, _, _ = make_controller()
    assert ctrl.state == PumpState.IDLE


# ── Test: IDLE → MIN_RUN (start condition) ────────────────────────────────────

@pytest.mark.asyncio
async def test_starts_pump_when_tank_a_full_and_tank_b_has_room():
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.MIN_RUN
    assert plug.on_calls == 1
    assert plug.is_on is True
    assert alerter.has_event("pump_start")


@pytest.mark.asyncio
async def test_does_not_start_when_tank_a_below_threshold():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=89.9, b_pct=40.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE
    assert plug.on_calls == 0


@pytest.mark.asyncio
async def test_does_not_start_when_tank_b_already_full():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=95.0, b_pct=90.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE
    assert plug.on_calls == 0


@pytest.mark.asyncio
async def test_does_not_start_at_exact_stop_high_threshold():
    """Tank B at exactly 90% — should NOT start (stop threshold is inclusive)."""
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=95.0, b_pct=90.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE


# ── Test: MIN_RUN behaviour ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stays_in_min_run_until_min_time_elapsed():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN
    assert ctrl.state == PumpState.MIN_RUN

    clock.advance(299)  # Just under 300s min run
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=50.0)  # Levels changed but not stop-worthy
    await ctrl._tick()
    assert ctrl.state == PumpState.MIN_RUN  # Still enforcing min run


@pytest.mark.asyncio
async def test_transitions_min_run_to_running_after_min_time():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN

    clock.advance(300)
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=55.0)
    await ctrl._tick()  # → RUNNING
    assert ctrl.state == PumpState.RUNNING


# ── Test: RUNNING → COOLDOWN (normal stops) ───────────────────────────────────

@pytest.mark.asyncio
async def test_stops_when_tank_a_reaches_low_threshold():
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN
    clock.advance(300)
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=45.0)  # Fresh data during min run
    await ctrl._tick()  # → RUNNING

    feed_sensors(ctrl, clock, a_pct=60.0, b_pct=75.0)  # Tank A at stop threshold
    await ctrl._tick()
    assert ctrl.state == PumpState.COOLDOWN
    assert plug.off_calls >= 1
    assert plug.is_on is False
    assert alerter.has_event("pump_stop")


@pytest.mark.asyncio
async def test_stops_when_tank_b_reaches_high_threshold():
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN
    clock.advance(300)
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=55.0)  # Fresh data during min run
    await ctrl._tick()  # → RUNNING

    feed_sensors(ctrl, clock, a_pct=75.0, b_pct=90.0)  # Tank B at stop threshold
    await ctrl._tick()
    assert ctrl.state == PumpState.COOLDOWN
    assert alerter.has_event("pump_stop")


# ── Test: COOLDOWN → IDLE ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_to_idle_after_cooldown():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN
    clock.advance(300)
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=50.0)  # Fresh data during min run
    await ctrl._tick()  # → RUNNING
    feed_sensors(ctrl, clock, a_pct=60.0, b_pct=75.0)
    await ctrl._tick()  # → COOLDOWN
    assert ctrl.state == PumpState.COOLDOWN

    clock.advance(600)  # Advance past cooldown
    feed_sensors(ctrl, clock, a_pct=60.0, b_pct=75.0)
    await ctrl._tick()  # → IDLE
    assert ctrl.state == PumpState.IDLE


@pytest.mark.asyncio
async def test_does_not_leave_cooldown_early():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN
    clock.advance(300)
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=50.0)  # Fresh data during min run
    await ctrl._tick()  # → RUNNING
    feed_sensors(ctrl, clock, a_pct=60.0, b_pct=75.0)
    await ctrl._tick()  # → COOLDOWN
    assert ctrl.state == PumpState.COOLDOWN

    clock.advance(599)  # Just under cooldown
    feed_sensors(ctrl, clock, a_pct=60.0, b_pct=75.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.COOLDOWN


# ── Test: Safety — absolute limits ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_fault_if_tank_a_critically_low_while_running():
    """Safety rule 1: NEVER pump if Tank A < 20%."""
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN

    feed_sensors(ctrl, clock, a_pct=19.9, b_pct=50.0)  # Tank A critically low
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert plug.off_calls >= 1
    assert alerter.has_event("fault")


@pytest.mark.asyncio
async def test_fault_if_tank_b_critically_full_while_running():
    """Safety rule 2: NEVER pump if Tank B > 95%."""
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN

    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=95.1)  # Tank B critically full
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert plug.off_calls >= 1
    assert alerter.has_event("fault")


@pytest.mark.asyncio
async def test_fault_on_max_runtime_exceeded():
    """Safety rule 3: Max pump runtime 30 min."""
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN (pump_started_at = t=0)
    clock.advance(300)
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=55.0)  # Fresh data during min run
    await ctrl._tick()  # → RUNNING

    # Advance total pump runtime past max (1800s). Pump started at t=0.
    clock.advance(1500)  # t = 300+1500 = 1800, pump runtime = 1800s >= 1800 → FAULT
    feed_sensors(ctrl, clock, a_pct=75.0, b_pct=70.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert plug.off_calls >= 1
    assert alerter.has_event("fault")


# ── Test: Safety — sensor timeout ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fault_on_sensor_timeout_from_idle():
    """Safety rule 4: No sensor data for 120s → FAULT."""
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)

    clock.advance(121)  # Advance past timeout without new readings
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert alerter.has_event("fault")


@pytest.mark.asyncio
async def test_fault_on_sensor_timeout_while_running():
    """Sensor dropout while pump is running → immediate FAULT, pump OFF."""
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN
    clock.advance(300)
    feed_sensors(ctrl, clock, a_pct=85.0, b_pct=55.0)
    await ctrl._tick()  # → RUNNING

    clock.advance(121)  # No new readings — sensor timed out
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert plug.off_calls >= 1


@pytest.mark.asyncio
async def test_no_fault_when_sensor_data_is_fresh():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)
    clock.advance(119)  # Just under timeout
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)  # Fresh data
    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE  # No fault


# ── Test: Plug failure ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fault_when_plug_fails_to_turn_on():
    """Safety rule 5: Plug turn-on failure → FAULT."""
    ctrl, plug, alerter, clock = make_controller(turn_on_ok=False)
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert alerter.has_event("fault")


# ── Test: FAULT auto-clear ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fault_auto_clears_after_fault_clear_seconds():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)
    clock.advance(121)  # Trigger sensor timeout
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT

    # Provide fresh safe readings
    clock.advance(1)
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)

    clock.advance(300)  # Advance past fault_clear_seconds
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE


@pytest.mark.asyncio
async def test_fault_does_not_clear_early():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)
    clock.advance(121)
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT

    clock.advance(299)  # Just under fault_clear_seconds
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT


@pytest.mark.asyncio
async def test_fault_sends_alert_only_once():
    """Repeated ticks in FAULT should not spam alerts."""
    ctrl, plug, alerter, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=50.0, b_pct=50.0)
    clock.advance(121)

    for _ in range(5):
        await ctrl._tick()

    fault_alerts = [e for e in alerter.events if e["event"] == "fault"]
    assert len(fault_alerts) == 1, f"Expected 1 fault alert, got {len(fault_alerts)}"


# ── Test: get_status snapshot ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_status_reflects_current_state():
    ctrl, plug, _, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()  # → MIN_RUN

    status = ctrl.get_status()
    assert status["state"] == "MIN_RUN"
    assert status["pump_on"] is True
    assert status["tank_a_pct"] == 92.0
    assert status["tank_b_pct"] == 40.0
    assert status["runtime_s"] is not None


@pytest.mark.asyncio
async def test_get_status_fault_reason():
    ctrl, plug, _, clock = make_controller(turn_on_ok=False)
    feed_sensors(ctrl, clock, a_pct=92.0, b_pct=40.0)
    await ctrl._tick()
    status = ctrl.get_status()
    assert status["state"] == "FAULT"
    assert "Plug failed" in status["fault_reason"]


# ── Test: on_sensor_data robustness ──────────────────────────────────────────

def test_ignores_malformed_sensor_payload():
    ctrl, _, _, clock = make_controller()
    ctrl.on_sensor_data("tank_a", {"bad_key": "no_level"})
    assert ctrl._tank_a is None  # Should not have updated


def test_ignores_unknown_sensor_id():
    ctrl, _, _, clock = make_controller()
    ctrl.on_sensor_data("tank_c", {"level_pct": 50.0, "distance_cm": 100.0})
    # Should not crash
    assert ctrl._tank_a is None


def test_sensor_data_thread_safe():
    """Simulate concurrent sensor updates (basic smoke test)."""
    import threading
    ctrl, _, _, clock = make_controller()
    errors = []

    def update():
        try:
            for _ in range(100):
                ctrl.on_sensor_data("tank_a", {"level_pct": 50.0, "distance_cm": 100.0, "volume_l": 10000})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=update) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread safety errors: {errors}"
