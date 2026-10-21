"""Start confirmation: the pump must not run on a single sensor reading.

On 2026-09-24 the Tank A JSN-SR04T reported its dead-zone floor (22.7 cm) three times
in a row. With SENSOR_MIN_CM at 21.0 those passed validation and became level_pct 100%,
which cleared the 94% start threshold and ran the pump for 26 minutes on a tank that was
actually at ~68%. Two defences were added:

  1. firmware — SENSOR_MIN_CM raised to a physically plausible 45 cm, so dead-zone
     readings are rejected at source (see docs/07-calibration.md);
  2. controller — this one: the start condition must hold across several *distinct*
     readings before the plug is switched on.

No broker and no plug: these use the mock plug, so they are safe to run anywhere.
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))   # reuse the mocks in test_pump_control

from pump_control import PumpController, PumpState  # noqa: E402
from test_pump_control import (  # noqa: E402
    MockAlerter, MockClock, MockPlug, TEST_CONFIG, feed_sensors,
)

CONFIRM_N = 3


def make_controller(confirm_n: int = CONFIRM_N):
    clock = MockClock()
    cfg = {"pump": {**TEST_CONFIG["pump"], "start_confirm_readings": confirm_n}}
    plug = MockPlug()
    ctrl = PumpController(cfg, plug, MockAlerter(), clock=clock)
    return ctrl, plug, clock


async def feed_tick(ctrl, clock, a_pct, b_pct=40.0):
    """One distinct reading followed by one state-machine tick."""
    feed_sensors(ctrl, clock, a_pct=a_pct, b_pct=b_pct)
    await ctrl._tick()


@pytest.mark.asyncio
async def test_single_qualifying_reading_does_not_start_the_pump():
    ctrl, plug, clock = make_controller()
    await feed_tick(ctrl, clock, 100.0)
    assert ctrl.state == PumpState.IDLE
    assert plug.on_calls == 0


@pytest.mark.asyncio
async def test_two_of_three_readings_still_do_not_start():
    ctrl, plug, clock = make_controller()
    for _ in range(CONFIRM_N - 1):
        await feed_tick(ctrl, clock, 100.0)
    assert ctrl.state == PumpState.IDLE
    assert plug.on_calls == 0


@pytest.mark.asyncio
async def test_pump_starts_once_the_condition_is_confirmed():
    ctrl, plug, clock = make_controller()
    for _ in range(CONFIRM_N):
        await feed_tick(ctrl, clock, 100.0)
    assert ctrl.state == PumpState.MIN_RUN
    assert plug.on_calls == 1


@pytest.mark.asyncio
async def test_repeated_ticks_on_one_reading_are_not_evidence():
    """Ticks run every 5 s but readings arrive every 30 s.

    Re-evaluating the same reading six times must not confirm anything, or the
    confirmation window would collapse to a few seconds.
    """
    ctrl, plug, clock = make_controller()
    feed_sensors(ctrl, clock, a_pct=100.0, b_pct=40.0)
    for _ in range(6):
        await ctrl._tick()
    assert ctrl.state == PumpState.IDLE
    assert plug.on_calls == 0


@pytest.mark.asyncio
async def test_a_disagreeing_reading_resets_the_streak():
    ctrl, plug, clock = make_controller()
    await feed_tick(ctrl, clock, 100.0)
    await feed_tick(ctrl, clock, 100.0)
    await feed_tick(ctrl, clock, 68.0)          # normal level breaks the run
    await feed_tick(ctrl, clock, 100.0)         # streak restarts at 1
    assert ctrl.state == PumpState.IDLE
    assert plug.on_calls == 0


@pytest.mark.asyncio
async def test_replays_the_2026_09_24_glitch_without_starting():
    """The real sequence: two dead-zone readings surrounded by normal ones.

    With SENSOR_MIN_CM fixed these never reach the controller, but the controller must
    survive them on its own — Tank B will use a different sensor and mounting.
    """
    ctrl, plug, clock = make_controller()
    for level in (68.2, 72.1, 100.0, 100.0, 72.6, 58.5):
        await feed_tick(ctrl, clock, level)
    assert ctrl.state == PumpState.IDLE
    assert plug.on_calls == 0


@pytest.mark.asyncio
async def test_sustained_genuine_fill_still_starts():
    """The guard must not block a real full tank."""
    ctrl, plug, clock = make_controller()
    for level in (94.5, 95.0, 96.0):
        await feed_tick(ctrl, clock, level)
    assert ctrl.state == PumpState.MIN_RUN
    assert plug.on_calls == 1


def test_shipped_config_requires_more_than_one_reading():
    """Guard against the example config drifting back to a single-reading start."""
    example = Path(__file__).parent.parent / "config" / "config.yaml.example"
    cfg = yaml.safe_load(example.read_text())
    assert cfg["pump"].get("start_confirm_readings", 1) >= 2
