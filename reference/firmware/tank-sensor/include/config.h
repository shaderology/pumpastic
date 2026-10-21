#pragma once

// =============================================================================
// Pumpastic Tank Sensor — Configuration
// =============================================================================

// --- Sensor Identity ---
// Change to "tank_b" and IP .51 for the second node
#define SENSOR_ID       "tank_a"
#define DEVICE_NAME     "pumpastic-tank-a"

// --- Serial bridge mode ---
// Tank A sits in the same weatherproof box as the Raspberry Pi, on a ~10 cm USB lead.
// Its PCB antenna is detuned (see docs/08-lessons-learned.md), and at the tank it
// reaches the AP at -78 dBm with ~72% of frames retried — good enough to dribble out a
// 1 KB reading, not good enough to finish an OTA upload. With this defined the node
// never brings up the radio at all: it prints each reading to USB serial and a bridge
// service on the Pi republishes it to MQTT. Comment out to return to the WiFi path.
#define SERIAL_BRIDGE_MODE

// Marker prefix for bridge lines, so the parser can ignore boot chatter and logs.
#define BRIDGE_PREFIX   "#PUMPASTIC "

// --- WiFi ---
// WIFI_SSID, WIFI_PASS and OTA_PASSWORD live in secrets.h (gitignored).
// Copy secrets.h.example to secrets.h and fill it in.
#include "secrets.h"

// Radio transmit power. The default is the ESP32's maximum (~19.5 dBm), and on cheap
// ESP32-S3 dev boards that can distort or sag the 3V3 rail during transmit bursts:
// receive keeps working (scans look perfect), but the AP cannot decode what we send, so
// authentication stalls and established sessions go silent until the AP ages us out.
// Diagnosed 2026-09-17 — see docs/08-lessons-learned.md. Values are the
// wifi_power_t enum: WIFI_POWER_8_5dBm, _11dBm, _13dBm, _15dBm, _17dBm, _19_5dBm.
// 2026-09-18: raised from _8_5dBm to _15dBm. 8.5 was chosen when the board sat ~1 m
// from the AP; installed at the tank it reads -78 dBm with ~72% of frames retried, so
// the power cut had become the wrong trade-off. Step back down if the router log shows
// auth_rsp with no assoc_req.
#define WIFI_TX_POWER   WIFI_POWER_15dBm

// Static IP — skips DHCP for a faster reconnect (WiFi nodes only)
// EXAMPLE addresses — Tank A: 192.168.1.40  |  Tank B: 192.168.1.41
#define STATIC_IP       IPAddress(192, 168, 1, 40)
#define GATEWAY_IP      IPAddress(192, 168, 1, 1)
#define SUBNET_MASK     IPAddress(255, 255, 255, 0)
#define DNS_IP          IPAddress(192, 168, 1, 1)  // Gateway doubles as DNS

// --- MQTT ---
#define MQTT_BROKER     "192.168.1.10"   // EXAMPLE: your Pi
#define MQTT_PORT       1883
#define MQTT_TOPIC_LEVEL    "rainwater/" SENSOR_ID "/level/state"
#define MQTT_TOPIC_STATUS   "rainwater/" SENSOR_ID "/status"
#define MQTT_LWT_ONLINE     "online"
#define MQTT_LWT_OFFLINE    "offline"

// --- Ultrasonic Sensor (JSN-SR04T V3.3 — production) ---
#define TRIG_PIN        5
#define ECHO_PIN        6
#define TRIGGER_PULSE_US    20      // JSN-SR04T: 20µs
#define ECHO_TIMEOUT_US     30000   // 30ms timeout (~5m max range)
#define NUM_SAMPLES         5       // Median filter sample count
#define SAMPLE_DELAY_MS     50      // Delay between samples

// --- Tank Dimensions ---
// Measured 2026-09-15. Tank A is 1.9 m diameter; the sensor sits on the tank top,
// 2.2 m above the floor, and the overflow outlet is at 1.7 m.
//
// level_pct is measured against the OVERFLOW height, not the sensor height, so 100%
// means "at the overflow outlet" — the operational definition of full. Scaling
// against sensor height instead would put Tank A's overflow at 77%, which would make
// the documented 90% start threshold physically unreachable and the pump would never
// run. See docs/07-calibration.md.
#define SENSOR_HEIGHT_CM    220.0   // sensor face to tank floor
#define OVERFLOW_HEIGHT_CM  170.0   // tank floor to overflow outlet  (= 100%)
#define TANK_CAPACITY_L     4820.0  // litres at overflow: pi*(1.9/2)^2*1.7 m3
// Minimum accepted distance. It must stay well ABOVE the dead zone (measured 22.7 cm
// on this unit). When a JSN-SR04T gets no usable echo it reports its minimum range,
// and with the old 21.0 floor those readings passed validation and became
// level_pct = 100%. On 2026-09-24 three such readings in a row (22.7, 22.9, 22.7 cm)
// satisfied the 94% start threshold and ran the pump for 26 minutes on a
// completely full-looking tank that was actually at ~68%.
//
// It was 45 cm (just under the 50 cm overflow) until 2026-10-19, when heavy rain
// out-ran both the pump and the overflow outlet: water rose to ~42 cm, every
// reading was rejected, Tank A went silent and the controller FAULTed the pump OFF
// exactly when the tank most needed emptying. Readings between this floor and the
// overflow distance are real overfill. They publish as 100% with "overfull": true.
#define SENSOR_MIN_CM       30.0
#define SENSOR_MAX_CM       600.0   // JSN-SR04T max range: ~6m

// --- Onboard RGB LED (WS2812 NeoPixel) ---
// GPIO 48 is standard for ESP32-S3-DevKitC-1. Adjust if your board differs.
#define LED_PIN         48
#define LED_COUNT       1
#define LED_BRIGHTNESS  40      // 0–255 (40 ≈ 16%, comfortable for desk use)

// --- OTA Updates ---
// OTA_PASSWORD comes from secrets.h. WiFi nodes only: SERIAL_BRIDGE_MODE has no OTA.

// --- Link Watchdog ---
// How long the MQTT session may stay down before the radio is torn down and rebuilt.
// Driven by MQTT keepalive (60 s), not by sensor publishes, so a faulty sensor never
// triggers it. Must comfortably exceed the keepalive or healthy nodes will bounce.
#define LINK_STALE_MS       150000  // 2.5 min (MQTT keepalive is 60 s)

// --- Timing ---
#define READ_INTERVAL_MS    30000   // 30 seconds between readings
