"""
Push notification alerter via ntfy.sh.

Configured in config.yaml under `alerts:`. Silent on failure —
alerting must never crash the controller.
"""
import logging
import threading
from typing import Optional

import requests

log = logging.getLogger("pumpastic.alerting")


class Alerter:
    """
    Sends push notifications to ntfy.sh for key pump events.
    All sends are fire-and-forget (background thread) so they never block
    the controller state machine.
    """

    def __init__(self, config: dict):
        alerts_cfg = config.get("alerts", {})
        # No default: ntfy topics are public, so anyone who knows the name can read
        # (and post to) it. An unset topic disables alerts rather than sharing one.
        self._topic = alerts_cfg.get("ntfy_topic") or ""
        self._server = alerts_cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        enabled_events = alerts_cfg.get("events", [])
        self._enabled: set = set(enabled_events)
        self._enabled_all = len(self._enabled) == 0  # empty list → all enabled

    def send(self, event: str, message: str, title: Optional[str] = None) -> None:
        """
        Send a notification for the given event type.
        Does nothing if the event is not in the enabled list.
        """
        if not self._topic:
            return
        if not self._enabled_all and event not in self._enabled:
            return
        t = threading.Thread(
            target=self._send_sync,
            args=(event, message, title or self._default_title(event)),
            daemon=True,
        )
        t.start()

    def _send_sync(self, event: str, message: str, title: str) -> None:
        url = f"{self._server}/{self._topic}"
        priority = "high" if event in ("fault", "max_runtime_exceeded") else "default"
        try:
            resp = requests.post(
                url,
                data=message.encode(),
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": f"droplet,{event}",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                log.debug("Alert sent: [%s] %s", event, message)
            else:
                log.warning("Alert HTTP %d for event %s", resp.status_code, event)
        except Exception as e:
            log.warning("Alert send failed (non-critical): %s", e)

    @staticmethod
    def _default_title(event: str) -> str:
        titles = {
            "pump_start":          "Pumpastic: Pump started",
            "pump_stop":           "Pumpastic: Pump stopped",
            "fault":               "Pumpastic: FAULT",
            "max_runtime_exceeded": "Pumpastic: Max runtime exceeded",
            "sensor_offline":      "Pumpastic: Sensor offline",
        }
        return titles.get(event, f"Pumpastic: {event}")
