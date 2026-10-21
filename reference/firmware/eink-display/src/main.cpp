/*
 * Pumpastic e-ink status display — PhotoPainter thin client.
 *
 * One wake cycle, then deep sleep:
 *   1. Power rails up (AXP2101), read battery + SHTC3.
 *   2. WiFi (cached BSSID/channel for a fast reconnect).
 *   3. POST telemetry, GET /display/frame.bin with If-None-Match.
 *   4. 200 → push the 192 000-byte frame to the panel (~25 s refresh).
 *      304 → nothing to draw (night, or unchanged).
 *   5. Deep sleep for X-Next-Wake-S; KEY or BOOT wakes early and forces a refresh.
 *
 * The device keeps no clock and makes no schedule decisions — the server does both.
 * Never blocks indefinitely: every wait has a timeout and every failure ends in sleep.
 */
#include <Arduino.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/rtc_io.h>
#include <esp_sleep.h>

#define XPOWERS_CHIP_AXP2101
#include <XPowersLib.h>

#include "config.h"

// ── State kept across deep sleep ──────────────────────────────────────────────
RTC_DATA_ATTR static char rtcEtag[40] = "";
RTC_DATA_ATTR static uint8_t rtcBssid[6];
RTC_DATA_ATTR static int32_t rtcChannel = 0;
RTC_DATA_ATTR static bool rtcBssidValid = false;
RTC_DATA_ATTR static uint32_t rtcFailures = 0;
RTC_DATA_ATTR static uint32_t rtcLastAwakeMs = 0;
RTC_DATA_ATTR static uint32_t rtcBootCount = 0;

static XPowersPMU pmu;
static bool pmuOk = false;
static SPIClass epdSpi(FSPI);

static void led(int pin, bool on) { digitalWrite(pin, on ? LOW : HIGH); }

// ── PMIC ────────────────────────────────────────────────────────────────────────
static void pmicInit() {
    pmuOk = pmu.begin(Wire, AXP2101_SLAVE_ADDRESS, PIN_I2C_SDA, PIN_I2C_SCL);
    if (!pmuOk) {
        Serial.println("[pmu] AXP2101 not found");
        return;
    }
    // Vendor rail setup: everything at 3.3 V. Which ALDO feeds what is undocumented,
    // so all of them stay on.
    pmu.setVbusCurrentLimit(XPOWERS_AXP2101_VBUS_CUR_LIM_2000MA);
    pmu.setDC1Voltage(3300);
    pmu.setALDO1Voltage(3300); pmu.enableALDO1();
    pmu.setALDO2Voltage(3300); pmu.enableALDO2();
    pmu.setALDO3Voltage(3300); pmu.enableALDO3();
    pmu.setALDO4Voltage(3300); pmu.enableALDO4();
    pmu.setPrechargeCurr(XPOWERS_AXP2101_PRECHARGE_50MA);
    pmu.setChargerConstantCurr(XPOWERS_AXP2101_CHG_CUR_500MA);
    pmu.setChargerTerminationCurr(XPOWERS_AXP2101_CHG_ITERM_25MA);
    pmu.enableBattVoltageMeasure();
    pmu.enableVbusVoltageMeasure();
    pmu.enableBattDetection();
}

// ── SHTC3 (0x70) ───────────────────────────────────────────────────────────────
static bool shtc3Cmd(uint16_t cmd) {
    Wire.beginTransmission(0x70);
    Wire.write(cmd >> 8);
    Wire.write(cmd & 0xFF);
    return Wire.endTransmission() == 0;
}

static uint8_t crc8(const uint8_t *d, int n) {
    uint8_t crc = 0xFF;
    for (int i = 0; i < n; i++) {
        crc ^= d[i];
        for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : crc << 1;
    }
    return crc;
}

static bool shtc3Read(float &tempC, float &rh) {
    if (!shtc3Cmd(0x3517)) return false;  // wake
    delay(1);
    if (!shtc3Cmd(0x7866)) return false;  // measure, T first, no clock stretching
    delay(15);
    uint8_t b[6];
    if (Wire.requestFrom(0x70, 6) != 6) return false;
    for (int i = 0; i < 6; i++) b[i] = Wire.read();
    shtc3Cmd(0xB098);                      // sleep
    if (crc8(b, 2) != b[2] || crc8(b + 3, 2) != b[5]) return false;
    tempC = -45.0f + 175.0f * ((b[0] << 8) | b[1]) / 65536.0f;
    rh = 100.0f * ((b[3] << 8) | b[4]) / 65536.0f;
    return true;
}

// ── E-paper ────────────────────────────────────────────────────────────────────
static bool epdWaitIdle(uint32_t timeoutMs = EPD_BUSY_TIMEOUT_MS) {
    uint32_t t0 = millis();
    while (digitalRead(PIN_EPD_BUSY) == LOW) {
        if (millis() - t0 > timeoutMs) {
            Serial.println("[epd] BUSY timeout");
            return false;
        }
        delay(10);
    }
    return true;
}

static void epdCmd(uint8_t c) {
    digitalWrite(PIN_EPD_DC, LOW);
    digitalWrite(PIN_EPD_CS, LOW);
    epdSpi.transfer(c);
    digitalWrite(PIN_EPD_CS, HIGH);
}

static void epdData(uint8_t d) {
    digitalWrite(PIN_EPD_DC, HIGH);
    digitalWrite(PIN_EPD_CS, LOW);
    epdSpi.transfer(d);
    digitalWrite(PIN_EPD_CS, HIGH);
}

static void epdCmdData(uint8_t c, std::initializer_list<uint8_t> data) {
    epdCmd(c);
    for (uint8_t d : data) epdData(d);
}

static bool epdInit() {
    pinMode(PIN_EPD_DC, OUTPUT);
    pinMode(PIN_EPD_CS, OUTPUT);
    pinMode(PIN_EPD_RST, OUTPUT);
    pinMode(PIN_EPD_BUSY, INPUT_PULLUP);
    digitalWrite(PIN_EPD_CS, HIGH);
    epdSpi.begin(PIN_EPD_SCK, -1, PIN_EPD_MOSI, -1);
    epdSpi.beginTransaction(SPISettings(EPD_SPI_HZ, MSBFIRST, SPI_MODE0));

    digitalWrite(PIN_EPD_RST, HIGH); delay(50);
    digitalWrite(PIN_EPD_RST, LOW);  delay(20);
    digitalWrite(PIN_EPD_RST, HIGH); delay(50);
    if (!epdWaitIdle(5000)) return false;
    delay(50);

    // Vendor init sequence, verbatim (display_bsp.cpp EPD_Init)
    epdCmdData(0xAA, {0x49, 0x55, 0x20, 0x08, 0x09, 0x18});
    epdCmdData(0x01, {0x3F});
    epdCmdData(0x00, {0x5F, 0x69});
    epdCmdData(0x03, {0x00, 0x54, 0x00, 0x44});
    epdCmdData(0x05, {0x40, 0x1F, 0x1F, 0x2C});
    epdCmdData(0x06, {0x6F, 0x1F, 0x17, 0x49});
    epdCmdData(0x08, {0x6F, 0x1F, 0x1F, 0x22});
    epdCmdData(0x30, {0x03});
    epdCmdData(0x50, {0x3F});
    epdCmdData(0x60, {0x02, 0x00});
    epdCmdData(0x61, {0x03, 0x20, 0x01, 0xE0});  // 800 x 480
    epdCmdData(0x84, {0x01});
    epdCmdData(0xE3, {0x2F});
    epdCmd(0x04);                                  // POWER_ON
    return epdWaitIdle(5000);
}

// 180° rotation in place: reverse byte order and swap the two pixels in each byte.
static void rotate180(uint8_t *fb) {
    for (size_t i = 0, j = FRAME_BYTES - 1; i < j; i++, j--) {
        uint8_t a = fb[i], b = fb[j];
        fb[i] = (b << 4) | (b >> 4);
        fb[j] = (a << 4) | (a >> 4);
    }
}

static bool epdShow(uint8_t *fb) {
    if (!epdInit()) return false;
    epdCmd(0x10);
    digitalWrite(PIN_EPD_DC, HIGH);
    digitalWrite(PIN_EPD_CS, LOW);
    for (size_t off = 0; off < FRAME_BYTES; off += 4096) {
        size_t n = min((size_t)4096, (size_t)(FRAME_BYTES - off));
        epdSpi.writeBytes(fb + off, n);
    }
    digitalWrite(PIN_EPD_CS, HIGH);

    epdCmd(0x04);                                  // POWER_ON
    if (!epdWaitIdle(5000)) return false;
    epdCmdData(0x06, {0x6F, 0x1F, 0x17, 0x49});   // booster, second setting
    uint32_t t0 = millis();
    epdCmdData(0x12, {0x00});                      // DISPLAY_REFRESH (~25 s)
    bool ok = epdWaitIdle();
    Serial.printf("[epd] refresh %s in %lu ms\n", ok ? "done" : "FAILED", millis() - t0);
    epdCmdData(0x02, {0x00});                      // POWER_OFF
    epdWaitIdle(5000);
    epdCmdData(0x07, {0xA5});                      // deep sleep (Waveshare 7in3e Sleep())
    epdSpi.endTransaction();
    return ok;
}

// ── Network ─────────────────────────────────────────────────────────────────────
static bool wifiConnect() {
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(DEVICE_NAME);
    WiFi.setSleep(false);
    for (int attempt = 0; attempt < 2; attempt++) {
        bool fast = attempt == 0 && rtcBssidValid;
        if (fast) WiFi.begin(WIFI_SSID, WIFI_PASS, rtcChannel, rtcBssid);
        else WiFi.begin(WIFI_SSID, WIFI_PASS);
        uint32_t t0 = millis();
        while (WiFi.status() != WL_CONNECTED && millis() - t0 < (fast ? 6000 : WIFI_TIMEOUT_MS)) delay(50);
        if (WiFi.status() == WL_CONNECTED) {
            memcpy(rtcBssid, WiFi.BSSID(), 6);
            rtcChannel = WiFi.channel();
            rtcBssidValid = true;
            Serial.printf("[wifi] %s %s ch%d %ddBm in %lu ms\n", fast ? "fast" : "full",
                          WiFi.localIP().toString().c_str(), rtcChannel, WiFi.RSSI(), millis() - t0);
            return true;
        }
        WiFi.disconnect(true);
        rtcBssidValid = false;  // the AP may have moved channel; fall back to a full scan
        delay(100);
    }
    Serial.println("[wifi] connect failed");
    return false;
}

static void postTelemetry(const String &json) {
    HTTPClient http;
    http.setTimeout(5000);
    if (!http.begin(SERVER_BASE TELEMETRY_PATH)) return;
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(json);
    Serial.printf("[http] telemetry → %d\n", code);
    http.end();
}

enum FetchResult { FETCH_NEW, FETCH_UNCHANGED, FETCH_FAILED };

static FetchResult fetchFrame(uint8_t *fb, bool force, uint32_t &nextWakeS) {
    HTTPClient http;
    http.setTimeout(HTTP_TIMEOUT_MS);
    if (!http.begin(SERVER_BASE FRAME_PATH)) return FETCH_FAILED;
    const char *keys[] = {"X-Next-Wake-S", "ETag"};
    http.collectHeaders(keys, 2);
    if (force) http.addHeader("X-Force", "1");
    else if (rtcEtag[0]) http.addHeader("If-None-Match", rtcEtag);

    int code = http.GET();
    String wake = http.header("X-Next-Wake-S");
    if (wake.length()) nextWakeS = wake.toInt();
    Serial.printf("[http] frame → %d, next wake %s s\n", code, wake.c_str());

    if (code == HTTP_CODE_NOT_MODIFIED) { http.end(); return FETCH_UNCHANGED; }
    if (code != HTTP_CODE_OK) { http.end(); return FETCH_FAILED; }

    WiFiClient *s = http.getStreamPtr();
    size_t got = 0;
    uint32_t t0 = millis();
    while (got < FRAME_BYTES && millis() - t0 < HTTP_TIMEOUT_MS) {
        size_t avail = s->available();
        if (avail) got += s->readBytes(fb + got, min(avail, (size_t)(FRAME_BYTES - got)));
        else if (!s->connected()) break;
        else delay(2);
    }
    String etag = http.header("ETag");
    http.end();
    if (got != FRAME_BYTES) {
        Serial.printf("[http] short frame: %u bytes\n", (unsigned)got);
        return FETCH_FAILED;
    }
    strlcpy(rtcEtag, etag.c_str(), sizeof(rtcEtag));
    return FETCH_NEW;
}

// ── Sleep ───────────────────────────────────────────────────────────────────────
[[noreturn]] static void sleepFor(uint32_t seconds) {
    seconds = constrain(seconds, (uint32_t)SLEEP_MIN_S, (uint32_t)SLEEP_MAX_S);
    rtcLastAwakeMs = millis();
    Serial.printf("[sleep] %lu s (awake %lu ms)\n", (unsigned long)seconds, (unsigned long)rtcLastAwakeMs);
    Serial.flush();
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    led(PIN_LED_RED, false);
    led(PIN_LED_GREEN, false);

    // Don't go to sleep with a button still held — EXT1 "any low" would wake at once.
    uint32_t t0 = millis();
    while ((digitalRead(PIN_BTN_BOOT) == LOW || digitalRead(PIN_BTN_KEY) == LOW) && millis() - t0 < 10000) delay(10);

    rtc_gpio_pullup_en((gpio_num_t)PIN_BTN_KEY);
    rtc_gpio_pulldown_dis((gpio_num_t)PIN_BTN_KEY);
    rtc_gpio_pullup_en((gpio_num_t)PIN_BTN_BOOT);
    rtc_gpio_pulldown_dis((gpio_num_t)PIN_BTN_BOOT);
    esp_sleep_enable_ext1_wakeup((1ULL << PIN_BTN_BOOT) | (1ULL << PIN_BTN_KEY), ESP_EXT1_WAKEUP_ANY_LOW);
    esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
    esp_deep_sleep_start();
}

static uint32_t backoffSeconds() {
    return rtcFailures <= 1 ? BACKOFF_1_S : rtcFailures == 2 ? BACKOFF_2_S : BACKOFF_MAX_S;
}

// ── Main ────────────────────────────────────────────────────────────────────────
void setup() {
    pinMode(PIN_AUDIO_PA, OUTPUT);
    digitalWrite(PIN_AUDIO_PA, LOW);
    pinMode(PIN_LED_GREEN, OUTPUT);
    pinMode(PIN_LED_RED, OUTPUT);
    pinMode(PIN_BTN_BOOT, INPUT_PULLUP);
    pinMode(PIN_BTN_KEY, INPUT_PULLUP);
    led(PIN_LED_GREEN, true);

    Serial.begin(115200);
    delay(300);  // let USB CDC enumerate if a host is listening; harmless otherwise
    rtcBootCount++;

    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    const char *causeStr = "boot";
    bool force = false;
    if (cause == ESP_SLEEP_WAKEUP_TIMER) causeStr = "timer";
    else if (cause == ESP_SLEEP_WAKEUP_EXT1) { causeStr = "button"; force = true; }
    else { force = true; }  // power-on / reset / flash: always draw something
    Serial.printf("\n[boot] %s v%s #%lu wake=%s failures=%lu\n", DEVICE_NAME, FW_VERSION,
                  (unsigned long)rtcBootCount, causeStr, (unsigned long)rtcFailures);

    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 100000);
    pmicInit();

    float tempC = NAN, rh = NAN;
    bool shtOk = shtc3Read(tempC, rh);

    uint8_t *fb = (uint8_t *)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_SPIRAM);
    if (!fb) {
        Serial.println("[mem] PSRAM alloc failed");
        rtcFailures++;
        sleepFor(BACKOFF_MAX_S);
    }

    if (!wifiConnect()) {
        rtcFailures++;
        sleepFor(backoffSeconds());
    }

    // Telemetry first, so the frame rendered next can show this wake's battery level.
    String j = "{";
    j += "\"fw\":\"" FW_VERSION "\",\"wake_cause\":\"" + String(causeStr) + "\"";
    j += ",\"rssi\":" + String(WiFi.RSSI());
    j += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
    j += ",\"failures\":" + String(rtcFailures);
    j += ",\"awake_ms\":" + String(rtcLastAwakeMs);
    if (pmuOk) {
        bool batt = pmu.isBatteryConnect();
        j += ",\"vbus\":" + String(pmu.isVbusIn() ? "true" : "false");
        j += ",\"charging\":" + String(pmu.isCharging() ? "true" : "false");
        if (batt) {
            j += ",\"battery_mv\":" + String(pmu.getBattVoltage());
            j += ",\"battery_pct\":" + String(pmu.getBatteryPercent());
        }
    }
    if (shtOk) j += ",\"temp_c\":" + String(tempC, 1) + ",\"rh\":" + String(rh, 0);
    j += "}";
    postTelemetry(j);

    uint32_t nextWakeS = 0;
    FetchResult r = fetchFrame(fb, force, nextWakeS);
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);  // radio off for the 25 s refresh

    if (r == FETCH_FAILED) {
        rtcFailures++;
        sleepFor(backoffSeconds());
    }
    rtcFailures = 0;

    if (r == FETCH_NEW) {
        led(PIN_LED_RED, true);
        if (EPD_ROTATE_180) rotate180(fb);
        if (!epdShow(fb)) {
            rtcEtag[0] = 0;  // make sure the next wake redraws
            rtcFailures++;
        }
        led(PIN_LED_RED, false);
    }
    heap_caps_free(fb);
    sleepFor(nextWakeS ? nextWakeS : 7200);
}

void loop() {}  // never reached
