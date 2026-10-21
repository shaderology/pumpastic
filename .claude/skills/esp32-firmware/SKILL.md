---
name: esp32-firmware
description: Use when developing, building, flashing, or debugging the ESP32-S3 tank sensor firmware (PlatformIO, Arduino, JSN-SR04T ultrasonic sensor, MQTT publishing, USB serial bridge mode, WiFi, GPIO, median filtering) or the e-ink display firmware. Also for serial-output, flash-failure and sensor-wiring troubleshooting.
---

# ESP32-S3 firmware

Sources: `reference/firmware/tank-sensor/` (`include/config.h`, `src/main.cpp`) and
`reference/firmware/eink-display/`. Site values (ports, addresses, dimensions) come from
`CLAUDE.local.md`.

## Hardware rules

1. **20 µs trigger pulse** for the JSN-SR04T (the HC-SR04's 10 µs is too short).
2. **Avoid strapping pins** GPIO 0, 3, 45, 46. Reference pins: TRIG 5, ECHO 6, WS2812 LED 48.
3. **Median of 5** readings 50 ms apart; at least 3 must be valid.
4. **`SENSOR_MIN_CM` sits clearly above the measured dead zone** (a no-echo reads as the
   dead-zone floor, which converts to 100%) **and far enough below the overflow distance
   to read storm overfill.** See `docs/07-calibration.md` and incidents 2 and 5 in
   `docs/08-lessons-learned.md` before changing it.
5. V3.x sensors run at 3.3 V; older V2 boards need 5 V and an ECHO divider.

## Secrets

`include/secrets.h` (gitignored) holds `WIFI_SSID`, `WIFI_PASS`, `OTA_PASSWORD`. Copy
`include/secrets.h.example` first. The OTA env reads `PUMPASTIC_OTA_PASSWORD`.

## Two transport modes

- **WiFi** (`SERIAL_BRIDGE_MODE` undefined): static IP, MQTT with LWT, ArduinoOTA, modem
  sleep off, capped transmit power (`WIFI_TX_POWER`), full radio teardown before each
  reconnect, dead-link watchdog (`LINK_STALE_MS`). Incident 3 in `docs/08` explains each.
- **USB** (`SERIAL_BRIDGE_MODE` defined): radio never starts; each reading prints as
  `#PUMPASTIC {json}` in a single write; `serial_bridge.py` on the Pi republishes it.
  No OTA. Framing details: `docs/04-protocols.md`.

## Level maths

```cpp
float water_depth = SENSOR_HEIGHT_CM - distance_cm;               // 100% = overflow outlet
float pct = constrain(water_depth / OVERFLOW_HEIGHT_CM * 100.0, 0.0, 100.0);
float volume_l = pct / 100.0 * TANK_CAPACITY_L;
// distance below the overflow distance → add "overfull": true
```

## LED (WS2812, GPIO 48)

Purple fast blink = booting · blue 1 Hz = idle · green 1 s = reading published ·
amber fast = no valid echoes · red solid = WiFi/MQTT down · cyan fast = OTA.

## Build and flash

```bash
cd reference/firmware/tank-sensor
pio run -e esp32s3                       # build
pio run -e esp32s3 -t upload             # USB; set upload_port in platformio.ini
pio device monitor -b 115200
PUMPASTIC_OTA_PASSWORD=... pio run -e esp32s3-ota -t upload    # WiFi nodes only
```

On Windows the PlatformIO CLI may not be on PATH: `~/.platformio/penv/Scripts/pio`.

**A node on the Pi's USB** (no radio, so no OTA): build on the workstation, then

```bash
scp .pio/build/esp32s3/firmware.bin pi@<pi>:/tmp/node.bin
# on the Pi
sudo systemctl stop pumpastic-tank-a-bridge                 # frees the port
P=$(ls /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*-if00)
~/esptool-venv/bin/esptool --chip esp32s3 --port "$P" --before default-reset \
    --after hard-reset write-flash 0x10000 /tmp/node.bin
sudo systemctl start pumpastic-tank-a-bridge
```

(`python3 -m venv ~/esptool-venv && ~/esptool-venv/bin/pip install esptool` once.)

## Reading raw output when a node "goes silent"

Stop the bridge, then `stty -F "$P" 115200 raw -echo; timeout 40 cat "$P"`. A line like
`Not enough valid readings (0/5). Raw echo us: 2416 2462 ...` means echoes outside the
valid range: divide µs by 58 for cm. All zeros means no echo at all (wiring, condensation).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `secrets.h: No such file` | fresh clone | copy `secrets.h.example` |
| no serial output | USB CDC flag missing | keep `-DARDUINO_USB_CDC_ON_BOOT=1` |
| flash timeout | not in boot mode | hold BOOT, tap RST, release BOOT |
| readings stuck at fixed values mid-tank | beam hitting a wall/fitting | stilling tube (`docs/02`) |
| WiFi node drops every few minutes | weak antenna / TX power / modem sleep | `docs/08` incident 3; or move to USB |
| e-ink board won't flash | it's asleep (port gone) | press KEY, flash within ~40 s |
