---
name: webcam-debug
description: Use this skill when visually inspecting ESP32 hardware — LED states, wiring, sensor connections, or physical setup. Triggers on any debugging task where seeing the hardware would help, such as verifying LED colour/pattern after a firmware flash, checking wiring before powering on, or confirming sensor orientation. Also use when the user mentions "camera", "webcam", "look at the board", or "check the LED". Always ask the user to point the camera at the relevant hardware before capturing.
---

# Webcam Visual Debugging Skill

## Overview

If the builder has a USB webcam (the reference used a Logitech C920) on the workstation, it can be used for visual inspection of ESP32 hardware. This uses OpenCV to capture frames directly — no browser or MCP server needed.

## Camera Details

- **Model:** HD Pro Webcam C920
- **Device index:** 0
- **Resolution:** 1280x720
- **Output:** JPEG image files in `tools\`

## How to Capture

### Single frame
```python
python -c "import cv2; cap = cv2.VideoCapture(0); cap.set(3,1280); cap.set(4,720); _, f = cap.read(); cv2.imwrite('tools/webcam_capture.jpg', f); cap.release()"
```
Then view with: `Read tools\webcam_capture.jpg`

### Multi-frame burst (for catching blinking LEDs)
```python
python -c "
import cv2, time
cap = cv2.VideoCapture(0)
cap.set(3,1280); cap.set(4,720)
for i in range(10):
    _, f = cap.read()
    cv2.imwrite(f'tools/webcam_{i}.jpg', f)
    time.sleep(0.2)
cap.release()
print('Captured 10 frames')
"
```
Then check several frames: `Read tools\webcam_3.jpg`, etc.

## Important Workflow

1. **Always ask the user first** — "Could you point the webcam at the ESP32 board?"
2. Capture frame(s)
3. Read the image file to view it
4. Describe what you see and diagnose

## LED Identification Guide

The ESP32-S3 board has two LEDs visible on camera:

| LED | Type | Location | What it tells you |
|-----|------|----------|-------------------|
| Small red | Power indicator | On ESP32 module | Board is powered (always on) |
| Large RGB | WS2812 NeoPixel (GPIO 48) | Near ESP32 chip | Firmware status (see colour table in CLAUDE.md) |

### LED colours and meanings
- **Purple fast blink** — Booting, connecting to WiFi/MQTT
- **Blue slow blink (1 Hz)** — Connected, waiting for next reading
- **Green solid (1s)** — Valid sensor reading just published
- **Amber fast blink** — Sensor failing, no valid echoes
- **Red solid** — WiFi or MQTT disconnected

### Camera vs blinking LEDs
Blinking LEDs may appear OFF in any single frame. Solutions:
1. **Take multiple frames** (10 frames over 2s covers most blink patterns)
2. **Use solid colours in firmware** when debugging camera-visible states — modify the LED code to hold a colour steady instead of blinking, so a single frame captures it reliably
3. The 1Hz blue blink has ~500ms ON / ~500ms OFF — a 10-frame burst at 200ms intervals will catch at least 2-3 ON frames

## What to Look For

- **LED colour/state** — Does it match expected firmware behaviour?
- **Wiring** — Are jumper wires connected to correct terminals? Any loose connections?
- **Sensor orientation** — Is the JSN-SR04T transducer facing the right direction?
- **Board seating** — Is the ESP32 properly seated in the screw-terminal board?
- **USB connection** — Is the USB-C cable plugged in securely?
- **Physical damage** — Any bent pins, burnt components, disconnected wires?

## Cleanup

After debugging, remove temp captures:
```bash
del tools\webcam_*.jpg
```
