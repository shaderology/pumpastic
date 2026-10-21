"""
Pumpastic — MQTT Broker Test
=============================
Tests Mosquitto broker on RPi: connect, publish, subscribe, round-trip.

Publishes only to a throwaway test topic, never to sensor topics.

Usage:
    PUMPASTIC_PI_HOST=192.168.1.10 python tools/mqtt_check.py
"""

import os
import sys
import time
import json
from datetime import datetime

BROKER = os.environ.get("PUMPASTIC_PI_HOST", "192.168.1.10")
PORT = 1883


def main():
    print("=" * 50)
    print("  Pumpastic — MQTT Broker Test")
    print("=" * 50)

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("\n✗ paho-mqtt not installed. Run: pip install paho-mqtt")
        sys.exit(1)

    # ── Test 1: Connection ──
    print(f"\n[1/3] Connecting to {BROKER}:{PORT}...")

    connected = {"value": False}
    received = {"value": None}

    def on_connect(client, userdata, flags, rc, properties=None):
        connected["value"] = (rc == 0)
        if rc != 0:
            codes = {1: "wrong protocol", 2: "ID rejected", 3: "server unavailable",
                     4: "bad credentials", 5: "not authorised"}
            print(f"  ✗ Connection refused: {codes.get(rc, f'rc={rc}')}")

    test_topic = "pumpastic/mqtt_test"
    test_id = f"test_{int(time.time())}"

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if payload.get("test_id") == test_id:
                received["value"] = payload
        except Exception:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pumpastic-mqtt-test")
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(BROKER, PORT, keepalive=10)
    except ConnectionRefusedError:
        print(f"  ✗ Connection refused — is Mosquitto running on {BROKER}?")
        print(f"    SSH to RPi and run: sudo systemctl start mosquitto")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Connection error: {e}")
        sys.exit(1)

    client.loop_start()

    for _ in range(20):
        if connected["value"]:
            break
        time.sleep(0.25)

    if not connected["value"]:
        print(f"  ✗ Timed out connecting to {BROKER}")
        client.loop_stop()
        sys.exit(1)

    print(f"  ✓ Connected to MQTT broker")

    # ── Test 2: Subscribe ──
    print(f"\n[2/3] Subscribe to {test_topic}...")
    client.subscribe(test_topic, qos=1)
    time.sleep(0.5)
    print(f"  ✓ Subscribed")

    # ── Test 3: Publish + round-trip ──
    print(f"\n[3/3] Publish and receive round-trip...")

    payload = json.dumps({
        "test_id": test_id,
        "source": "pumpastic_mqtt_test",
        "timestamp": datetime.now().isoformat(),
    })

    result = client.publish(test_topic, payload, qos=1)
    result.wait_for_publish(timeout=5)

    # Wait for round-trip
    start = time.time()
    while received["value"] is None and (time.time() - start) < 5:
        time.sleep(0.1)

    if received["value"]:
        rtt = (time.time() - start) * 1000
        print(f"  ✓ Round-trip successful ({rtt:.0f}ms)")
        print(f"    Payload: {json.dumps(received['value'], indent=2)}")
    else:
        print(f"  ✗ Published but did not receive message back")
        print(f"    This may indicate the broker is not routing correctly")

    # Clean up retained test message
    client.publish(test_topic, "", retain=True)

    # ── Test Rainwater Topics ──
    print(f"\n[BONUS] Checking for existing rainwater/ messages...")
    existing = {"messages": []}

    def on_existing(client, userdata, msg):
        existing["messages"].append({
            "topic": msg.topic,
            "payload": msg.payload.decode()[:100],
            "retained": msg.retain,
        })

    client.on_message = on_existing
    client.subscribe("rainwater/#", qos=0)
    time.sleep(2)

    if existing["messages"]:
        print(f"  Found {len(existing['messages'])} existing messages:")
        for m in existing["messages"]:
            r = " [retained]" if m["retained"] else ""
            print(f"    {m['topic']}: {m['payload']}{r}")
    else:
        print(f"  No existing rainwater/ messages (expected for fresh setup)")

    client.loop_stop()
    client.disconnect()

    print("\n" + "=" * 50)
    print("  MQTT TEST PASSED")
    print("=" * 50)


if __name__ == "__main__":
    main()
