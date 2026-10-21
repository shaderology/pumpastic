"""
Flask web dashboard + control panel for Pumpastic.

Tabs:
  Overview  — live tank levels and pump state
  Devices   — MQTT broker, Tapo plug, ESP32 connectivity
  Config    — editable config.yaml (hot-applied, no restart needed)
  Controls  — simulate sensor data, emergency stop, clear fault

Routes:
  GET  /               → dashboard (web/index.html — a static page, no templating)
  GET  /api/status     → pump + tank status snapshot
  GET  /api/devices    → device connectivity snapshot (cached, 15 s TTL)
  GET  /api/config     → sanitized config values
  POST /api/config     → save + hot-apply config changes
  POST /api/control    → control actions (simulate_sensor, force_pump_off, clear_fault)
  GET  /api/weather    → cached Open-Meteo forecast (see weather.py)
  GET  /api/solar      → inverter output / today / grid flow via Modbus TCP (see solar.py)
  GET  /api/system     → Pi health: CPU temp, throttling/under-voltage, load, memory
  GET  /api/display    → e-ink display schedule + last telemetry

E-ink display device protocol (see eink_display.py, docs/04-protocols.md):
  GET  /display/frame.bin       → 192 000-byte 4-bpp frame, or 304. Headers: ETag, X-Next-Wake-S
  GET  /display/frame.png       → PNG preview of the current frame
  POST /display/telemetry       → device battery / room / RSSI JSON

Access: http://<rpi-ip>:8080
"""
import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory

log = logging.getLogger("pumpastic.dashboard")

_WEB_DIR = Path(__file__).resolve().parent / "web"


class WebDashboard:
    """
    Tabbed Flask dashboard with live status, device panel, config view, and controls.
    Runs in a daemon thread alongside the main controller loop.
    """

    def __init__(
        self,
        get_status_fn: Callable[[], dict],
        config: dict,
        mqtt_client,
        plug,
        loop,
        pump_controller,
        get_device_status_fn: Callable[[], dict],
        level_history=None,
        weather=None,
        display=None,
        solar=None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self._get_status = get_status_fn
        self._config = config
        self._mqtt = mqtt_client
        self._plug = plug
        self._loop = loop
        self._pump = pump_controller
        self._get_device_status = get_device_status_fn
        self._history = level_history
        self._weather = weather
        self._display = display
        self._solar = solar
        self._host = host
        self._port = port
        self._app = Flask("pumpastic-dashboard")
        self._setup_routes()

    def start(self) -> None:
        """Start the dashboard in a background daemon thread."""
        t = threading.Thread(target=self._run, name="dashboard", daemon=True)
        t.start()
        log.info("Dashboard started on http://%s:%d", self._host, self._port)

    def _run(self) -> None:
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
        self._app.run(
            host=self._host,
            port=self._port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    @staticmethod
    def _sanitize_config(config: dict) -> dict:
        """Return a dashboard-safe subset (no credentials, env vars, or paths)."""
        safe_keys = {"mqtt", "tanks", "pump", "alerts", "dashboard"}
        return {k: v for k, v in config.items() if k in safe_keys}

    def _setup_routes(self) -> None:

        @self._app.route("/")
        def index():
            resp = send_from_directory(_WEB_DIR, "index.html")
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        @self._app.route("/manifest.webmanifest")
        def manifest():
            icon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
                    "%3Cpath d='M16 3C16 3 6 14 6 20a10 10 0 0 0 20 0C26 14 16 3 16 3z' fill='%230ea5e9'/%3E%3C/svg%3E")
            return jsonify({
                "name": "Pumpastic", "short_name": "Pumpastic", "start_url": "/",
                "display": "standalone", "background_color": "#0b1220", "theme_color": "#0b1220",
                "icons": [{"src": icon, "sizes": "any", "type": "image/svg+xml"}],
            })

        @self._app.route("/api/weather")
        def api_weather():
            if self._weather is None:
                return jsonify({"available": False, "error": "weather service disabled"})
            return jsonify(self._weather.get())

        @self._app.route("/api/solar")
        def api_solar():
            if self._solar is None:
                return jsonify({"available": False, "enabled": False})
            return jsonify({"enabled": True, **self._solar.get()})

        @self._app.route("/api/system")
        def api_system():
            return jsonify(_system_health())

        # ── E-ink display ───────────────────────────────────────
        @self._app.route("/api/display")
        def api_display():
            if self._display is None:
                return jsonify({"enabled": False}), 404
            return jsonify(self._display.get_status())

        @self._app.route("/display/frame.bin")
        def display_frame():
            if self._display is None:
                return Response(status=404)
            force = request.headers.get("X-Force") == "1" or request.args.get("force") == "1"
            try:
                r = self._display.decide(request.headers.get("If-None-Match"), force)
            except Exception as exc:
                log.exception("Display render failed: %s", exc)
                # Tell the device to come back in 15 min rather than at the next slot.
                return Response(status=503, headers={"X-Next-Wake-S": "900"})
            headers = {"X-Next-Wake-S": str(r["next_wake_s"]), "Cache-Control": "no-store"}
            if r["etag"]:
                headers["ETag"] = f'"{r["etag"]}"'
            if not r["send"]:
                return Response(status=304, headers=headers)
            return Response(r["frame"], mimetype="application/octet-stream", headers=headers)

        @self._app.route("/display/frame.png")
        def display_preview():
            if self._display is None:
                return Response(status=404)
            from eink_display import to_png
            im, _, etag = self._display.render_now()
            return Response(to_png(im), mimetype="image/png",
                            headers={"Cache-Control": "no-store", "ETag": f'"{etag}"'})

        @self._app.route("/display/telemetry", methods=["POST"])
        def display_telemetry():
            if self._display is None:
                return Response(status=404)
            body = request.get_json(force=True, silent=True) or {}
            self._display.record_telemetry(body)
            return jsonify({"ok": True})

        @self._app.route("/api/status")
        def api_status():
            return jsonify(self._get_status())

        @self._app.route("/api/devices")
        def api_devices():
            return jsonify(self._get_device_status())

        @self._app.route("/api/config")
        def api_config():
            return jsonify(self._sanitize_config(self._config))

        @self._app.route("/api/history")
        def api_history():
            if self._history is None:
                return jsonify({"labels": [], "tank_a": [], "tank_b": []})
            try:
                days = int(request.args.get("days", 30))
                days = max(7, min(365, days))
            except (TypeError, ValueError):
                days = 30
            return jsonify(self._history.get_daily(days))

        @self._app.route("/api/config", methods=["POST"])
        def api_save_config():
            body = request.get_json(force=True, silent=True) or {}
            errors = []

            def gf(key, lo, hi):
                try:
                    v = float(body[key])
                    if not (lo <= v <= hi):
                        errors.append(f"{key} must be {lo}–{hi}")
                        return None
                    return v
                except (KeyError, TypeError, ValueError):
                    errors.append(f"{key} must be a number")
                    return None

            p = {
                "start_threshold_pct":     gf("start_threshold_pct",     1,   100),
                "stop_low_threshold_pct":  gf("stop_low_threshold_pct",  0,    99),
                "stop_high_threshold_pct": gf("stop_high_threshold_pct", 1,   100),
                "absolute_min_pct":        gf("absolute_min_pct",        5,    50),
                "absolute_max_pct":        gf("absolute_max_pct",       50,    99),
                "max_run_seconds":         gf("max_run_seconds",        60,  7200),
                "sensor_timeout_seconds":  gf("sensor_timeout_seconds", 30,   600),
                "min_run_seconds":         gf("min_run_seconds",         0,  7200),
                "cooldown_seconds":        gf("cooldown_seconds",        0,  3600),
                "fault_clear_seconds":     gf("fault_clear_seconds",     0,  3600),
                "stuck_detection_count":   gf("stuck_detection_count",   0,   100),
                "stuck_detection_delta_cm":gf("stuck_detection_delta_cm",0.1,  10),
                "dry_run_watt_threshold":  gf("dry_run_watt_threshold",  0,  5000),
                "dry_run_timeout_seconds": gf("dry_run_timeout_seconds", 5,   300),
            }
            ta_h = gf("tank_a_height_cm",   10, 1000)
            ta_o = gf("tank_a_overflow_cm", 10, 1000)
            ta_c = gf("tank_a_capacity_l",   1, 1_000_000)
            tb_h = gf("tank_b_height_cm",   10, 1000)
            tb_o = gf("tank_b_overflow_cm", 10, 1000)
            tb_c = gf("tank_b_capacity_l",   1, 1_000_000)

            if errors:
                return jsonify({"ok": False, "message": "; ".join(errors)})

            # Cross-field sanity
            checks = [
                (p["stop_low_threshold_pct"] >= p["start_threshold_pct"],
                 "Stop (low) threshold must be less than start threshold"),
                (p["absolute_min_pct"] >= p["start_threshold_pct"],
                 "Start threshold must be above the dry-run floor"),
                (p["stop_high_threshold_pct"] > p["absolute_max_pct"],
                 "Tank B stop threshold must be at or below the overflow ceiling"),
                (p["min_run_seconds"] > p["max_run_seconds"],
                 "Min run time cannot exceed max run time"),
                (ta_o >= ta_h,
                 "Tank A overflow height must be below the sensor height"),
                (tb_o >= tb_h,
                 "Tank B overflow height must be below the sensor height"),
            ]
            for bad, msg in checks:
                if bad:
                    return jsonify({"ok": False, "message": msg})

            # Apply to in-memory config
            self._config["pump"].update(p)
            for tank_id, h, o, c in (("tank_a", ta_h, ta_o, ta_c),
                                     ("tank_b", tb_h, tb_o, tb_c)):
                self._config.setdefault("tanks", {}).setdefault(tank_id, {})
                self._config["tanks"][tank_id]["height_cm"] = h
                self._config["tanks"][tank_id]["overflow_height_cm"] = o
                self._config["tanks"][tank_id]["capacity_litres"] = c

            # Persist to disk
            cfg_path = Path(__file__).parent.parent / "config" / "config.yaml"
            try:
                with open(cfg_path, "w") as f:
                    yaml.dump(self._config, f, default_flow_style=False, allow_unicode=True)
            except Exception as exc:
                log.error("Config write failed: %s", exc)
                return jsonify({"ok": False, "message": f"Saved in memory but disk write failed: {exc}"})

            # Hot-reload pump thresholds (no restart needed)
            self._pump.reload_config(self._config["pump"])

            log.info("Config updated via dashboard")
            return jsonify({"ok": True, "message": "Config saved and applied"})

        @self._app.route("/api/control", methods=["POST"])
        def api_control():
            body = request.get_json(force=True, silent=True) or {}
            action = body.get("action", "")

            # ── Simulate sensor reading ─────────────────────────
            if action == "simulate_sensor":
                sensor = body.get("sensor", "")
                if sensor not in ("tank_a", "tank_b"):
                    return jsonify({"ok": False, "message": "sensor must be tank_a or tank_b"})
                try:
                    level_pct = float(body.get("level_pct", 50))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "message": "level_pct must be a number"})
                level_pct = max(0.0, min(100.0, level_pct))
                self._mqtt.publish_simulated_sensor(sensor, level_pct)
                return jsonify({
                    "ok": True,
                    "message": f"Published simulated {sensor} reading at {level_pct:.1f}%",
                })

            # ── Force pump off (emergency stop) ─────────────────
            if action == "force_pump_off":
                if not self._loop or not self._loop.is_running():
                    return jsonify({"ok": False, "message": "Event loop not available"}), 503
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._pump.emergency_stop(), self._loop
                    )
                    future.result(timeout=10)
                    return jsonify({"ok": True, "message": "Pump stopped — entering COOLDOWN"})
                except Exception as e:
                    log.error("emergency_stop failed: %s", e)
                    return jsonify({"ok": False, "message": f"Failed: {e}"})

            # ── Clear fault ──────────────────────────────────────
            if action == "clear_fault":
                cleared = self._pump.clear_fault()
                if cleared:
                    return jsonify({"ok": True, "message": "Fault cleared — returning to IDLE"})
                return jsonify({"ok": False, "message": "Controller is not in FAULT state"})

            # ── Clear level history ──────────────────────────────
            if action == "clear_history":
                if self._history is None:
                    return jsonify({"ok": False, "message": "History not available"})
                self._history.clear()
                return jsonify({"ok": True, "message": "Level history cleared"})

            return jsonify({"ok": False, "message": f"Unknown action: {action!r}"})


def _system_health() -> dict:
    """Pi health for the Devices tab. Every probe is optional — missing ones return None."""
    out: dict = {"cpu_temp_c": None, "throttled": None, "uptime_s": None, "load_1m": None,
                 "mem_available_mb": None, "disk_free_gb": None, "wifi_dbm": None}
    try:
        out["cpu_temp_c"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except Exception:
        pass
    try:
        # "throttled=0x50005". Bit 0 = under-voltage now, bit 16 = under-voltage since boot.
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
        out["throttled"] = int(r.stdout.strip().split("=")[1], 16)
    except Exception:
        pass
    try:
        out["uptime_s"] = float(Path("/proc/uptime").read_text().split()[0])
        out["load_1m"] = os.getloadavg()[0]
    except Exception:
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["mem_available_mb"] = int(line.split()[1]) / 1024
    except Exception:
        pass
    try:
        out["disk_free_gb"] = shutil.disk_usage("/").free / 1e9
    except Exception:
        pass
    try:
        # /proc/net/wireless: "wlan0: 0000   43.  -67.  -256 ..." — the level column is dBm
        for line in Path("/proc/net/wireless").read_text().splitlines()[2:]:
            parts = line.split()
            if parts and parts[0].startswith("wlan"):
                out["wifi_dbm"] = int(float(parts[3]))
    except Exception:
        pass
    out["checked_at"] = time.time()
    return out
