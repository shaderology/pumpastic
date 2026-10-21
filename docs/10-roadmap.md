# 10 — Roadmap: known issues and planned improvements

What the reference installation still lacks, and what's planned. Most of these are worth
building in from the start if you're beginning fresh.

## Status at a glance

| Item | Why | Status |
|---|---|---|
| [Float switch](#1-float-switch-hardware-dry-run-cut-off) | a dry-run guard that needs no software | on hand, not fitted |
| [Shelly Pro 1PM instead of the smart plug](#2-shelly-pro-1pm-instead-of-a-smart-plug) | DIN-rail, Ethernet, device-level runtime cap | planned |
| [Network isolation](#3-network-isolation-iot-vlan-or-a-separate-wlan) | the broker and dashboard trust everyone on the LAN | planned |
| [Hydrostatic level sensor](#4-submersible-hydrostatic-level-sensor) | ultrasonic has a dead zone, false echoes and a blind zone | evaluating |
| [Tank B sensor](#5-smaller-items) | Tank B is simulated, so its ceiling protects nothing yet | waiting for hardware |
| [Drawdown check](#5-smaller-items) | catch a sensor that stops tracking while pumping | planned |
| [Enable dry-run and stuck-sensor detection](#5-smaller-items) | implemented, switched off until tuned | ready |
| [Pi on Ethernet, better power supply](#5-smaller-items) | WiFi relay stalls and under-voltage USB drops | planned |

## Known issues today

- **Tank A's ultrasonic sensor goes blind below ~66%.** It locks onto fixed echoes, so
  the 60% stop never fires and every cycle ends at the runtime cap (`docs/08`, incident 4).
  The cap holds Tank A at ~46%, well above the 20% floor, but rule 1 is enforced by the cap,
  not the sensor.
- **Overfill in storms.** Inflow can beat both the pump and the overflow outlet. Readings
  above the outlet are now accepted (`docs/08`, incident 5). Check that your outlet is
  sized for your roof area and kept clear.
- **The USB-bridged node re-enumerates about once an hour.** The bridge reconnects in ~5 s
  and no readings are lost. Cause not yet known; possibly power.
- **Tank B's limits are simulated** until its sensor is installed.

---

## 1. Float switch: hardware dry-run cut-off

**Why.** Every other safety layer runs in software on hardware that can fail together:
sensor, Pi, network, plug. A float switch in Tank A cuts the pump when the water drops,
with no software involved.

**How.**
- Use a "pump-down" (emptying) float: the circuit **opens when the float drops**. Set
  its cut-off at or above the software floor (rule 1, 20%).
- Wire it **in series with the pump supply**. If the float's contact rating is below the
  pump's current (motor inrush included), switch a contactor coil with it instead of the
  motor directly. Mains work: licensed electrician.
- Optionally, also wire a second (low-voltage) contact to an input the controller can read.
  Then "the float tripped while software still thought there was water" becomes an alert,
  which is a sign the level sensor is lying. The Shelly Pro 1PM's switch input can do this
  (item 2).

**Test.** Lift and drop the float by hand with the pump running from the Controls tab.
The pump must stop with no software involvement.

## 2. Shelly Pro 1PM instead of a smart plug

**Why.** A plug-in smart plug was fine for prototyping, but:
- plug relays wear faster switching motor loads;
- it's WiFi-only and depends on a cloud account for its local API handshake;
- the controller is the only thing enforcing the runtime cap.

The Shelly Pro 1PM is a DIN-rail relay with power metering, **Ethernet** as well as WiFi,
a local HTTP/RPC API and native MQTT that need no cloud account, and its own timers.

**How.**
- Have an electrician mount it in a DIN enclosure. For a motor load, let it drive an
  AC-3-rated contactor rather than switching the pump directly.
- Connect it by **Ethernet** (one less WiFi dependency) and give it a DHCP reservation.
- **Device-level runtime cap:** configure the switch's auto-off (`auto_off` /
  `auto_off_delay` in `Switch.SetConfig`) to a little above `max_run_seconds`. If the Pi
  dies mid-run, the relay still turns itself off. Also set the power-on default to OFF.
- **Controller change:** replace `plug_control.py` with a class offering the same async
  methods (`connect`, `turn_on`, `turn_off`, `is_online`, `get_power_watts`,
  `get_device_status`) over the Gen2 RPC API:
  ```
  GET http://<shelly>/rpc/Switch.Set?id=0&on=true
  GET http://<shelly>/rpc/Switch.Set?id=0&on=false
  GET http://<shelly>/rpc/Switch.GetStatus?id=0      → {"output": true, "apower": 612.4, …}
  ```
  The state machine doesn't care what's behind the interface. Protect the device's API
  with its built-in authentication.
- Optionally wire the float switch's auxiliary contact to the Shelly's switch input
  (item 1) and alert on it.

**Test.** Rerun phase 3 of the build plan (lamp first), then confirm the auto-off fires by
stopping the controller mid-run with a lamp attached.

## 3. Network isolation: IoT VLAN or a separate WLAN

**Why.** Anonymous MQTT and an unauthenticated dashboard are acceptable only if everyone on
the network is trusted. Anything that can reach the broker can publish a fake "Tank A 95%"
and start the pump (`docs/08`, incident 1). Separately, the reference site lost the plug,
the solar dongle and the display at once when the router's WiFi-to-WiFi relay stalled.

**Option A: an IoT VLAN** (if the router supports VLANs)
- A dedicated SSID mapped to the VLAN for the sensor nodes, display and plug; the Pi and a
  Shelly on Ethernet ports in the same VLAN.
- Firewall: main LAN → Pi on 8080 (dashboard) and 22 (SSH) only; IoT VLAN → main LAN
  blocked; IoT → internet only for what is needed: NTP, the weather API, ntfy, and the
  plug's cloud handshake if a Tapo plug is still in use (a Shelly needs none).
- Keep the solar dongle wherever the inverter needs it, and allow Pi → dongle :502 across
  the boundary.

**Option B: a separate WLAN hosted by the Pi** (an air gap from the house network)
- The Pi runs an access point (NetworkManager hotspot, or hostapd + dnsmasq) just for the
  nodes, display and plug, and joins the house network over Ethernet only for the dashboard.
- Nothing on the house WiFi can reach the broker at all.
- Trade-offs: a Pi 3's single radio can't be AP and client at once (hence Ethernet for the
  uplink); WiFi range is now set by the Pi's position, and the Pi becomes a single point of
  failure for every node (acceptable: the system fails off).

**Either way:** add MQTT authentication (`password_file`, per-client ACLs: nodes may
publish only their own topics), reach the dashboard remotely only through a VPN, and
update `CLAUDE.local.md` and the firmware `secrets.h` with the new SSID.

## 4. Submersible hydrostatic level sensor

**Why.** Every serious incident with the level reading came from ultrasonic physics: a dead
zone that reads as "full", fixed echoes off walls and fittings, a blind zone in the bottom
third, and condensation. A **submersible pressure transmitter** lying near the tank floor
measures the weight of water above it:

| | Ultrasonic (JSN-SR04T) | Hydrostatic (submersible, 4–20 mA) |
|---|---|---|
| Dead zone | ~23 cm at the top: reads "full" | none at the top; can't see the last few cm above the sensor |
| False echoes, blind zones | yes: walls, ribs, pipes, foam | none |
| Reads the full depth | only with a clear beam / stilling tube | yes, so rule 1 (20% floor) is enforced by the sensor |
| Storm overfill | needs headroom above the dead zone | no issue |
| Wire break or failure | looks like a plausible reading | below 3.6 mA, detectable as a fault (NAMUR NE43) |
| Watch out for | mounting geometry | needs a **vented cable** (atmospheric reference, keep the vent dry), 12–24 V loop supply, surge protection, potable-water rating if relevant, slow drift |

**How.**
- Choose a range close to the tank height (e.g. 0–2.5 m for a 2.2 m tank). Accuracy is a
  % of full scale, so an oversized range wastes resolution.
- Hang it a few cm above the floor, clear of sediment; `level_pct` then needs that offset.
- Read the 4–20 mA loop with a precision shunt into an external ADC (e.g. ADS1115 over I²C)
  or a current-to-voltage module. The ESP32's own ADC is too noisy and non-linear for cm
  resolution.
- **Firmware:** a new reading function: `depth_cm = (mA − 4) / 16 × range_cm + offset_cm`.
  Treat < 3.6 mA or > 21 mA as invalid (no reading, so the sensor timeout fails safe).
  Keep the median filter. The payload is unchanged, and `distance_cm` can be derived for
  compatibility.
- **Calibrate** at two points (low and full) against a tape, as in `docs/07`.

**Interim fix if staying ultrasonic:** a stilling tube (`docs/02`), plus the drawdown
check below.

## 5. Smaller items

- **Tank B sensor.** Mount clear of the dead zone (or use a hydrostatic sensor), measure
  the geometry, remove `simulated_pct`. Until then, Tank B's 90% stop and 95% ceiling
  protect nothing.
- **Drawdown check.** While pumping, FAULT if Tank A hasn't dropped ≥ 2 cm in 5 min
  (expected ~4.5 cm at 1,540 L/h on a 2.8 m² tank). It would have caught the blind-zone run
  at ~56 minutes instead of 89. Make the thresholds config values, with tests.
- **Turn on the detectors that already exist.** `dry_run_watt_threshold` at ~30% of
  measured running watts; `stuck_detection_count` once the noise floor is known (`docs/05`).
- **Pi on Ethernet and a proper power supply.** This removes the WiFi relay dependency and
  the under-voltage USB drops.
- **Smarter starts (as gates only; safety rules stay authoritative):** prefer pumping while
  exporting solar; pump down ahead of forecast heavy rain.
- **E-ink:** a local "offline" badge when the Pi is unreachable; chimes on the speaker.
- **Demo mode:** the Pi falls back to its own access point when the home network is
  absent, so the system can be shown off-site (overlaps with option B above).
