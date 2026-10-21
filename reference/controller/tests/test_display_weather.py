"""
Tests for the weather parser, the e-ink frame packer/renderer, the display schedule and
the device-facing HTTP protocol. None of these touch MQTT, the plug or the network, so
they are safe to run anywhere (including on the production Pi).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from eink_display import (  # noqa: E402
    BLACK, BLUE, FRAME_BYTES, H, W, WHITE, DisplayService, Schedule, pack, render, unpack,
)
from weather import describe, parse_open_meteo  # noqa: E402

TZ = ZoneInfo("UTC")


def at(h, m=0, day=1):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


# ── Weather ────────────────────────────────────────────────────────────────

def _raw(past=2, future=4):
    n = past + future
    dates = [f"2026-10-{i + 1:02d}" for i in range(n)]
    return {
        "current": {"time": f"{dates[past]}T13:45", "temperature_2m": 22.4, "relative_humidity_2m": 44,
                    "precipitation": 0.0, "weather_code": 61, "wind_speed_10m": 12.2, "is_day": 1},
        "daily": {
            "time": dates,
            "weather_code": [61] * n,
            "temperature_2m_max": [25.0] * n,
            "temperature_2m_min": [12.0] * n,
            "precipitation_sum": [1.0, 2.0, 0.0, 5.5, None, 3.0][:n],
            "precipitation_probability_max": [50] * n,
            "precipitation_hours": [2.0] * n,
            "sunrise": [d + "T05:30" for d in dates],
            "sunset": [d + "T17:45" for d in dates],
        },
        "hourly": {
            "time": [f"{dates[past]}T{h:02d}:00" for h in range(24)] + [f"{dates[past + 1]}T{h:02d}:00" for h in range(24)],
            "precipitation": [0.1] * 48,
            "precipitation_probability": [10] * 48,
        },
    }


def test_parse_splits_past_and_future():
    w = parse_open_meteo(_raw(), past_days=2)
    assert [d["date"] for d in w["daily"]] == ["2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]
    assert w["rain_past_7d_mm"] == 3.0
    # None precipitation is treated as 0, not a crash
    assert w["rain_next_7d_mm"] == 8.5
    assert w["current"]["label"] == "Light rain" and w["current"]["icon"] == "rain"


def test_parse_hourly_starts_at_current_hour():
    w = parse_open_meteo(_raw(), past_days=2)
    assert w["hourly"][0]["time"] == "2026-10-03T13:00"
    assert len(w["hourly"]) == 24


def test_describe_unknown_code():
    assert describe(None) == ("Unknown", "cloud")
    assert describe(1234) == ("Unknown", "cloud")
    assert describe(95)[1] == "storm"


# ── Frame packing ──────────────────────────────────────────────────────────

def test_pack_layout_even_x_high_nibble():
    from PIL import Image
    im = Image.new("P", (W, H), WHITE)
    im.putpixel((0, 0), BLACK)   # even x → high nibble of byte 0
    im.putpixel((1, 0), BLUE)    # odd x  → low nibble of byte 0
    im.putpixel((799, 479), BLACK)
    fb = pack(im)
    assert len(fb) == FRAME_BYTES == 192_000
    assert fb[0] == (BLACK << 4) | BLUE
    assert fb[1] == 0x11  # white, white
    assert fb[-1] == (WHITE << 4) | BLACK


def test_render_uses_only_panel_colours_and_roundtrips():
    status = {"state": "IDLE", "pump_on": False,
              "tank_a": {"level_pct": 91.4, "volume_l": 4405, "age_s": 5},
              "tank_b": {"level_pct": 50.0, "volume_l": 17548, "age_s": 5}}
    weather = {"available": True, "stale": False, "location": "Your Town", **parse_open_meteo(_raw(), 2)}
    im = render(status, weather, {"tanks": {"tank_b": {"simulated_pct": 50}}}, at(8), {"battery_pct": 80})
    used = set(im.tobytes())
    assert used <= {0, 1, 2, 3, 5, 6}, f"non-panel indices {used}"
    assert unpack(pack(im)).tobytes() == im.tobytes()


@pytest.mark.parametrize("weather", [None, {"available": False}])
def test_render_survives_missing_data(weather):
    status = {"state": "FAULT", "fault_reason": "Tank A sensor timeout (no reading for 130 s)",
              "pump_on": False, "tank_a": None, "tank_b": {}}
    im = render(status, weather, {}, at(8))
    assert im.size == (W, H)


# ── Schedule: 06:00, 08:00 … 20:00 ─────────────────────────────────────────

S = Schedule(start_hour=6, end_hour=21, interval_hours=2, grace_minutes=10)


def test_slots():
    assert [s.hour for s in S.slots_on(at(0).date())] == [6, 8, 10, 12, 14, 16, 18, 20]


@pytest.mark.parametrize("now,due", [
    (at(5, 0), False),
    (at(5, 52), True),     # early wake within grace counts as the 06:00 slot
    (at(6, 0), True),
    (at(13, 7), True),
    (at(20, 30), True),
    (at(20, 55), False),   # 21:05 with grace — window closed
    (at(23, 0), False),
    (at(2, 0), False),
])
def test_is_due(now, due):
    assert S.is_due(now) is due


@pytest.mark.parametrize("now,expected", [
    (at(5, 0), at(6)),
    (at(5, 55), at(8)),        # woke early for 06:00 → next target is 08:00, not 06:00 again
    (at(6, 1), at(8)),
    (at(19, 58), at(6, day=2)),  # 20:00 slot taken early → next is tomorrow 06:00
    (at(20, 2), at(6, day=2)),
    (at(23, 30), at(6, day=2)),
])
def test_next_slot(now, expected):
    assert S.next_slot(now) == expected


def test_overnight_sleep_length():
    assert S.seconds_until_next(at(20, 1)) == int(timedelta(hours=9, minutes=59).total_seconds())
    assert S.seconds_until_next(at(5, 59)) >= 60


# ── Device protocol ────────────────────────────────────────────────────────

class _FrozenSchedule(Schedule):
    def __init__(self, now):
        super().__init__(start_hour=6, end_hour=21, interval_hours=2)
        object.__setattr__(self, "_now", now)

    def now(self):
        return self._now


def _service(tmp_path, now):
    status = {"state": "IDLE", "pump_on": False,
              "tank_a": {"level_pct": 70, "volume_l": 3374, "age_s": 3},
              "tank_b": {"level_pct": 40, "volume_l": 14000, "age_s": 3}}
    svc = DisplayService({}, lambda: status, None, tmp_path)
    svc._schedule = _FrozenSchedule(now)
    return svc


def test_night_wake_returns_304(tmp_path):
    svc = _service(tmp_path, at(23, 0))
    r = svc.decide('"abc"', force=False)
    assert r["send"] is False
    assert r["next_wake_s"] == 7 * 3600


def test_first_boot_always_gets_a_frame_even_at_night(tmp_path):
    svc = _service(tmp_path, at(23, 0))
    r = svc.decide(None, force=False)
    assert r["send"] is True and len(r["frame"]) == FRAME_BYTES


def test_button_press_forces_frame(tmp_path):
    svc = _service(tmp_path, at(2, 0))
    assert svc.decide('"abc"', force=True)["send"] is True


def test_same_etag_in_window_is_304(tmp_path):
    svc = _service(tmp_path, at(8, 0))
    first = svc.decide('"old"', force=False)
    assert first["send"] is True
    again = svc.decide(f'"{first["etag"]}"', force=False)
    assert again["send"] is False


def test_telemetry_is_filtered_and_persisted(tmp_path):
    svc = _service(tmp_path, at(8, 0))
    svc.record_telemetry({"battery_pct": 81, "rssi": -60, "evil": "x" * 10})
    st = svc.get_status()
    assert st["telemetry"]["battery_pct"] == 81 and "evil" not in st["telemetry"]
    again = DisplayService({}, lambda: {}, None, tmp_path)
    assert again.get_status()["telemetry"]["rssi"] == -60


# ── Portrait ───────────────────────────────────────────────────────────────

def _status():
    return {"state": "IDLE", "pump_on": False,
            "tank_a": {"level_pct": 91.4, "volume_l": 4405, "age_s": 5},
            "tank_b": {"level_pct": 50.0, "volume_l": 17548, "age_s": 5}}


def test_portrait_page_and_panel():
    from eink_display import to_panel
    weather = {"available": True, "stale": False, "location": "Your Town", **parse_open_meteo(_raw(), 2)}
    solar = {"power_kw": 4.7, "today_kwh": 4.6, "grid_kw": -0.4}
    page = render(_status(), weather, {}, at(8), {"battery_pct": 80}, solar, orientation="portrait")
    assert page.size == (480, 800)
    assert set(page.tobytes()) <= {0, 1, 2, 3, 5, 6}
    panel = to_panel(page)
    assert panel.size == (W, H) and len(pack(panel)) == FRAME_BYTES


@pytest.mark.parametrize("turn,expected_xy", [
    ("cw", (0, 479)),    # frame turned clockwise: page top-left lands at panel bottom-left
    ("ccw", (799, 0)),   # counter-clockwise: page top-left lands at panel top-right
])
def test_portrait_rotation_direction(turn, expected_xy):
    from PIL import Image
    from eink_display import to_panel
    page = Image.new("P", (480, 800), WHITE)
    page.putpixel((0, 0), BLUE)  # mark the page's top-left corner
    panel = to_panel(page, turn)
    assert panel.getpixel(expected_xy) == BLUE


def test_landscape_page_passes_through():
    from eink_display import to_panel
    page = render(_status(), None, {}, at(8))
    assert to_panel(page) is page


# ── Daily summary (icon from cloud cover, not worst-hour code) ─────────────

from weather import daily_summary  # noqa: E402


@pytest.mark.parametrize("code,rain,prob,cloud,expected_icon", [
    (3, 0.0, 0, 28, "sun"),        # 2026-10-03: "overcast" code, 28% cloud, 12 h sun → mostly sunny
    (3, 0.0, 18, 52, "partly"),
    (51, 0.7, 18, 50, "partly"),   # one drizzly hour, 0.7 mm → not a rain day
    (95, 6.8, 78, 67, "storm"),    # real rain keeps the precipitation icon
    (61, 0.4, 60, 90, "rain"),     # high chance alone is enough
    (45, 0.0, 0, 80, "cloud"),     # a foggy hour at dawn doesn't make a "fog" day
    (3, 0.0, 0, None, "cloud"),    # no cloud data → fall back to the code
])
def test_daily_summary(code, rain, prob, cloud, expected_icon):
    assert daily_summary(code, rain, prob, cloud)[1] == expected_icon


def test_parse_uses_cloud_cover_when_present():
    raw = _raw()
    raw["daily"]["weather_code"] = [3] * 6
    raw["daily"]["precipitation_sum"] = [0.0] * 6
    raw["daily"]["precipitation_probability_max"] = [0] * 6
    raw["daily"]["cloud_cover_mean"] = [20, 20, 20, 50, 85, 10]
    w = parse_open_meteo(raw, past_days=2)
    assert [d["icon"] for d in w["daily"]] == ["sun", "partly", "cloud", "sun"]
