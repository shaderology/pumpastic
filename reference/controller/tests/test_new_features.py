"""
Tests for sensor stuck detection, dry-run detection, and web dashboard API.

Run:
    cd controller
    python -m pytest tests/test_new_features.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pump_control import PumpController, PumpState


# ── Shared helpers (same pattern as test_pump_control.py) ────────────────────

class MockClock:
    def __init__(self, start: datetime = None):
        self._now = start or datetime(2026, 1, 1, 12, 0, 0)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class MockPlug:
    def __init__(self, turn_on_ok: bool = True):
        self._turn_on_ok = turn_on_ok
        self.is_on = False
        self.on_calls = 0
        self.off_calls = 0

    async def turn_on(self) -> bool:
        self.on_calls += 1
        if self._turn_on_ok:
            self.is_on = True
        return self._turn_on_ok

    async def turn_off(self) -> bool:
        self.off_calls += 1
        self.is_on = False
        return True

    async def is_online(self) -> bool:
        return True

    async def get_power_watts(self) -> Optional[float]:
        return 550.0 if self.is_on else 0.0


class MockAlerter:
    def __init__(self):
        self.events: List[dict] = []

    def send(self, event: str, message: str, title: str = None) -> None:
        self.events.append({"event": event, "message": message})

    def has_event(self, event: str) -> bool:
        return any(e["event"] == event for e in self.events)


BASE_CONFIG = {
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
        # See the note in test_pump_control.py: these tests drive the pump with a single
        # reading. Start confirmation is covered by test_start_confirmation.py.
        "start_confirm_readings": 1,
    }
}


def make_ctrl(extra_pump_cfg: dict = None, clock: MockClock = None):
    clock = clock or MockClock()
    cfg = {"pump": {**BASE_CONFIG["pump"], **(extra_pump_cfg or {})}}
    plug = MockPlug()
    alerter = MockAlerter()
    ctrl = PumpController(cfg, plug, alerter, clock=clock)
    return ctrl, plug, alerter, clock


def feed(ctrl, a_pct: float, b_pct: float, a_dist: float = 100.0, b_dist: float = 100.0):
    ctrl.on_sensor_data("tank_a", {"level_pct": a_pct, "distance_cm": a_dist, "volume_l": a_pct * 200})
    ctrl.on_sensor_data("tank_b", {"level_pct": b_pct, "distance_cm": b_dist, "volume_l": b_pct * 200})


# ═══════════════════════════════════════════════════════════════════════════════
# Sensor stuck detection
# ═══════════════════════════════════════════════════════════════════════════════

STUCK_CFG = {"stuck_detection_count": 5, "stuck_detection_delta_cm": 0.5}


def test_stuck_detection_disabled_by_default():
    """With stuck_detection_count: 0 (default), identical readings must not fault."""
    ctrl, _, alerter, clock = make_ctrl()
    for _ in range(20):
        feed(ctrl, 50.0, 50.0, a_dist=150.0, b_dist=150.0)

    # Still no fault — detection is off
    assert not alerter.has_event("fault")


@pytest.mark.asyncio
async def test_stuck_faults_after_n_identical_readings():
    ctrl, _, alerter, clock = make_ctrl(STUCK_CFG)

    # Feed 5 identical distance readings for Tank A
    for _ in range(5):
        feed(ctrl, 50.0, 50.0, a_dist=150.00, b_dist=80.0)

    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert alerter.has_event("fault")
    assert "stuck" in ctrl.get_status()["fault_reason"].lower()


@pytest.mark.asyncio
async def test_stuck_faults_when_readings_within_delta():
    """Readings that vary by ≤ delta_cm (0.5) should still be flagged."""
    ctrl, _, alerter, _ = make_ctrl(STUCK_CFG)

    # 5 readings all within 0.5 cm range of each other
    dists = [149.8, 150.0, 150.2, 149.9, 150.1]
    for d in dists:
        feed(ctrl, 50.0, 50.0, a_dist=d, b_dist=80.0)

    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT


@pytest.mark.asyncio
async def test_no_stuck_fault_when_readings_vary_enough():
    """If readings vary by more than delta_cm, no fault."""
    ctrl, _, alerter, _ = make_ctrl(STUCK_CFG)

    # Both tanks vary — neither should trigger stuck detection
    a_dists = [140.0, 145.0, 150.0, 155.0, 160.0]  # 20 cm spread
    b_dists = [80.0,  85.0,  90.0,  95.0, 100.0]
    for a, b in zip(a_dists, b_dists):
        feed(ctrl, 50.0, 50.0, a_dist=a, b_dist=b)

    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE


@pytest.mark.asyncio
async def test_stuck_faults_on_tank_b_too():
    """Tank B stuck readings also trigger a fault."""
    ctrl, _, alerter, _ = make_ctrl(STUCK_CFG)

    # Tank A varies (no fault), Tank B stuck at 200 cm
    a_dists = [80.0, 85.0, 90.0, 95.0, 100.0]
    for i, a in enumerate(a_dists):
        feed(ctrl, 50.0, 50.0, a_dist=a, b_dist=200.0)

    await ctrl._tick()
    assert ctrl.state == PumpState.FAULT
    assert "Tank B" in ctrl.get_status()["fault_reason"]


@pytest.mark.asyncio
async def test_stuck_does_not_fault_before_n_readings():
    """Need exactly N readings before the check fires."""
    ctrl, _, alerter, _ = make_ctrl(STUCK_CFG)

    # Only 4 readings (need 5)
    for _ in range(4):
        feed(ctrl, 50.0, 50.0, a_dist=150.0, b_dist=80.0)

    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE


@pytest.mark.asyncio
async def test_stuck_clears_when_reading_changes():
    """After N stuck readings, a different reading should reset the window."""
    ctrl, _, alerter, _ = make_ctrl(STUCK_CFG)

    # Tank B varies throughout so it never triggers stuck detection
    b_dists = [80.0, 85.0, 90.0, 95.0, 100.0]

    # 4 identical Tank A readings, then one clearly different
    for i in range(4):
        feed(ctrl, 50.0, 50.0, a_dist=150.0, b_dist=b_dists[i])
    feed(ctrl, 50.0, 50.0, a_dist=160.0, b_dist=b_dists[4])  # Breaks the run

    await ctrl._tick()
    assert ctrl.state == PumpState.IDLE  # Spread is now 10 cm — no fault


# ═══════════════════════════════════════════════════════════════════════════════
# Dry-run detection
# ═══════════════════════════════════════════════════════════════════════════════

DRY_RUN_CFG = {"dry_run_watt_threshold": 50.0, "dry_run_timeout_seconds": 30.0}


def test_dry_run_disabled_by_default():
    """With dry_run_watt_threshold: 0 (default), low power must not fault."""
    ctrl, _, alerter, clock = make_ctrl()
    ctrl.on_power_reading(0.0)  # Should be a no-op
    assert not alerter.has_event("fault")


@pytest.mark.asyncio
async def test_dry_run_faults_after_timeout():
    """Pump running + low power for ≥ dry_run_timeout_seconds → FAULT."""
    ctrl, plug, alerter, clock = make_ctrl(DRY_RUN_CFG)

    # Get into MIN_RUN
    feed(ctrl, 92.0, 40.0)
    await ctrl._tick()
    assert ctrl.state == PumpState.MIN_RUN

    # Pump is running — report low power, advance past timeout
    ctrl.on_power_reading(10.0)  # Below 50W threshold
    clock.advance(30)
    feed(ctrl, 88.0, 45.0)
    await ctrl._tick()

    assert ctrl.state == PumpState.FAULT
    assert plug.off_calls >= 1
    assert alerter.has_event("fault")
    assert "dry-run" in ctrl.get_status()["fault_reason"].lower()


@pytest.mark.asyncio
async def test_dry_run_no_fault_before_timeout():
    """Low power for less than timeout must not fault."""
    ctrl, _, alerter, clock = make_ctrl(DRY_RUN_CFG)
    feed(ctrl, 92.0, 40.0)
    await ctrl._tick()  # → MIN_RUN

    ctrl.on_power_reading(10.0)
    clock.advance(29)  # Just under 30s
    feed(ctrl, 88.0, 45.0)
    await ctrl._tick()

    assert ctrl.state == PumpState.MIN_RUN  # Still running


@pytest.mark.asyncio
async def test_dry_run_resets_when_power_recovers():
    """Power rising above threshold mid-run must cancel the dry-run timer."""
    ctrl, _, alerter, clock = make_ctrl(DRY_RUN_CFG)
    feed(ctrl, 92.0, 40.0)
    await ctrl._tick()  # → MIN_RUN

    ctrl.on_power_reading(10.0)   # Low power starts timer
    clock.advance(20)
    ctrl.on_power_reading(550.0)  # Power recovered — reset timer
    clock.advance(20)
    feed(ctrl, 88.0, 45.0)
    await ctrl._tick()

    # Total elapsed since first low-power = 40s, but timer was reset at 20s
    assert ctrl.state == PumpState.MIN_RUN  # No fault


@pytest.mark.asyncio
async def test_dry_run_no_fault_when_pump_off():
    """Low power reading when pump is IDLE must not fault."""
    ctrl, _, alerter, clock = make_ctrl(DRY_RUN_CFG)
    feed(ctrl, 50.0, 50.0)

    ctrl.on_power_reading(0.0)
    clock.advance(60)
    feed(ctrl, 50.0, 50.0)
    await ctrl._tick()

    assert ctrl.state == PumpState.IDLE


# ═══════════════════════════════════════════════════════════════════════════════
# Web dashboard
# ═══════════════════════════════════════════════════════════════════════════════

def _make_dashboard(get_status_fn):
    """Helper: construct a WebDashboard with minimal stubs for unit testing."""
    from unittest.mock import MagicMock
    from web_dashboard import WebDashboard

    stub_mqtt = MagicMock()
    stub_mqtt.publish_simulated_sensor = MagicMock()
    stub_pump = MagicMock()
    stub_pump.clear_fault = MagicMock(return_value=False)

    return WebDashboard(
        get_status_fn=get_status_fn,
        config={"mqtt": {"broker": "localhost", "port": 1883}, "tanks": {}, "pump": {}, "alerts": {}},
        mqtt_client=stub_mqtt,
        plug=MagicMock(),
        loop=None,
        pump_controller=stub_pump,
        get_device_status_fn=lambda: {"mqtt_connected": False, "last_checked": None},
    )


def test_dashboard_api_status():
    """GET /api/status returns controller status as JSON."""
    status = {
        "state": "IDLE",
        "pump_on": False,
        "fault_reason": "",
        "state_since": "2026-01-01T12:00:00",
        "runtime_s": None,
        "tank_a": {"level_pct": 75.0, "distance_cm": 50.0, "volume_l": 15000, "age_s": 5},
        "tank_b": {"level_pct": 40.0, "distance_cm": 120.0, "volume_l": 8000, "age_s": 5},
        "tank_a_pct": 75.0,
        "tank_b_pct": 40.0,
    }
    dash = _make_dashboard(lambda: status)
    client = dash._app.test_client()

    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["state"] == "IDLE"
    assert data["tank_a_pct"] == 75.0
    assert data["pump_on"] is False


def test_dashboard_index_renders():
    """GET / returns the HTML dashboard page."""
    dash = _make_dashboard(lambda: {"state": "IDLE"})
    client = dash._app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Pumpastic" in resp.data
    assert b"/api/status" in resp.data


def test_dashboard_api_fault_state():
    """Fault reason is surfaced correctly via the API."""
    status = {
        "state": "FAULT",
        "pump_on": False,
        "fault_reason": "Tank A sensor stuck: 5 readings within 0.10 cm",
        "state_since": "2026-01-01T12:05:00",
        "runtime_s": None,
        "tank_a": None,
        "tank_b": None,
        "tank_a_pct": 50.0,
        "tank_b_pct": 50.0,
    }
    dash = _make_dashboard(lambda: status)
    client = dash._app.test_client()

    data = client.get("/api/status").get_json()
    assert data["state"] == "FAULT"
    assert "stuck" in data["fault_reason"]
