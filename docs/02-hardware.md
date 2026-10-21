# 02 — Hardware

## Bill of materials

| Item | Reference choice | Notes |
|---|---|---|
| Sensor MCU ×2 | ESP32-S3 dev board (N16R8), USB-C | Any ESP32-S3 works. A board with an **external antenna** connector is worth it if the tank is far from the access point (see `docs/08`) |
| Screw-terminal base ×2 | Matching ESP32-S3 terminal breakout | Makes outdoor wiring reliable; check which GPIOs it exposes |
| Ultrasonic sensor ×2 | JSN-SR04T **V3.x** (waterproof probe on a cable) | V3.x runs at 3.3 V. Dead zone ~20–25 cm: measure yours |
| Controller | Raspberry Pi 3B or newer, 64-bit OS | Use a proper **5.1 V / 2.5 A+** supply and a short, thick cable. Under-voltage causes USB drops |
| Pump switch | TP-Link Tapo P110M (16 A, energy monitoring, local API) | Any switch with a local API and power reading. Plug relays wear with motor loads; see below |
| Pump | Surface transfer pump sized for your head | Reference: 600 W, 3,600 L/h at 0 m, 40 m max head → 1,540 L/h at ~23 m total head |
| Float switch | Normally-closed, low-level cut-off | Wired **in series with the pump supply** in Tank A, independent of all software |
| Enclosure | Weatherproof box near Tank A | The reference Pi and Tank A node share one box, joined by a 10 cm USB lead |
| Optional | Waveshare ESP32-S3-PhotoPainter 7.3" e-paper; USB webcam for debugging | |

## Sensor wiring (JSN-SR04T V3.x)

| Sensor | ESP32-S3 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| TRIG | GPIO 5 |
| ECHO | GPIO 6 (direct; V3.x is 3.3 V-safe) |

- Avoid strapping pins GPIO 0, 3, 45, 46.
- The onboard WS2812 status LED is on GPIO 48 on most S3 dev boards.
- Older JSN-SR04T revisions (V2) need 5 V and a divider on ECHO. Check yours.

## Mounting the sensor

This is where most of the trouble comes from (`docs/08`).

- **Clear the dead zone.** The sensor can't see water closer than its dead zone (~23 cm
  on the reference unit). Mount it so the *highest* water you care about (the overflow,
  plus storm overfill) is still clearly beyond that.
- **Keep the beam clear.** The cone widens with depth. Ribs, pipes, inlet hoses and the
  tank wall all return echoes that look like water. The reference sensor lost the real
  surface below ~66% and latched onto fixed echoes.
- **Use a stilling tube** (90–100 mm PVC from just under the sensor to near the floor, with
  holes near the bottom). The beam then only sees water inside the tube, which is also
  calmer. This is the standard fix and the recommended starting point.
- **Mount it level**, pointing straight down, away from the inlet.

## The pump circuit

- A plug-in smart plug is fine for prototyping a ~600 W pump. Motor inrush is several
  times running current and plug relays wear faster with inductive loads. For a permanent
  install, use a DIN-rail relay with power metering (e.g. Shelly Pro 1PM) driving an
  AC-3-rated contactor.
- **Fixed mains wiring must be done by a licensed electrician.**
- Fit the **float switch** in series with the pump supply. It's the only protection that
  still works when the sensor, the Pi and the network all fail together.
- Smart plugs typically come back **OFF** after a power cut. Keep it that way.

## WiFi or USB for the sensor nodes

Tank sites are often at the edge of WiFi coverage. The reference Tank A node, with a PCB
antenna at −78 dBm, dropped off every few minutes and could never finish an OTA update.
Options, best first:

1. **USB to the Pi**, if the Pi can sit next to the tank. The firmware's
   `SERIAL_BRIDGE_MODE` turns the radio off and prints readings; `serial_bridge.py`
   republishes them. Robust, and the node is powered by the Pi.
2. **A board with an external antenna**, or an access point near the tanks.
3. A PCB-antenna board with transmit power tuned down; see `docs/08`.

## Optional: e-ink display

The Waveshare ESP32-S3-PhotoPainter (7.3", 800×480, 6 colours) runs on its own battery
because it sleeps between refreshes (it reports its charge to the dashboard). It has no OTA, and its USB port disappears while
it sleeps: press its KEY button to wake it before flashing. **Back up the factory flash
before the first upload**; the backup may contain WiFi credentials, so keep it out of git.
