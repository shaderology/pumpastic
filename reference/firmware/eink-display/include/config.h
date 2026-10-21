// Pumpastic e-ink status display — configuration
#pragma once

#define FW_VERSION      "1.0.0"
#define DEVICE_NAME     "pumpastic-eink"

// ---- WiFi ---- (DHCP). WIFI_SSID / WIFI_PASS live in secrets.h (gitignored);
// copy secrets.h.example to secrets.h and fill it in.
#include "secrets.h"
#define WIFI_TIMEOUT_MS 15000

// ---- Server (the Pi's Flask dashboard) ----
#define SERVER_BASE     "http://192.168.1.10:8080"   // EXAMPLE: your Pi
#define FRAME_PATH      "/display/frame.bin"
#define TELEMETRY_PATH  "/display/telemetry"
#define HTTP_TIMEOUT_MS 20000

// ---- Sleep ----
// The server decides the cadence via X-Next-Wake-S. These bound what we accept and
// cover failures (no server answer) with an increasing backoff.
#define SLEEP_MIN_S         60
#define SLEEP_MAX_S         (14 * 3600)
#define BACKOFF_1_S         300     // 1st consecutive failure: 5 min
#define BACKOFF_2_S         900     // 2nd: 15 min
#define BACKOFF_MAX_S       1800    // 3rd+: 30 min

// ---- Panel orientation ----
// The server sends frames the right way up for the frame standing with its USB cable
// exiting at the bottom; the panel's native scan order matches that (verified on the
// webcam 2026-10-01). The vendor code's 180° flip applies to its BMP decode path, not
// to raw frames. Set to 1 if the frame is ever mounted the other way up.
#define EPD_ROTATE_180      0

// ---- Pins (from the vendor BSP; see the esp32-s3-photopainter skill) ----
#define PIN_I2C_SDA     47
#define PIN_I2C_SCL     48
#define PIN_EPD_DC      8
#define PIN_EPD_CS      9
#define PIN_EPD_SCK     10
#define PIN_EPD_MOSI    11
#define PIN_EPD_RST     12
#define PIN_EPD_BUSY    13      // HIGH = idle
#define PIN_BTN_BOOT    0       // active LOW, EXT1 wake
#define PIN_BTN_KEY     4       // active LOW, EXT1 wake
#define PIN_LED_GREEN   42      // active LOW
#define PIN_LED_RED     45      // active LOW
#define PIN_AUDIO_PA    7       // keep LOW — PA hisses and draws current

#define EPD_W           800
#define EPD_H           480
#define FRAME_BYTES     (EPD_W * EPD_H / 2)
#define EPD_SPI_HZ      20000000
#define EPD_BUSY_TIMEOUT_MS 60000
