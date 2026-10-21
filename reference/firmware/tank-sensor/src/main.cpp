/**
 * Pumpastic Tank Sensor Firmware
 * ESP32-S3 + JSN-SR04T V3.3 Waterproof Ultrasonic Sensor
 *
 * Reads water level via ultrasonic distance measurement,
 * publishes JSON to MQTT broker on Raspberry Pi 3.
 *
 * Phase 1: Serial output + MQTT publishing
 * Phase 4: Deep sleep + solar power
 */

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include "config.h"

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
Adafruit_NeoPixel led(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

unsigned long lastReadTime = 0;
unsigned long bootTime = 0;

// Link liveness. WiFi.status() keeps reporting WL_CONNECTED long after the AP has
// stopped forwarding our frames, so the station sits there "connected" with a dead
// link and never reconnects. Track the last moment the MQTT session was proven
// alive and hard-reset the radio when that goes stale, regardless of what
// WiFi.status() claims.
//
// Liveness comes from the MQTT session itself, NOT from sensor publishes:
// PubSubClient sends PINGREQ every keepalive/2 and tears the session down when the
// broker stops answering, so a live session proves packets are flowing both ways.
// Keying this off publishes instead would make a faulty sensor look like a dead
// link and bounce a perfectly healthy radio every LINK_STALE_MS.
unsigned long lastGoodLink = 0;

// ─────────────────────────────────────────────
// LED State Machine (non-blocking)
// ─────────────────────────────────────────────
//
//  BOOT         Purple fast blink  — WiFi/MQTT connecting
//  IDLE         Blue 1 Hz blink    — connected, waiting for next reading
//  SENSOR_OK    Green solid 1 s    — valid reading published (auto → IDLE)
//  SENSOR_FAIL  Amber fast blink   — connected but sensor returning no valid echoes
//  FAULT        Red solid          — WiFi or MQTT disconnected
//

enum LedState { LED_BOOT, LED_IDLE, LED_SENSOR_OK, LED_SENSOR_FAIL, LED_FAULT, LED_OTA };

LedState      ledState          = LED_BOOT;
unsigned long ledStateEnteredAt = 0;
unsigned long lastLedToggle     = 0;
bool          ledPhase          = false;

void ledSet(uint8_t r, uint8_t g, uint8_t b) {
    led.setPixelColor(0, led.Color(r, g, b));
    led.show();
}

void ledTransition(LedState next) {
    ledState          = next;
    ledStateEnteredAt = millis();
    lastLedToggle     = millis();
    ledPhase          = false;
}

void updateLed() {
    unsigned long now = millis();

    switch (ledState) {
        case LED_BOOT:
            // Purple fast blink while connecting
            if (now - lastLedToggle >= 150) {
                lastLedToggle = now;
                ledPhase = !ledPhase;
                ledSet(ledPhase ? 40 : 0, 0, ledPhase ? 40 : 0);
            }
            break;

        case LED_IDLE:
            // Blue 1 Hz blink: connected, waiting for next reading
            if (now - lastLedToggle >= 500) {
                lastLedToggle = now;
                ledPhase = !ledPhase;
                ledSet(0, 0, ledPhase ? 40 : 0);
            }
            break;

        case LED_SENSOR_OK:
            // Solid green for 1 s, then back to IDLE
            if (!ledPhase) { ledSet(0, 60, 0); ledPhase = true; }  // set once on entry
            if (now - ledStateEnteredAt >= 1000) {
                ledTransition(LED_IDLE);
            }
            break;

        case LED_SENSOR_FAIL:
            // Amber fast blink: connected but no valid sensor readings
            if (now - lastLedToggle >= 120) {
                lastLedToggle = now;
                ledPhase = !ledPhase;
                ledSet(ledPhase ? 60 : 0, ledPhase ? 20 : 0, 0);
            }
            break;

        case LED_FAULT:
            // Solid red: WiFi or MQTT down
            if (!ledPhase) { ledSet(60, 0, 0); ledPhase = true; }  // set once on entry
            break;

        case LED_OTA:
            // Cyan fast blink: OTA update in progress
            if (now - lastLedToggle >= 100) {
                lastLedToggle = now;
                ledPhase = !ledPhase;
                ledSet(0, ledPhase ? 40 : 0, ledPhase ? 40 : 0);
            }
            break;
    }
}

// ─────────────────────────────────────────────
// Sensor Reading with Median Filter
// ─────────────────────────────────────────────

// Raw pulseIn() results from the most recent burst, kept purely for diagnostics.
long lastEchoDuration = 0;
long lastRawEcho[NUM_SAMPLES] = {0};

float readSingleDistance() {
    // Clear trigger
    digitalWrite(TRIG_PIN, LOW);
    delayMicroseconds(2);

    // Send trigger pulse
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(TRIGGER_PULSE_US);
    digitalWrite(TRIG_PIN, LOW);

    // Measure echo pulse duration
    long duration = pulseIn(ECHO_PIN, HIGH, ECHO_TIMEOUT_US);
    lastEchoDuration = duration;

    if (duration == 0) return -1.0;  // No echo received

    // Calculate distance in cm
    // Speed of sound ≈ 343 m/s at 20°C = 0.0343 cm/µs
    float distance = (duration * 0.0343) / 2.0;

    // Validate range
    if (distance < SENSOR_MIN_CM || distance > SENSOR_MAX_CM) {
        return -1.0;  // Out of valid range
    }

    return distance;
}

float readDistanceMedian() {
    float samples[NUM_SAMPLES];
    int validCount = 0;

    for (int i = 0; i < NUM_SAMPLES; i++) {
        float d = readSingleDistance();
        lastRawEcho[i] = lastEchoDuration;
        if (d > 0) {
            samples[validCount++] = d;
        }
        delay(SAMPLE_DELAY_MS);
    }

    // Need at least 3 valid readings
    if (validCount < 3) {
        Serial.printf("[SENSOR] Not enough valid readings (%d/%d). Raw echo us:",
                      validCount, NUM_SAMPLES);
        for (int i = 0; i < NUM_SAMPLES; i++) Serial.printf(" %ld", lastRawEcho[i]);
        Serial.println("   (0 = no echo at all)");
        return -1.0;
    }

    // Sort for median (insertion sort — small array)
    for (int i = 1; i < validCount; i++) {
        float key = samples[i];
        int j = i - 1;
        while (j >= 0 && samples[j] > key) {
            samples[j + 1] = samples[j];
            j--;
        }
        samples[j + 1] = key;
    }

    return samples[validCount / 2];
}

// ─────────────────────────────────────────────
// Water Level Calculations
// ─────────────────────────────────────────────

float distanceToLevelPct(float distance_cm) {
    float water_depth = SENSOR_HEIGHT_CM - distance_cm;
    // Scaled to the overflow outlet, so 100% = full. Water can physically sit above
    // the outlet for a moment during heavy inflow; constrain() clamps that to 100.
    float pct = (water_depth / OVERFLOW_HEIGHT_CM) * 100.0;
    return constrain(pct, 0.0, 100.0);
}

float levelToVolumeLitres(float level_pct) {
    return (level_pct / 100.0) * TANK_CAPACITY_L;
}

// ─────────────────────────────────────────────
// WiFi Connection
// ─────────────────────────────────────────────

// Diagnostic: report the reason code whenever the radio drops or fails to
// associate — this is what distinguishes a bad password from a missing AP.
void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
        Serial.printf("[WIFI] disconnect reason: %d\n",
                      info.wifi_sta_disconnected.reason);
    }
}

// Diagnostic: list the APs this radio can actually hear, so "cannot associate"
// can be told apart from "AP not in range".
void scanForAP() {
    Serial.println("[WIFI] Scanning...");
    int n = WiFi.scanNetworks();
    if (n <= 0) {
        Serial.println("[WIFI] scan found no networks");
        return;
    }
    for (int i = 0; i < n; i++) {
        Serial.printf("[WIFI]   %-24s ch%-3d %4d dBm  %s%s\n",
                      WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i),
                      WiFi.BSSIDstr(i).c_str(),
                      WiFi.SSID(i) == WIFI_SSID ? "  <-- target" : "");
    }
    WiFi.scanDelete();
}

// Tear the radio all the way down before reconnecting. Calling WiFi.begin() on top
// of a half-associated supplicant is what produces the bursts of "authentication
// failure" entries in the router log, and repeated failures get the client
// rate-limited by the AP — so always restart from a clean state.
void resetRadio() {
    mqttClient.disconnect();
    WiFi.disconnect(true, true);   // drop association, power down radio, clear config
    WiFi.mode(WIFI_OFF);
    delay(500);
}

bool connectWiFi() {
    Serial.printf("[WIFI] Connecting to %s (static IP %s)", WIFI_SSID,
                  STATIC_IP.toString().c_str());

    WiFi.setAutoReconnect(false);  // we manage reconnects manually (always with static IP)
    WiFi.persistent(false);        // don't save credentials to flash
    WiFi.mode(WIFI_STA);

    // Register once: onEvent() appends a handler on every call, so registering inside
    // the reconnect path stacked duplicates and printed each reason code 2-3 times.
    static bool eventRegistered = false;
    if (!eventRegistered) {
        WiFi.onEvent(onWiFiEvent);
        eventRegistered = true;
    }

    // Must follow WiFi.mode(): the radio has to exist before its power can be set.
    WiFi.setTxPower(WIFI_TX_POWER);
    WiFi.setHostname(DEVICE_NAME);

    // Modem sleep is the default and it is what makes this node drop off the AP after
    // a few idle minutes: we send ~3 KB per 10 min, so the radio spends nearly all of
    // its time asleep and the AP eventually ages the station out. Keep it awake —
    // this node is mains/USB powered, so there is nothing to save.
    WiFi.setSleep(false);

    WiFi.config(STATIC_IP, GATEWAY_IP, SUBNET_MASK, DNS_IP);
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    // Allow up to 30s — first cold boot needs more time for radio init
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 60) {
        // Pump LED updates through the 500ms wait so the blink stays crisp
        for (int i = 0; i < 5; i++) { delay(100); updateLed(); }
        Serial.print(".");
        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[WIFI] Connected! IP: %s  GW: %s  RSSI: %d dBm  TX: %.1f dBm\n",
                      WiFi.localIP().toString().c_str(),
                      WiFi.gatewayIP().toString().c_str(),
                      WiFi.RSSI(),
                      WiFi.getTxPower() / 4.0f);   // reported in 0.25 dBm steps
        lastGoodLink = millis();
        return true;
    } else {
        Serial.printf("\n[WIFI] FAILED — status: %d\n", WiFi.status());
        Serial.printf("[WIFI] MAC: %s\n", WiFi.macAddress().c_str());
        scanForAP();
        ledTransition(LED_FAULT);
        return false;
    }
}

// ─────────────────────────────────────────────
// MQTT Connection
// ─────────────────────────────────────────────

bool connectMQTT() {
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    mqttClient.setKeepAlive(60);  // 60 s gives more slack than the 15 s default

    Serial.printf("[MQTT] Connecting to %s:%d...\n", MQTT_BROKER, MQTT_PORT);

    // Connect with LWT (Last Will and Testament)
    bool connected = mqttClient.connect(
        DEVICE_NAME,
        NULL, NULL,                         // No auth
        MQTT_TOPIC_STATUS,                  // LWT topic
        1,                                  // LWT QoS 1
        true,                               // LWT retained
        MQTT_LWT_OFFLINE                    // LWT message
    );

    if (connected) {
        Serial.println("[MQTT] Connected!");
        lastGoodLink = millis();
        // Publish online status
        mqttClient.publish(MQTT_TOPIC_STATUS, MQTT_LWT_ONLINE, true);
        ledTransition(LED_IDLE);
        return true;
    } else {
        Serial.printf("[MQTT] FAILED, rc=%d\n", mqttClient.state());
        ledTransition(LED_FAULT);
        return false;
    }
}

unsigned long lastMqttAttempt  = 0;
unsigned long lastWifiAttempt  = 0;

void ensureConnected() {
    unsigned long now = millis();

    // Dead-link watchdog. If nothing has provably reached the broker for
    // LINK_STALE_MS we are in the "associated but unreachable" state, which
    // WiFi.status() cannot see. Bounce the radio from scratch.
    if (now - lastGoodLink >= LINK_STALE_MS) {
        Serial.printf("[WIFI] No MQTT session for %lus (status=%d, RSSI=%d) "
                      "— forcing full reconnect\n",
                      (now - lastGoodLink) / 1000, WiFi.status(), WiFi.RSSI());
        lastGoodLink = now;   // restart the clock so we retry on an interval, not every loop
        ledTransition(LED_FAULT);
        resetRadio();
        if (connectWiFi()) connectMQTT();
        return;
    }

    if (WiFi.status() != WL_CONNECTED) {
        ledTransition(LED_FAULT);
        // Back off between attempts — hammering the AP with auth requests is what
        // gets this MAC temporarily blocked by the AP's station tracker.
        if (now - lastWifiAttempt >= 30000) {
            lastWifiAttempt = now;
            resetRadio();
            if (connectWiFi()) connectMQTT();
        }
        return;
    }

    if (!mqttClient.connected() && (now - lastMqttAttempt >= 10000)) {
        lastMqttAttempt = now;
        ledTransition(LED_FAULT);
        connectMQTT();
    }
}

// ─────────────────────────────────────────────
// Publish Sensor Data
// ─────────────────────────────────────────────

void publishReading(float distance_cm, float level_pct, float volume_l) {
    JsonDocument doc;
    doc["sensor_id"] = SENSOR_ID;
    doc["distance_cm"] = round(distance_cm * 10.0) / 10.0;  // 1 decimal
    doc["level_pct"] = round(level_pct * 10.0) / 10.0;
    doc["volume_l"] = (int)volume_l;
    // Water above the overflow outlet (inflow beating the outlet). level_pct is
    // clamped to 100, so flag it explicitly rather than hide it.
    if (distance_cm < SENSOR_HEIGHT_CM - OVERFLOW_HEIGHT_CM) doc["overfull"] = true;
#ifdef SERIAL_BRIDGE_MODE
    doc["link"] = "usb";          // no radio — say so rather than reporting a fake RSSI
#else
    doc["rssi"] = WiFi.RSSI();
#endif
    doc["uptime_s"] = (millis() - bootTime) / 1000;

    char payload[256];
    serializeJson(doc, payload, sizeof(payload));

    #ifdef SERIAL_BRIDGE_MODE
    // One framed line per reading, emitted as a SINGLE write. Printing the prefix and
    // payload separately let USB split the record across two packets, and a reader
    // timing out between them saw a truncated line (observed 2026-09-18).
    char line[320];
    snprintf(line, sizeof(line), "%s%s", BRIDGE_PREFIX, payload);
    Serial.println(line);
#else
    bool published = mqttClient.publish(MQTT_TOPIC_LEVEL, payload, true);  // retained
    if (published) lastGoodLink = millis();

    Serial.printf("[MQTT] %s -> %s\n",
                  MQTT_TOPIC_LEVEL,
                  published ? payload : "PUBLISH FAILED");
#endif
}

// ─────────────────────────────────────────────
// Setup & Loop
// ─────────────────────────────────────────────

void setup() {
    Serial.begin(115200);

    // Init LED immediately so we have visual feedback during boot
    led.begin();
    led.setBrightness(LED_BRIGHTNESS);
    led.clear();
    led.show();
    ledTransition(LED_BOOT);

    delay(2000);  // Wait for serial monitor + radio init on cold boot

    bootTime = millis();
    lastGoodLink = millis();   // grace period before the dead-link watchdog can fire

    Serial.println("\n==============================");
    Serial.println("  Pumpastic Tank Sensor");
    Serial.printf("  ID: %s\n", SENSOR_ID);
    Serial.println("==============================\n");

    // Configure sensor pins
    pinMode(TRIG_PIN, OUTPUT);
    pinMode(ECHO_PIN, INPUT);
    digitalWrite(TRIG_PIN, LOW);

#ifdef SERIAL_BRIDGE_MODE
    // No radio at all: readings leave over USB serial and the Pi bridge republishes
    // them. Keeping the radio off also removes it as a power and noise source.
    WiFi.mode(WIFI_OFF);
    Serial.println("[BRIDGE] Serial bridge mode — WiFi disabled");
    ledTransition(LED_IDLE);
#else
    // Connect WiFi and MQTT
    connectWiFi();
    connectMQTT();
#endif

#ifndef SERIAL_BRIDGE_MODE
    // OTA update support — allows flashing over WiFi without USB cable
    ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.onStart([]() {
        Serial.println("[OTA] Starting update...");
        ledTransition(LED_OTA);
    });
    ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
        Serial.printf("[OTA] Progress: %u%%\n", (progress / (total / 100)));
        updateLed();
    });
    ArduinoOTA.onEnd([]() {
        Serial.println("[OTA] Done. Rebooting...");
    });
    ArduinoOTA.onError([](ota_error_t error) {
        Serial.printf("[OTA] Error[%u]\n", error);
        ledTransition(LED_FAULT);
    });
    ArduinoOTA.begin();
    Serial.println("[OTA] Ready");
#endif

    Serial.println("[SETUP] Ready. Taking readings...\n");
}

void loop() {
    // Update LED every iteration (non-blocking)
    updateLed();

#ifndef SERIAL_BRIDGE_MODE
    ArduinoOTA.handle();

    // Maintain MQTT connection. A live session is proof of end-to-end traffic, so
    // it is what keeps the dead-link watchdog satisfied.
    mqttClient.loop();
    if (mqttClient.connected()) lastGoodLink = millis();
    ensureConnected();
#endif

    unsigned long now = millis();
    if (now - lastReadTime >= READ_INTERVAL_MS) {
        lastReadTime = now;

        // Read sensor
        float distance = readDistanceMedian();

        if (distance > 0) {
            float level = distanceToLevelPct(distance);
            float volume = levelToVolumeLitres(level);

            Serial.printf("[SENSOR] Distance: %.1f cm | Level: %.1f%% | Volume: %d L\n",
                          distance, level, (int)volume);

            publishReading(distance, level, volume);
            ledTransition(LED_SENSOR_OK);    // Green 1s flash
        } else {
            Serial.println("[SENSOR] Reading failed — no valid data");
            ledTransition(LED_SENSOR_FAIL);  // Amber blink until next valid read
        }
    }
}
