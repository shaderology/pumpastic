"""
Pumpastic controller — entry point.

Wires together: MQTT client, pump state machine, plug control, alerter.
Handles graceful startup and shutdown (safety: pump always OFF on exit).

Run:
    cd ~/pumpastic/controller
    python3 src/controller.py
"""
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Allow running as `python3 src/controller.py` from the controller/ dir
sys.path.insert(0, str(Path(__file__).parent))

from alerting import Alerter
from level_history import LevelHistory
from mqtt_client import MqttClient
from plug_control import PlugControl
from pump_control import PumpController
from web_dashboard import WebDashboard
from weather import WeatherService
from solar import SolarService

_HEARTBEAT_INTERVAL_S = 30
_POWER_POLL_INTERVAL_S = 10
_DEVICE_POLL_INTERVAL_S = 15
_SIMULATED_SENSOR_INTERVAL_S = 60   # Re-inject every 60 s (well within 120 s timeout)
_HISTORY_RECORD_INTERVAL_S = 3600  # One snapshot per hour
# rainwater/pump/state is retained and carries the live tank levels, so subscribers
# (anything reading the retained snapshot) treat it as current. It is
# published on every state change, but the controller sits in one state for hours at a
# time, so without this it would advertise whatever the levels were at the last
# transition. Republish on a timer to keep it honest.
_STATE_PUBLISH_INTERVAL_S = 15


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}\nCopy config.yaml.example to config.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    level_name = config.get("logging", {}).get("level", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)
    log_file = config.get("logging", {}).get("file")

    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        log_dir = Path(log_file).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3
            )
        )

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
    )


async def run(config: dict) -> None:
    log = logging.getLogger("pumpastic.controller")
    log.info("Pumpastic controller starting")

    # ── Credentials from environment ──
    # TAPO_* is the documented name; KASA_* is accepted from older setups.
    username = os.environ.get("TAPO_USERNAME") or os.environ.get("KASA_USERNAME")
    password = os.environ.get("TAPO_PASSWORD") or os.environ.get("KASA_PASSWORD")
    if not username or not password:
        sys.exit(
            "TAPO_USERNAME / TAPO_PASSWORD environment variables not set.\n"
            "See systemd/pumpastic.env.example for how to configure them."
        )

    plug_host = config["plug"]["host"]

    # ── Build components ──
    plug = PlugControl(plug_host, username, password)
    alerter = Alerter(config)
    data_dir = Path(__file__).parent.parent / "data"
    history = LevelHistory(data_dir)

    pump = PumpController(config, plug, alerter)
    mqtt = MqttClient(config, pump.on_sensor_data)

    # Wire state change → MQTT publish
    pump._on_state_change = mqtt.publish_pump_state  # noqa: SLF001

    # ── Safety: ensure pump is OFF before we start ──
    log.info("Startup safety check: ensuring pump is OFF")
    if not await plug.turn_off():
        log.warning("Could not reach plug on startup — continuing, will retry")

    # ── Start MQTT ──
    mqtt.start()
    await asyncio.sleep(1)  # Allow connection to establish

    # ── Device status cache (kept fresh by device_poll_loop) ──
    loop = asyncio.get_running_loop()
    _device_cache: dict = {
        "mqtt_connected": False,
        "mqtt_broker": config["mqtt"]["broker"],
        "mqtt_port": config["mqtt"]["port"],
        "plug_reachable": False,
        "plug_on": False,
        "plug_power_w": None,
        "plug_model": None,
        "plug_nickname": None,
        "tank_a_online": None,
        "tank_b_online": None,
        "last_checked": None,
    }

    # ── Start web dashboard ──
    # Weather + e-ink display are informational only: they read status, never write it,
    # and a failure in either is contained (logged, stale data shown) — see their modules.
    weather = None
    if (config.get("weather") or {}).get("enabled", True):
        weather = WeatherService(config, data_dir)
        weather.start()

    solar = None
    if (config.get("solar") or {}).get("enabled", False):
        solar = SolarService(config, publish=mqtt.publish_solar)
        solar.start()

    display = None
    if (config.get("display") or {}).get("enabled", True):
        try:
            from eink_display import DisplayService
            display = DisplayService(config, pump.get_status, weather, data_dir,
                                     get_solar=solar.display_data if solar else None)
        except Exception as exc:  # e.g. Pillow missing — the dashboard must still start
            log.error("E-ink display disabled: %s", exc)

    dashboard_cfg = config.get("dashboard", {})
    if dashboard_cfg.get("enabled", True):
        dashboard = WebDashboard(
            get_status_fn=pump.get_status,
            config=config,
            mqtt_client=mqtt,
            plug=plug,
            loop=loop,
            pump_controller=pump,
            get_device_status_fn=lambda: dict(_device_cache),
            level_history=history,
            weather=weather,
            display=display,
            solar=solar,
            host=dashboard_cfg.get("host", "0.0.0.0"),
            port=int(dashboard_cfg.get("port", 8080)),
        )
        dashboard.start()

    # ── Graceful shutdown handler ──
    shutdown_event = asyncio.Event()

    def _handle_signal(sig):
        log.info("Received signal %s — shutting down", sig.name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    # ── Background tasks ──
    async def heartbeat_loop():
        while not shutdown_event.is_set():
            mqtt.publish_heartbeat()
            mqtt.publish_online()   # self-heal a late LWT overwriting our status
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

    async def power_poll_loop():
        while not shutdown_event.is_set():
            if pump.state.value in ("MIN_RUN", "RUNNING"):
                watts = await plug.get_power_watts()
                if watts is not None:
                    mqtt.publish_pump_power(watts)
                    pump.on_power_reading(watts)
                    # INFO, not DEBUG: this is the measurement that sets
                    # dry_run_watt_threshold, and rainwater/pump/power is QoS 0 and
                    # not retained — without a log line the reading is gone the
                    # moment the run ends.
                    log.info("Pump power: %.1f W", watts)
            await asyncio.sleep(_POWER_POLL_INTERVAL_S)

    async def device_poll_loop():
        """Keep _device_cache fresh for the dashboard Devices tab."""
        while not shutdown_event.is_set():
            try:
                _device_cache["mqtt_connected"] = mqtt.is_connected
                _device_cache["tank_a_online"] = mqtt.tank_a_online
                _device_cache["tank_b_online"] = mqtt.tank_b_online
                ds = await plug.get_device_status()
                _device_cache.update(ds)
            except Exception as e:
                log.debug("Device poll error: %s", e)
                _device_cache["plug_reachable"] = False
            _device_cache["last_checked"] = datetime.now().isoformat()
            await asyncio.sleep(_DEVICE_POLL_INTERVAL_S)

    async def simulated_sensor_loop():
        """
        Inject periodic fake readings for any tank configured with simulated_pct.
        Publishes to MQTT (visible to external tools) and feeds the state machine
        so a missing physical sensor never causes a timeout FAULT.
        """
        simulated = {
            tank_id: float(cfg["simulated_pct"])
            for tank_id, cfg in config.get("tanks", {}).items()
            if cfg.get("simulated_pct") is not None
        }
        if not simulated:
            return
        for tank_id, level in simulated.items():
            log.info("Simulated sensor active: %s fixed at %.1f%%", tank_id, level)
        await asyncio.sleep(2)  # Let MQTT connect first
        while not shutdown_event.is_set():
            for tank_id, level in simulated.items():
                mqtt.publish_simulated_sensor(tank_id, level)
            await asyncio.sleep(_SIMULATED_SENSOR_INTERVAL_S)

    async def state_publish_loop():
        """Keep the retained pump/state snapshot fresh between state changes."""
        await asyncio.sleep(5)  # let the first sensor readings land
        while not shutdown_event.is_set():
            try:
                mqtt.publish_pump_state(pump.get_status())
            except Exception:
                log.exception("Periodic pump state publish failed")
            await asyncio.sleep(_STATE_PUBLISH_INTERVAL_S)

    async def history_record_loop():
        await asyncio.sleep(30)  # Wait for sensors to report initial data
        while not shutdown_event.is_set():
            status = pump.get_status()
            ta = status.get("tank_a") or {}
            tb = status.get("tank_b") or {}
            history.record(
                ta.get("level_pct") if ta else None,
                tb.get("level_pct") if tb else None,
            )
            await asyncio.sleep(_HISTORY_RECORD_INTERVAL_S)

    async def state_machine_loop():
        await pump.run()

    tasks = [
        asyncio.create_task(state_machine_loop(), name="state-machine"),
        asyncio.create_task(heartbeat_loop(), name="heartbeat"),
        asyncio.create_task(power_poll_loop(), name="power-poll"),
        asyncio.create_task(device_poll_loop(), name="device-poll"),
        asyncio.create_task(simulated_sensor_loop(), name="simulated-sensors"),
        asyncio.create_task(state_publish_loop(), name="state-publish"),
        asyncio.create_task(history_record_loop(), name="history-record"),
        asyncio.create_task(shutdown_event.wait(), name="shutdown-watcher"),
    ]

    log.info("Controller running")

    # Wait until shutdown is requested
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Cancel all remaining tasks
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    # ── Shutdown: safety-first pump OFF ──
    log.info("Shutdown: turning pump OFF")
    await plug.turn_off()
    mqtt.stop()
    log.info("Controller stopped")


def main() -> None:
    config = load_config()
    setup_logging(config)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
