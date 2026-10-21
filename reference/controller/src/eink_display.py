"""
Server-side renderer and schedule for the e-ink status display.

Hardware: Waveshare ESP32-S3-PhotoPainter — 7.3" Spectra E6 panel, 800×480, six
colours, ~25 s full refresh with no partial refresh. The device firmware is a thin
client: wake → POST telemetry → GET /display/frame.bin → push to panel → deep sleep for
the number of seconds in the X-Next-Wake-S header. All layout lives here, so the
design can change without reflashing.

Frame format (what the panel's 0x10 command takes): 800×480, 4 bits per pixel, two
pixels per byte, even x in the HIGH nibble, row-major, 192 000 bytes. Colour indices
are the panel's own: black 0, white 1, yellow 2, red 3, blue 5, green 6 (4 is unused).
We draw straight into a palette ("P") image using those indices, with font
anti-aliasing off, so there is nothing to quantise and no dither speckle on text.

Schedule (config.yaml → display): a refresh every `refresh_interval_hours` from
`active_start_hour` up to (not including) `active_end_hour`. With the defaults
6 / 21 / 2 that is 06:00, 08:00 … 20:00 — eight refreshes a day, none overnight.
The server owns the clock: it tells the device how long to sleep, and answers a
scheduled wake outside the window with 304 so the panel doesn't flicker at night.

Solar: the layout reserves the lower-right panel. `solar` is None until a data
source exists (see docs/04-protocols.md), and the panel then shows a placeholder.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("pumpastic.display")

W, H = 800, 480
FRAME_BYTES = W * H // 2

# Panel colour indices
BLACK, WHITE, YELLOW, RED, BLUE, GREEN = 0, 1, 2, 3, 5, 6

# RGB used only for the PNG preview. Index 4 is unused by the panel; it is given a
# colour nothing draws with so a stray 4 is obvious in the preview.
_PREVIEW_PALETTE = [
    0, 0, 0,        # 0 black
    255, 255, 255,  # 1 white
    235, 200, 0,    # 2 yellow
    200, 30, 30,    # 3 red
    255, 0, 255,    # 4 unused
    30, 70, 190,    # 5 blue
    40, 140, 60,    # 6 green
]

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "AtkinsonHyperlegible-Bold.ttf" if bold else "AtkinsonHyperlegible-Regular.ttf"
    try:
        return ImageFont.truetype(str(_FONT_DIR / name), size)
    except OSError:
        log.warning("Font %s missing — falling back to Pillow default", name)
        return ImageFont.load_default(size)


# ── Schedule ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Schedule:
    start_hour: int = 6
    end_hour: int = 21
    interval_hours: int = 2
    tz: str = "UTC"
    # A device whose RTC runs a little fast wakes early. Treat a wake up to this many
    # minutes before a slot as belonging to that slot.
    grace_minutes: int = 10

    @classmethod
    def from_config(cls, config: dict) -> "Schedule":
        c = config.get("display") or {}
        tz = c.get("timezone") or (config.get("weather") or {}).get("timezone") or cls.tz
        return cls(
            start_hour=int(c.get("active_start_hour", cls.start_hour)),
            end_hour=int(c.get("active_end_hour", cls.end_hour)),
            interval_hours=max(1, int(c.get("refresh_interval_hours", cls.interval_hours))),
            tz=str(tz),
            grace_minutes=int(c.get("grace_minutes", cls.grace_minutes)),
        )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def slots_on(self, day: date) -> list[datetime]:
        z = self.zone
        return [datetime(day.year, day.month, day.day, h, tzinfo=z)
                for h in range(self.start_hour, self.end_hour, self.interval_hours)]

    def now(self) -> datetime:
        return datetime.now(self.zone)

    def is_due(self, now: datetime) -> bool:
        """True if `now` (plus grace) falls inside a slot's interval within today's window."""
        probe = now + timedelta(minutes=self.grace_minutes)
        slots = self.slots_on(probe.date())
        if not slots:
            return False
        window_end = slots[-1] + timedelta(hours=self.interval_hours)
        window_end = min(window_end, slots[0].replace(hour=0) + timedelta(hours=self.end_hour))
        return slots[0] <= probe < window_end

    def next_slot(self, now: datetime) -> datetime:
        """First slot strictly after now + grace (so an early wake doesn't re-target its own slot)."""
        probe = now + timedelta(minutes=self.grace_minutes)
        for offset in range(0, 3):
            for s in self.slots_on(probe.date() + timedelta(days=offset)):
                if s > probe:
                    return s
        return probe + timedelta(hours=self.interval_hours)  # degenerate config

    def seconds_until_next(self, now: datetime) -> int:
        return max(60, int((self.next_slot(now) - now).total_seconds()))


# ── Drawing helpers ─────────────────────────────────────────────────────────

def _text(d: ImageDraw.ImageDraw, xy, s: str, size: int, fill=BLACK, bold=False, anchor="la"):
    d.text(xy, s, font=_font(size, bold), fill=fill, anchor=anchor)


def _fit(s: str, size: int, bold: bool, max_w: int) -> int:
    """Largest font size ≤ size that renders `s` within max_w pixels."""
    while size > 10 and _font(size, bold).getlength(s) > max_w:
        size -= 1
    return size


def _ellipsize(s: str, size: int, bold: bool, max_w: int) -> str:
    """Trim `s` with an ellipsis so it fits max_w at this size."""
    f = _font(size, bold)
    if f.getlength(s) <= max_w:
        return s
    while s and f.getlength(s + "…") > max_w:
        s = s[:-1]
    return s.rstrip() + "…"


def _cloud(d, cx, cy, s, outline=3):
    """Cloud centred on (cx, cy) with width ≈ s. Outline drawn by over-painting."""
    parts = [
        (cx - 0.30 * s, cy + 0.02 * s, 0.22 * s),
        (cx - 0.02 * s, cy - 0.12 * s, 0.28 * s),
        (cx + 0.26 * s, cy + 0.04 * s, 0.20 * s),
    ]
    base = (cx - 0.48 * s, cy + 0.02 * s, cx + 0.46 * s, cy + 0.24 * s)
    for colour, grow in ((BLACK, outline), (WHITE, 0)):
        for x, y, r in parts:
            d.ellipse((x - r - grow, y - r - grow, x + r + grow, y + r + grow), fill=colour)
        d.rounded_rectangle((base[0] - grow, base[1] - grow, base[2] + grow, base[3] + grow),
                            radius=0.11 * s + grow, fill=colour)


def _sun(d, cx, cy, r, rays=True):
    if rays:
        import math
        for i in range(8):
            a = i * math.pi / 4
            x1, y1 = cx + math.cos(a) * (r + 5), cy + math.sin(a) * (r + 5)
            x2, y2 = cx + math.cos(a) * (r + 13), cy + math.sin(a) * (r + 13)
            d.line((x1, y1, x2, y2), fill=BLACK, width=4)
    d.ellipse((cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3), fill=BLACK)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=YELLOW)


def draw_weather_icon(d: ImageDraw.ImageDraw, icon: str, cx: int, cy: int, size: int = 64) -> None:
    """Weather glyph from primitives, in panel colours. `icon` uses weather.py's keys."""
    s = size
    if icon == "sun":
        _sun(d, cx, cy, int(s * 0.22))
        return
    if icon in ("partly", "showers"):
        _sun(d, cx - int(s * 0.18), cy - int(s * 0.16), int(s * 0.16))
        _cloud(d, cx + int(s * 0.06), cy + int(s * 0.08), int(s * 0.80))
    elif icon == "fog":
        _cloud(d, cx, cy - int(s * 0.10), int(s * 0.85))
    else:
        _cloud(d, cx, cy - int(s * (0.06 if icon in ("rain", "drizzle", "storm", "snow") else 0)), int(s * 0.92))

    drop_y = cy + int(s * 0.26)
    if icon in ("rain", "showers"):
        xs = (-0.22, 0.0, 0.22) if icon == "rain" else (-0.08, 0.14)
        for fx in xs:
            x = cx + int(s * fx) + (int(s * 0.06) if icon == "showers" else 0)
            d.line((x + 6, drop_y, x - 2, drop_y + int(s * 0.22)), fill=BLUE, width=5)
    elif icon == "drizzle":
        for fx, fy in ((-0.2, 0), (0, 0.1), (0.2, 0)):
            x, y = cx + int(s * fx), drop_y + int(s * fy) + 4
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=BLUE)
    elif icon == "storm":
        x, y = cx, drop_y - 4
        bolt = [(x + 4, y), (x - 10, y + 18), (x - 1, y + 18), (x - 6, y + 32), (x + 12, y + 10), (x + 2, y + 10), (x + 8, y)]
        d.polygon(bolt, fill=YELLOW, outline=BLACK, width=2)
        for fx in (-0.26, 0.26):
            xx = cx + int(s * fx)
            d.line((xx + 4, drop_y, xx - 2, drop_y + int(s * 0.18)), fill=BLUE, width=5)
    elif icon == "snow":
        for fx, fy in ((-0.2, 0), (0, 0.12), (0.2, 0)):
            x, y = cx + int(s * fx), drop_y + int(s * fy) + 4
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=BLUE)
    elif icon == "fog":
        for i, w in enumerate((0.42, 0.32)):
            y = cy + int(s * 0.22) + i * 10
            d.line((cx - int(s * w), y, cx + int(s * w), y), fill=BLACK, width=4)


def _tank(d, x, y, w, h, pct: Optional[float], stale: bool):
    """Tank outline with a blue water column."""
    r = 14
    d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=WHITE, outline=BLACK, width=4)
    if pct is None:
        return
    inner_top, inner_bot = y + 6, y + h - 6
    level = max(0.0, min(100.0, pct))
    top = inner_bot - (inner_bot - inner_top) * level / 100.0
    if level > 0.5:
        d.rounded_rectangle((x + 6, int(top), x + w - 6, inner_bot), radius=max(2, r - 6),
                            fill=RED if stale else BLUE)
    # scale ticks at 25 / 50 / 75 %, drawn over the water so they read either way
    for q in (25, 50, 75):
        ty = int(inner_bot - (inner_bot - inner_top) * q / 100.0)
        tick_colour = WHITE if ty > top else BLACK
        d.line((x + 6, ty, x + 18, ty), fill=tick_colour, width=2)
        d.line((x + w - 18, ty, x + w - 6, ty), fill=tick_colour, width=2)


def _dashed_rect(d, box, dash=10, gap=8, width=2, fill=BLACK):
    x0, y0, x1, y1 = box
    for x in range(x0, x1, dash + gap):
        d.line((x, y0, min(x + dash, x1), y0), fill=fill, width=width)
        d.line((x, y1, min(x + dash, x1), y1), fill=fill, width=width)
    for y in range(y0, y1, dash + gap):
        d.line((x0, y, x0, min(y + dash, y1)), fill=fill, width=width)
        d.line((x1, y, x1, min(y + dash, y1)), fill=fill, width=width)


def _fmt_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def _section(d, x, y, label):
    _text(d, (x, y), label.upper(), 19, bold=True)


# ── Renderer ────────────────────────────────────────────────────────────────
#
# The page is laid out at its *viewing* size: 800×480 landscape or 480×800 portrait.
# to_panel() then rotates a portrait page into the panel's native 800×480 buffer, so the
# device firmware never knows the orientation. Each section is a helper that takes its
# box, and both layouts are just different arrangements of the same helpers.

def _new_page(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("P", (w, h), WHITE)
    im.putpalette(_PREVIEW_PALETTE + [0] * (768 - len(_PREVIEW_PALETTE)))
    d = ImageDraw.Draw(im)
    d.fontmode = "1"  # no anti-aliasing: every pixel is an exact panel colour
    return im, d


def _current_weather(weather: Optional[dict]) -> str:
    if weather and weather.get("available"):
        c = weather["current"]
        if c.get("temp") is not None:
            return f"{weather.get('location', '')}  {round(c['temp'])}°  {c['label']}"
    return ""


def _tank_column(d, cx: int, name: str, t: dict, simulated: bool,
                 y_name: int, ty: int, tw: int, th: int, y_pct: int, y_sub: int) -> Optional[float]:
    """Name, tank graphic, big percentage and a sub-line. Returns litres counted toward the total."""
    pct = t.get("level_pct")
    age = t.get("age_s")
    stale = pct is None or age is None or age > 600
    _text(d, (cx, y_name), name, 23, bold=True, anchor="mm")
    _tank(d, cx - tw // 2, ty, tw, th, pct, stale)
    if pct is None:
        _text(d, (cx, y_pct), "no data", 30, RED, bold=True, anchor="mm")
        return None
    _text(d, (cx, y_pct), f"{pct:.0f}%", 46, RED if stale else BLACK, bold=True, anchor="mm")
    vol = t.get("volume_l")
    if stale:
        hrs = (age or 0) / 3600
        _text(d, (cx, y_sub), f"stale {hrs:.0f} h" if hrs >= 1 else "stale", 21, RED, bold=True, anchor="mm")
    elif simulated:
        _text(d, (cx, y_sub), "simulated", 21, RED, bold=True, anchor="mm")
    elif vol is not None:
        _text(d, (cx, y_sub), f"{vol:,.0f} L", 22, anchor="mm")
    return None if simulated else vol


def _tanks(d, status: dict, config: dict, centres: tuple[int, int],
           y_name: int, ty: int, tw: int, th: int, y_pct: int, y_sub: int) -> tuple[float, bool]:
    sim_b = ((config.get("tanks") or {}).get("tank_b") or {}).get("simulated_pct") is not None
    total = 0.0
    for cx, (key, name) in zip(centres, (("tank_a", "Tank A"), ("tank_b", "Tank B"))):
        vol = _tank_column(d, cx, name, status.get(key) or {}, key == "tank_b" and sim_b,
                           y_name, ty, tw, th, y_pct, y_sub)
        total += vol or 0.0
    return total, sim_b


def _pump_line(d, x0: int, x1: int, y: int, status: dict, total: float, sim_b: bool) -> None:
    state = status.get("state", "?")
    if state == "FAULT":
        # Drop the parenthesised detail; the dashboard has the full reason.
        msg = (status.get("fault_reason") or "fault").split("(")[0].strip()
        d.rounded_rectangle((x0, y - 17, x1, y + 17), radius=8, fill=RED)
        _text(d, (x0 + 10, y), _ellipsize(f"FAULT: {msg}", 20, True, x1 - x0 - 20), 20, WHITE, bold=True, anchor="lm")
        return
    on = bool(status.get("pump_on"))
    d.ellipse((x0 + 6, y - 8, x0 + 22, y + 8), fill=GREEN if on else BLACK)
    label = "Pumping to Tank B" if on else {"COOLDOWN": "Pump resting", "IDLE": "Pump idle"}.get(state, f"Pump {state.lower()}")
    _text(d, (x0 + 32, y), label, 22, bold=True, anchor="lm")
    # A simulated tank would make the total fiction, so it is only shown when both are real.
    if total and not sim_b:
        _text(d, (x1, y), f"{total:,.0f} L total", 22, anchor="rm")


def _forecast(d, x0: int, x1: int, y: int, weather: Optional[dict], days_n: int = 4) -> None:
    """Day columns starting at y (top of the 'Today' box): name, icon, mm, chance, temps."""
    if not (weather and weather.get("available") and weather.get("daily")):
        _text(d, ((x0 + x1) // 2, y + 90), "Forecast unavailable", 24, RED, bold=True, anchor="mm")
        return
    col_w = (x1 - x0) // days_n
    for i, day in enumerate(weather["daily"][:days_n]):
        cx = x0 + col_w * i + col_w // 2
        name = "Today" if i == 0 else datetime.fromisoformat(day["date"]).strftime("%a")
        if i == 0:
            d.rounded_rectangle((cx - col_w // 2 + 3, y, cx + col_w // 2 - 3, y + 194), radius=10, outline=BLACK, width=2)
        _text(d, (cx, y + 18), name, 23, bold=True, anchor="mm")
        draw_weather_icon(d, day.get("icon", "cloud"), cx, y + 66, 66)
        mm = day.get("rain_mm") or 0.0
        if mm < 0.1:
            _text(d, (cx, y + 130), "dry", 30, bold=True, anchor="ms")
        else:
            mm_s = f"{mm:.0f}" if mm >= 10 else f"{mm:.1f}"
            num_w = _font(36, True).getlength(mm_s)
            unit_w = _font(18).getlength(" mm")
            left = cx - (num_w + unit_w) / 2
            _text(d, (left, y + 130), mm_s, 36, BLUE, bold=True, anchor="ls")
            _text(d, (left + num_w, y + 130), " mm", 18, BLUE, anchor="ls")
        prob = day.get("rain_prob")
        chance = f"{prob if prob is not None else 0}% chance"
        _text(d, (cx, y + 150), chance, _fit(chance, 17, False, col_w - 8), anchor="mm")
        _text(d, (cx, y + 176), f"{round(day['temp_max'])}° / {round(day['temp_min'])}°", 20, anchor="mm")


def _rain_totals(weather: Optional[dict]) -> str:
    if weather and weather.get("available"):
        return (f"Rain last 7 days {weather.get('rain_past_7d_mm', 0):.1f} mm  ·  "
                f"next 7 days {weather.get('rain_next_7d_mm', 0):.1f} mm")
    return ""


def _updated_text(now: datetime, telemetry: Optional[dict]) -> tuple[str, bool]:
    """Footer 'Updated …' text, and whether it should be red (battery low, not charging)."""
    s = f"Updated {_fmt_time(now)}"
    if telemetry and telemetry.get("battery_pct") is not None:
        bp = int(telemetry["battery_pct"])
        s += f"  ·  battery {bp}%"
        return s, bp <= 15 and not telemetry.get("charging")
    return s, False


def _render_landscape(status, weather, config, now, telemetry, solar) -> Image.Image:
    im, d = _new_page(W, H)
    d.rectangle((0, 0, W, 58), fill=BLACK)
    _text(d, (18, 29), f"{now:%A} {now.day} {now:%B}", 32, WHITE, bold=True, anchor="lm")
    _text(d, (W - 18, 29), _current_weather(weather), 24, WHITE, anchor="rm")

    _section(d, 18, 72, "Water")
    total, sim_b = _tanks(d, status, config, (104, 280), y_name=118, ty=138, tw=122, th=178, y_pct=352, y_sub=388)
    _pump_line(d, 14, 364, 425, status, total, sim_b)
    d.line((382, 72, 382, 438), fill=BLACK, width=2)

    _section(d, 400, 72, "Rain forecast")
    if weather and weather.get("stale"):
        _text(d, (W - 16, 72), "forecast out of date", 17, RED, bold=True, anchor="ra")
    _forecast(d, 400, W - 14, 92, weather)

    d.line((400, 298, W - 16, 298), fill=BLACK, width=2)
    _section(d, 400, 310, "Solar")
    _draw_solar(d, (400, 338, W - 16, 438), solar)

    d.line((14, 446, W - 14, 446), fill=BLACK, width=2)
    _text(d, (18, 463), _rain_totals(weather), 19, anchor="lm")
    upd, warn = _updated_text(now, telemetry)
    _text(d, (W - 18, 463), upd, 19, RED if warn else BLACK, bold=warn, anchor="rm")
    return im


def _render_portrait(status, weather, config, now, telemetry, solar) -> Image.Image:
    pw, ph = H, W  # 480 × 800
    im, d = _new_page(pw, ph)
    # Header: date, then current conditions on a second line.
    d.rectangle((0, 0, pw, 84), fill=BLACK)
    date_s = f"{now:%A} {now.day} {now:%B}"
    _text(d, (pw // 2, 28), date_s, _fit(date_s, 34, True, pw - 32), WHITE, bold=True, anchor="mm")
    _text(d, (pw // 2, 62), _current_weather(weather), 22, WHITE, anchor="mm")

    _section(d, 18, 98, "Water")
    total, sim_b = _tanks(d, status, config, (128, 352), y_name=136, ty=154, tw=136, th=160, y_pct=350, y_sub=386)
    _pump_line(d, 18, pw - 18, 420, status, total, sim_b)

    d.line((14, 442, pw - 14, 442), fill=BLACK, width=2)
    _section(d, 18, 452, "Rain forecast")
    if weather and weather.get("stale"):
        _text(d, (pw - 18, 452), "out of date", 17, RED, bold=True, anchor="ra")
    _forecast(d, 14, pw - 14, 476, weather)

    d.line((14, 682, pw - 14, 682), fill=BLACK, width=2)
    _section(d, 18, 692, "Solar")
    _draw_solar(d, (14, 718, pw - 14, 770), solar, compact=True)

    # Footer: one centred line; the 7-day rain totals don't fit beside it at this width.
    d.line((14, 776, pw - 14, 776), fill=BLACK, width=2)
    upd, warn = _updated_text(now, telemetry)
    _text(d, (pw // 2, 789), upd, 18, RED if warn else BLACK, bold=warn, anchor="mm")
    return im


def render(status: dict, weather: Optional[dict], config: dict, now: datetime,
           telemetry: Optional[dict] = None, solar: Optional[dict] = None,
           orientation: str = "landscape") -> Image.Image:
    """
    Lay out the dashboard at its viewing size: 800×480 landscape, or 480×800 if
    orientation == "portrait". Returns a 'P' image whose pixel values are panel indices.
    Use to_panel() to get the 800×480 buffer the device needs.
    """
    if orientation == "portrait":
        return _render_portrait(status, weather, config, now, telemetry, solar)
    return _render_landscape(status, weather, config, now, telemetry, solar)


def to_panel(page: Image.Image, portrait_turn: str = "cw") -> Image.Image:
    """
    Map a laid-out page onto the panel's native 800×480 landscape buffer.

    portrait_turn is which way the frame was turned from landscape to stand it up:
    "cw" = turned clockwise (the old left edge is now the top), "ccw" = counter-clockwise.
    """
    if page.size == (W, H):
        return page
    if page.size != (H, W):
        raise ValueError(f"unexpected page size {page.size}")
    # Turned clockwise: the page's top must land on the panel's left edge, i.e. rotate the
    # page 90° counter-clockwise (PIL ROTATE_90).
    op = Image.Transpose.ROTATE_90 if portrait_turn == "cw" else Image.Transpose.ROTATE_270
    return page.transpose(op)


def _draw_solar(d, box, solar: Optional[dict], compact: bool = False) -> None:
    """Solar panel: Now / Today / grid flow. compact=True (portrait) fits a ~52 px box:
    label on top, then value + unit on one line."""
    x0, y0, x1, y1 = box
    if not solar:
        # Inset the dashed box so it never meets the line below the section.
        inner = (x0, y0, x1, y1 - 6)
        _dashed_rect(d, inner)
        cy = (inner[1] + inner[3]) // 2
        _sun(d, x0 + 40, cy, 13 if compact else 16, rays=not compact)
        _text(d, (x0 + 76, cy), "Solar data unavailable", 22, bold=True, anchor="lm")
        return
    # Now / today / grid flow. Grid is signed at the meter: + export, − import.
    grid = solar.get("grid_kw") or 0.0
    cols = [("Now", f"{solar.get('power_kw', 0):.1f}", "kW", GREEN),
            ("Today", f"{solar.get('today_kwh', 0):.1f}", "kWh", BLACK),
            ("Exporting" if grid >= 0 else "Importing", f"{abs(grid):.1f}", "kW", GREEN if grid >= 0 else RED)]
    if compact:
        cw = (x1 - x0) // 3
        for i, (label, val, unit, colour) in enumerate(cols):
            cx = x0 + cw * i + cw // 2
            num_w = _font(34, True).getlength(val)
            unit_w = _font(17).getlength(" " + unit)
            left = cx - (num_w + unit_w) / 2
            _text(d, (cx, y0), label, 17, anchor="mt")
            _text(d, (left, y0 + 50), val, 34, colour, bold=True, anchor="ls")
            _text(d, (left + num_w, y0 + 50), " " + unit, 17, anchor="ls")
        return
    cw = (x1 - x0) // 3
    for i, (label, val, unit, colour) in enumerate(cols):
        cx = x0 + cw * i + cw // 2
        _text(d, (cx, y0 + 14), label, 19, anchor="mm")
        _text(d, (cx, y0 + 56), val, 40, colour, bold=True, anchor="mm")
        _text(d, (cx, y0 + 88), unit, 18, anchor="mm")


# ── Packing ─────────────────────────────────────────────────────────────────

def pack(im: Image.Image) -> bytes:
    """Pack a 'P' image of panel indices into the 4-bpp frame (even x → high nibble)."""
    if im.size != (W, H) or im.mode != "P":
        raise ValueError(f"expected 800x480 P image, got {im.size} {im.mode}")
    px = im.tobytes()
    # Two-pixel lookup: translate pairs via a precomputed table is not possible with
    # bytes.translate (it's 1:1), so combine hi/lo slices directly.
    hi, lo = px[0::2], px[1::2]
    out = bytes((a << 4) | b for a, b in zip(hi, lo))
    assert len(out) == FRAME_BYTES
    return out


def unpack(buf: bytes) -> Image.Image:
    """Inverse of pack(), for golden-image tests and the PNG preview of a stored frame."""
    px = bytearray(W * H)
    px[0::2] = bytes(b >> 4 for b in buf)
    px[1::2] = bytes(b & 0x0F for b in buf)
    im = Image.frombytes("P", (W, H), bytes(px))
    im.putpalette(_PREVIEW_PALETTE + [0] * (768 - len(_PREVIEW_PALETTE)))
    return im


def to_png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ── Service: frame cache, schedule decisions, telemetry ─────────────────────

class DisplayService:
    """
    Glue between the Flask routes and the renderer. Thread-safe; holds no reference to
    pump control beyond the read-only status getter.
    """

    def __init__(self, config: dict, get_status, weather, data_dir: Path, get_solar=None) -> None:
        self._config = config
        self._get_status = get_status
        self._weather = weather
        self._get_solar = get_solar
        self._schedule = Schedule.from_config(config)
        self._state_path = data_dir / "display_state.json"
        self._lock = threading.Lock()
        self._state: dict = {"telemetry": None, "last_seen": None, "last_frame_served": None,
                             "last_etag": None, "last_next_wake_s": None}
        self._load()

    @property
    def schedule(self) -> Schedule:
        return self._schedule

    # Rendering ------------------------------------------------------------

    def render_now(self) -> tuple[Image.Image, bytes, str]:
        """Returns (page as laid out — portrait or landscape, packed panel frame, etag)."""
        now = self._schedule.now()
        weather = self._weather.get() if self._weather else None
        solar = None
        if self._get_solar:
            try:
                solar = self._get_solar()
            except Exception as exc:
                log.warning("Solar source failed: %s", exc)
        with self._lock:
            telemetry = self._state.get("telemetry")
        disp = self._config.get("display") or {}
        im = render(self._get_status(), weather, self._config, now, telemetry, solar,
                    orientation=str(disp.get("orientation", "landscape")))
        frame = pack(to_panel(im, str(disp.get("portrait_turn", "cw"))))
        etag = hashlib.sha1(frame).hexdigest()[:16]
        return im, frame, etag

    # Device protocol ------------------------------------------------------

    def decide(self, if_none_match: Optional[str], force: bool) -> dict:
        """
        Work out the response to a frame request.

        Returns {"send": bool, "frame": bytes|None, "etag": str|None, "next_wake_s": int}.
        - force (button press / first boot, no stored ETag) → always send a fresh frame.
        - scheduled wake outside the active window → don't send (304), sleep to next slot.
        - inside the window → send the new frame unless it is byte-identical.
        """
        now = self._schedule.now()
        next_wake = self._schedule.seconds_until_next(now)
        due = force or not if_none_match or self._schedule.is_due(now)
        result = {"send": False, "frame": None, "etag": if_none_match, "next_wake_s": next_wake}
        if due:
            _, frame, etag = self.render_now()
            if etag != (if_none_match or "").strip('"'):
                result.update(send=True, frame=frame, etag=etag)
        with self._lock:
            self._state["last_seen"] = now.isoformat(timespec="seconds")
            self._state["last_next_wake_s"] = next_wake
            if result["send"]:
                self._state["last_frame_served"] = now.isoformat(timespec="seconds")
                self._state["last_etag"] = result["etag"]
        self._save()
        log.info("Display frame request: force=%s due=%s → %s, next wake in %d s",
                 force, due, "200" if result["send"] else "304", next_wake)
        return result

    def record_telemetry(self, body: dict) -> None:
        allowed = ("battery_mv", "battery_pct", "charging", "vbus", "temp_c", "rh", "rssi",
                   "wake_cause", "fw", "ip", "failures", "awake_ms")
        t = {k: body[k] for k in allowed if k in body}
        now = self._schedule.now()
        t["received_at"] = now.isoformat(timespec="seconds")
        with self._lock:
            self._state["telemetry"] = t
            self._state["last_seen"] = t["received_at"]
        self._save()
        log.info("Display telemetry: %s", t)

    def get_status(self) -> dict:
        now = self._schedule.now()
        with self._lock:
            s = dict(self._state)
        overdue = False
        if s.get("last_seen") and s.get("last_next_wake_s"):
            expected = datetime.fromisoformat(s["last_seen"]) + timedelta(seconds=s["last_next_wake_s"] + 1800)
            overdue = now > expected
        return {
            **s,
            "overdue": overdue,
            "next_refresh": self._schedule.next_slot(now).isoformat(timespec="minutes"),
            "schedule": {
                "start_hour": self._schedule.start_hour,
                "end_hour": self._schedule.end_hour,
                "interval_hours": self._schedule.interval_hours,
                "timezone": self._schedule.tz,
            },
        }

    # Persistence ------------------------------------------------------------

    def _load(self) -> None:
        try:
            self._state.update(json.loads(self._state_path.read_text()))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Ignoring unreadable display state: %s", exc)

    def _save(self) -> None:
        try:
            with self._lock:
                blob = json.dumps(self._state)
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(blob)
            tmp.replace(self._state_path)
        except Exception as exc:
            log.warning("Could not write display state: %s", exc)
