"""
Persistent tank level history for the dashboard graph.

Records hourly snapshots; aggregates to daily averages on read.
Stores up to ~400 days of hourly data (~9 600 records) in a JSON file.
"""
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("pumpastic.history")

_MAX_RECORDS = 9_600  # 400 days × 24 h


class LevelHistory:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "level_history.json"
        self._lock = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._records: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            with open(self._path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as exc:
            log.warning("Could not load history file: %s", exc)
            return []

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._records, f)
        except Exception as exc:
            log.error("Could not save history: %s", exc)

    def record(self, tank_a_pct: Optional[float], tank_b_pct: Optional[float]) -> None:
        """Append a snapshot. Call once per hour from the controller loop."""
        if tank_a_pct is None and tank_b_pct is None:
            return
        entry: dict = {"ts": datetime.now(timezone.utc).isoformat()}
        if tank_a_pct is not None:
            entry["a"] = round(tank_a_pct, 1)
        if tank_b_pct is not None:
            entry["b"] = round(tank_b_pct, 1)
        with self._lock:
            self._records.append(entry)
            if len(self._records) > _MAX_RECORDS:
                self._records = self._records[-_MAX_RECORDS:]
            self._save()

    def get_daily(self, days: int = 30) -> dict:
        """Return daily averages for the last N days as chart-ready lists."""
        with self._lock:
            records = list(self._records)

        buckets: dict[str, dict[str, list]] = defaultdict(lambda: {"a": [], "b": []})
        for r in records:
            date = r["ts"][:10]
            if "a" in r:
                buckets[date]["a"].append(r["a"])
            if "b" in r:
                buckets[date]["b"].append(r["b"])

        sorted_dates = sorted(buckets)[-days:]
        labels: list[str] = []
        tank_a: list = []
        tank_b: list = []
        for date in sorted_dates:
            labels.append(date)
            av = buckets[date]["a"]
            bv = buckets[date]["b"]
            tank_a.append(round(sum(av) / len(av), 1) if av else None)
            tank_b.append(round(sum(bv) / len(bv), 1) if bv else None)

        return {"labels": labels, "tank_a": tank_a, "tank_b": tank_b}

    def clear(self) -> None:
        with self._lock:
            self._records = []
            self._save()
        log.info("Level history cleared")
