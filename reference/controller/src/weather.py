"""
Weather and rain forecast for the dashboard and the e-ink display.

Source: Open-Meteo (https://open-meteo.com) — free, no API key, and its "best match"
model for Australia includes BOM ACCESS-G. Fetched in a background thread every
`refresh_minutes` and cached to disk, so the dashboard and the e-ink frame always have
the last good forecast even while the internet is down.

Nothing here feeds pump control. The forecast is informational only; a failure in
this module must never reach the state machine, so every external call is wrapped
and the worst outcome is a stale or empty forecast.

Config (config.yaml):
  weather:
    location_name: Your Town
    latitude: 51.4779
    longitude: -0.0015
    timezone: UTC
    refresh_minutes: 30
"""
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("pumpastic.weather")

_API_URL = "https://api.open-meteo.com/v1/forecast"
_DEFAULTS = {
    "location_name": "Weather",
    "latitude": 51.4779,
    "longitude": -0.0015,
    "timezone": "UTC",
    "refresh_minutes": 30,
}

# WMO weather interpretation codes → (short label, icon key).
# Icon keys are shared by the web dashboard and the e-ink renderer.
_WMO = {
    0: ("Clear", "sun"),
    1: ("Mostly clear", "sun"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"),
    57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "showers"),
    81: ("Showers", "showers"),
    82: ("Heavy showers", "showers"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm", "storm"),
    99: ("Thunderstorm", "storm"),
}


def describe(code: Optional[int]) -> tuple[str, str]:
    """Map a WMO weather code to (label, icon key)."""
    if code is None:
        return ("Unknown", "cloud")
    return _WMO.get(int(code), ("Unknown", "cloud"))


def daily_summary(code: Optional[int], rain_mm: float, rain_prob: Optional[int],
                  cloud_mean: Optional[float]) -> tuple[str, str]:
    """
    Pick a day's (label, icon) the way a person would describe the day.

    Open-Meteo's daily weather_code is the *most severe* hourly code: one overcast hour
    makes the day "Overcast", one drizzly hour makes it "Drizzle". On 2026-10-03 that
    showed a cloud for a day with 28% mean cloud and 12 h of sunshine, while Google and
    AccuWeather said "mostly sunny". So the precipitation icon is kept only when rain is
    meaningful; otherwise the icon comes from mean cloud cover.
    """
    label, icon = describe(code)
    wet = icon in ("drizzle", "rain", "showers", "storm", "snow")
    if wet and (rain_mm >= 1.0 or (rain_prob or 0) >= 50):
        return label, icon
    if cloud_mean is None:
        return ("Dry", "cloud") if wet else (label, icon)
    if cloud_mean < 35:
        return "Mostly sunny", "sun"
    if cloud_mean < 70:
        return "Partly cloudy", "partly"
    return "Cloudy", "cloud"


def parse_open_meteo(raw: dict, past_days: int) -> dict:
    """
    Reshape an Open-Meteo response into the structure the UI consumes.

    `daily` holds only today onwards; the `past_days` days before today are summed
    into `rain_past_7d_mm` (observed/analysed rain, useful next to tank levels).
    """
    d = raw["daily"]
    days = []
    clouds = d.get("cloud_cover_mean") or [None] * len(d["time"])
    for i, date in enumerate(d["time"]):
        rain = d["precipitation_sum"][i] or 0.0
        label, icon = daily_summary(d["weather_code"][i], rain,
                                    d["precipitation_probability_max"][i], clouds[i])
        days.append({
            "date": date,
            "code": d["weather_code"][i],
            "label": label,
            "icon": icon,
            "temp_max": d["temperature_2m_max"][i],
            "temp_min": d["temperature_2m_min"][i],
            "rain_mm": rain,
            "cloud_mean": clouds[i],
            "rain_prob": d["precipitation_probability_max"][i],
            "rain_hours": d["precipitation_hours"][i],
            "sunrise": d["sunrise"][i],
            "sunset": d["sunset"][i],
        })
    past, future = days[:past_days], days[past_days:]

    c = raw.get("current", {})
    label, icon = describe(c.get("weather_code"))
    current = {
        "time": c.get("time"),
        "temp": c.get("temperature_2m"),
        "humidity": c.get("relative_humidity_2m"),
        "rain_mm": c.get("precipitation"),
        "wind_kmh": c.get("wind_speed_10m"),
        "is_day": bool(c.get("is_day", 1)),
        "code": c.get("weather_code"),
        "label": label,
        "icon": icon,
    }

    # Next 24 h of hourly rain, starting at the current hour.
    h = raw.get("hourly", {})
    hourly = []
    now_hour = (c.get("time") or "")[:13]
    times = h.get("time", [])
    start = next((i for i, t in enumerate(times) if t[:13] >= now_hour), 0) if now_hour else 0
    for i in range(start, min(start + 24, len(times))):
        hourly.append({
            "time": times[i],
            "rain_mm": h["precipitation"][i] or 0.0,
            "rain_prob": h["precipitation_probability"][i],
        })

    return {
        "current": current,
        "daily": future,
        "hourly": hourly,
        "rain_past_7d_mm": round(sum(x["rain_mm"] for x in past), 1),
        "rain_next_7d_mm": round(sum(x["rain_mm"] for x in future), 1),
    }


class WeatherService:
    """Background Open-Meteo poller with an on-disk cache of the last good result."""

    PAST_DAYS = 7
    FORECAST_DAYS = 7

    def __init__(self, config: dict, data_dir: Path) -> None:
        cfg = {**_DEFAULTS, **(config.get("weather") or {})}
        self._lat = float(cfg["latitude"])
        self._lon = float(cfg["longitude"])
        self._tz = str(cfg["timezone"])
        self._name = str(cfg["location_name"])
        self._interval_s = max(5, int(cfg["refresh_minutes"])) * 60
        self._cache_path = data_dir / "weather_cache.json"
        self._lock = threading.Lock()
        self._data: Optional[dict] = None
        self._fetched_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._wake = threading.Event()
        self._load_cache()

    # ── Public ──────────────────────────────────────────────────

    def start(self) -> None:
        threading.Thread(target=self._run, name="weather", daemon=True).start()
        log.info("Weather service started for %s (%.4f, %.4f)", self._name, self._lat, self._lon)

    def refresh_now(self) -> None:
        """Ask the background thread to fetch immediately."""
        self._wake.set()

    def get(self) -> dict:
        """Snapshot for the API. Always returns a dict, even before the first fetch."""
        with self._lock:
            data = self._data
            fetched_at = self._fetched_at
            err = self._last_error
        age = (time.time() - fetched_at) if fetched_at else None
        return {
            "location": self._name,
            "latitude": self._lat,
            "longitude": self._lon,
            "available": data is not None,
            "fetched_at": datetime.fromtimestamp(fetched_at).isoformat(timespec="seconds") if fetched_at else None,
            "age_s": round(age) if age is not None else None,
            # Stale = missed two refresh cycles. The UI shows a badge rather than hiding it.
            "stale": age is None or age > 2 * self._interval_s + 120,
            "error": err,
            "source": "Open-Meteo",
            **(data or {}),
        }

    # ── Internals ───────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            self._fetch()
            # Retry sooner after a failure; otherwise wait a full interval.
            wait = self._interval_s if self._last_error is None else min(300, self._interval_s)
            self._wake.wait(wait)
            self._wake.clear()

    def _fetch(self) -> None:
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "timezone": self._tz,
            "past_days": self.PAST_DAYS,
            "forecast_days": self.FORECAST_DAYS,
            "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,"
                       "wind_speed_10m,is_day",
            "hourly": "precipitation,precipitation_probability",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max,precipitation_hours,cloud_cover_mean,sunrise,sunset",
        }
        try:
            r = requests.get(_API_URL, params=params, timeout=20)
            r.raise_for_status()
            parsed = parse_open_meteo(r.json(), self.PAST_DAYS)
        except Exception as exc:  # network, HTTP, JSON or schema change
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("Weather fetch failed: %s", exc)
            return

        now = time.time()
        with self._lock:
            self._data = parsed
            self._fetched_at = now
            self._last_error = None
        log.debug("Weather updated: %s, next 7d rain %.1f mm",
                  parsed["current"]["label"], parsed["rain_next_7d_mm"])
        self._save_cache(parsed, now)

    def _load_cache(self) -> None:
        try:
            blob = json.loads(self._cache_path.read_text())
            self._data = blob["data"]
            self._fetched_at = float(blob["fetched_at"])
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Ignoring unreadable weather cache: %s", exc)

    def _save_cache(self, data: dict, fetched_at: float) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": fetched_at, "data": data}))
            tmp.replace(self._cache_path)
        except Exception as exc:
            log.warning("Could not write weather cache: %s", exc)
