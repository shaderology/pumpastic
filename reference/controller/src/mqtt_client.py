"""
MQTT client wrapper for the Pumpastic controller.

Handles:
  - Connection + auto-reconnect to Mosquitto broker
  - Subscription to tank sensor topics
  - Publishing pump state and controller heartbeat
  - LWT (Last Will) for controller offline detection
"""
import datetime
import json
import logging
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

log = logging.getLogger("pumpastic.mqtt")

# Topics
TOPIC_TANK_A = "rainwater/tank_a/level/state"
TOPIC_TANK_B = "rainwater/tank_b/level/state"
TOPIC_TANK_A_STATUS = "rainwater/tank_a/status"
TOPIC_TANK_B_STATUS = "rainwater/tank_b/status"
TOPIC_PUMP_STATE = "rainwater/pump/state"
TOPIC_PUMP_POWER = "rainwater/pump/power"
TOPIC_CTRL_STATUS = "rainwater/controller/status"
TOPIC_CTRL_HEARTBEAT = "rainwater/controller/heartbeat"


class MqttClient:
    """
    Wraps paho-mqtt. Runs the network loop in a background thread via loop_start().
    Sensor data is passed to the controller via the on_sensor_data callback.
    """

    def __init__(self, config: dict, on_sensor_data: Callable[[str, dict], None]):
        self._broker = config["mqtt"]["broker"]
        self._port = int(config["mqtt"]["port"])
        self._client_id = config["mqtt"].get("client_id", "pumpastic-controller")
        self._on_sensor_data = on_sensor_data
        self._tank_config = config.get("tanks", {})
        self._tank_a_lwt: Optional[str] = None   # "online" / "offline" / None
        self._tank_b_lwt: Optional[str] = None

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            clean_session=True,
        )
        self._client.will_set(TOPIC_CTRL_STATUS, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect to broker and start the background network loop."""
        log.info("Connecting to MQTT broker %s:%d", self._broker, self._port)
        self._client.connect(self._broker, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        """Publish offline status, then disconnect."""
        try:
            self._publish(TOPIC_CTRL_STATUS, "offline", qos=1, retain=True)
            time.sleep(0.3)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()

    # ── Publishing ────────────────────────────────────────────────────────────

    def publish_pump_state(self, status: dict) -> None:
        """Publish controller state snapshot as retained JSON."""
        self._publish(TOPIC_PUMP_STATE, json.dumps(status), qos=1, retain=True)

    def publish_pump_power(self, watts: float) -> None:
        """Publish energy reading from the Tapo plug."""
        self._publish(
            TOPIC_PUMP_POWER,
            json.dumps({"watts": watts}),
            qos=0,
            retain=False,
        )

    def publish_online(self) -> None:
        """Re-assert the retained online status.

        The client uses a fixed client_id, so when a new session connects the broker
        kicks the previous one and fires its retained LWT ("offline"). That can land
        *after* the new session's connect-time "online" publish, leaving the topic
        retained as "offline" while the controller is very much running — and any
        subscriber believes it. Re-asserting on the
        heartbeat interval self-heals the race within one heartbeat.
        """
        self._publish(TOPIC_CTRL_STATUS, "online", qos=1, retain=True)

    def publish_solar(self, topic: str, payload: str) -> None:
        """Solar snapshot (retained, QoS 0) — informational, see solar.py."""
        self._publish(topic, payload, qos=0, retain=True)

    def publish_heartbeat(self) -> None:
        """Periodic alive ping."""
        self._publish(
            TOPIC_CTRL_HEARTBEAT,
            json.dumps({"ts": datetime.datetime.now().isoformat()}),
            qos=0,
            retain=False,
        )

    def publish_simulated_sensor(self, sensor_id: str, level_pct: float) -> None:
        """
        Publish a fake sensor reading to the MQTT broker.
        Used by the dashboard Controls tab to exercise the state machine.
        The payload is tagged with "simulated": true so logs can distinguish it.
        """
        tank_cfg = self._tank_config.get(sensor_id, {})
        # height_cm is the sensor face to tank floor; overflow_height_cm is the floor
        # to the overflow outlet. level_pct is scaled to the overflow height (100% =
        # at the outlet), so the water depth is a fraction of the overflow height and
        # the sensor reads the remainder. Older configs without overflow_height_cm
        # fall back to the sensor height, which reproduces the previous behaviour.
        height_cm = float(tank_cfg.get("height_cm", 220.0))
        overflow_cm = float(tank_cfg.get("overflow_height_cm", height_cm))
        capacity_l = float(tank_cfg.get("capacity_litres", 4820.0))
        distance_cm = height_cm - (level_pct / 100.0) * overflow_cm
        volume_l = capacity_l * (level_pct / 100.0)
        topic = f"rainwater/{sensor_id}/level/state"
        payload = {
            "sensor_id": sensor_id,
            "distance_cm": round(distance_cm, 1),
            "level_pct": round(level_pct, 1),
            "volume_l": round(volume_l, 0),
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "simulated": True,
        }
        self._publish(topic, json.dumps(payload), qos=0, retain=False)
        log.info(
            "Simulated %s at %.1f%% (distance=%.1f cm, volume=%.0f L)",
            sensor_id, level_pct, distance_cm, volume_l,
        )

    @property
    def is_connected(self) -> bool:
        """True if the paho client is currently connected to the broker."""
        return self._client.is_connected()

    @property
    def tank_a_online(self) -> Optional[bool]:
        """True=online, False=offline, None=no LWT received yet."""
        if self._tank_a_lwt is None:
            return None
        return self._tank_a_lwt == "online"

    @property
    def tank_b_online(self) -> Optional[bool]:
        """True=online, False=offline, None=no LWT received yet."""
        if self._tank_b_lwt is None:
            return None
        return self._tank_b_lwt == "online"

    # ── paho-mqtt callbacks ───────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected to %s:%d", self._broker, self._port)
            client.publish(TOPIC_CTRL_STATUS, "online", qos=1, retain=True)
            client.subscribe(TOPIC_TANK_A, qos=0)
            client.subscribe(TOPIC_TANK_B, qos=0)
            client.subscribe(TOPIC_TANK_A_STATUS, qos=1)
            client.subscribe(TOPIC_TANK_B_STATUS, qos=1)
            log.info("Subscribed to tank sensor and status topics")
        else:
            log.error("MQTT connect failed: reason_code=%s", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if reason_code != 0:
            log.warning("MQTT unexpected disconnect (rc=%s) — paho will reconnect", reason_code)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic

        # LWT status messages are plain strings, not JSON
        if topic == TOPIC_TANK_A_STATUS:
            self._tank_a_lwt = msg.payload.decode().strip()
            log.debug("Tank A LWT: %s", self._tank_a_lwt)
            return
        if topic == TOPIC_TANK_B_STATUS:
            self._tank_b_lwt = msg.payload.decode().strip()
            log.debug("Tank B LWT: %s", self._tank_b_lwt)
            return

        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warning("Unparseable message on %s: %s", topic, e)
            return

        if topic == TOPIC_TANK_A:
            self._on_sensor_data("tank_a", payload)
        elif topic == TOPIC_TANK_B:
            self._on_sensor_data("tank_b", payload)
        else:
            log.debug("Unhandled topic: %s", topic)

    def _publish(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        try:
            result = self._client.publish(topic, payload, qos=qos, retain=retain)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                log.warning("Publish to %s failed: rc=%d", topic, result.rc)
        except Exception as e:
            log.warning("Publish exception on %s: %s", topic, e)
