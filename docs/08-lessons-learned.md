# 08 — Lessons learned

Real incidents from the reference installation, and what each one changed. Read this
before you skip a "paranoid" rule.

## 1. A test reading moved 164 litres

**What happened.** An integration test published a synthetic "Tank A at 95%" reading to
the production broker. The controller did exactly what it should and started the pump,
which was already plumbed in. 164 L moved in 73 s before someone pulled the plug; with a
5-minute minimum run it would have been far more. With a low Tank A it would have been a
dry run.

**Changed.** Tests that touch the broker or the plug were separated from the safe suite and
labelled as hazards; agents are told never to run them casually.

**Lesson.** In a system that acts on messages, *test data is a command*.

## 2. One dead-zone echo started the pump

**What happened.** Mid-evening, a single reading of 22.7 cm, the sensor's dead-zone floor,
arrived between normal readings of ~100 cm. The validation floor was the sensor's spec
(21 cm), so it passed, converted to 100%, satisfied the start threshold, and ran the pump
for 26 minutes on a tank at ~68%.

**Changed.** The minimum distance moved clearly above the measured dead zone, and a start
now needs 3 *distinct* readings that agree. A test replays the exact sequence.

**Lesson.** A sensor's "no echo" often looks like a valid extreme value. Validate against
physics (where the water can be), not the datasheet, and never act on one reading.

## 3. The WiFi drops that weren't the router

**What happened.** The Tank A node kept dropping off WiFi. Restarting the access point
seemed to help, so the router got the blame. It wasn't the router.

**Root causes, all in firmware:** modem sleep (default on), reconnecting on top of a
half-open session (the AP then rate-limits the client), trusting `WiFi.status()` instead of
proving traffic, and **transmit power too high for a cheap board**: the 3.3 V rail sagged
during bursts, so the board heard the AP perfectly but the AP couldn't decode its frames
(auth requests arrived, association never completed, even at −35 dBm on a desk).

**Changed.** Modem sleep off; full radio teardown before every reconnect; a dead-link
watchdog based on MQTT keepalive; transmit power tuned; reason codes and an AP scan printed
on failure. In the end the node moved to **USB serial** because its PCB antenna was simply
too weak at the tank (−78 dBm, 72% retries).

**Lesson.** If a fix "works" only after restarting the other side, the other side was
cleaning up after you.

## 4. The sensor goes blind below two-thirds

**What happened.** During the first full-cycle test the level fell linearly for 50 minutes,
then stuck at ~107 cm, then at ~115 cm, while the pump kept drawing normal power. The beam
had lost the water and was returning fixed echoes off the tank wall or fittings. The 60%
stop threshold never fired; the run ended at the 90-minute cap as a FAULT, with the tank
really at ~46%.

**Changed.** Documented as a known limitation; the runtime cap was confirmed to hold Tank A
well above its floor in that case; a stilling tube and a drawdown check are planned.

**Lesson.** The runtime cap isn't a formality; it's what saves you when the sensor lies.
Test the *whole* range before trusting a stop threshold.

## 5. A storm made the sensor go silent when it mattered most

**What happened.** A full day of rain. The pump ran its 90-minute cap, rested, and started
again, but inflow exceeded the pump's 1,540 L/h, and the water rose *past the overflow
outlet* to ~40 cm from the sensor. The firmware's minimum distance was 45 cm, set on the
theory that water can't get above the outlet. Every reading was rejected, the node went
silent, and after 120 s the controller faulted and **turned the pump off at the moment the
tank most needed emptying**. The raw serial output showed steady, real echoes at ~42 cm.

**Changed.** Minimum distance lowered to 30 cm (still clear of the dead zone); readings
above the outlet publish as 100% with `"overfull": true`. After the firmware update the
fault cleared, the pump restarted, and the level fell ~1 cm/min, matching the measured
flow, so it was real water.

**Lesson.** "Physically impossible" limits need a margin for weather. When a node goes
silent, read its raw output before assuming it's dead.

## 6. Smaller ones

- **Flow rate from in-run readings was 5× too high.** Two noisy mid-transfer readings gave
  135 L/min; settled before/after medians gave 25.7 L/min. Only measure with the pump off.
- **Split USB packets dropped readings.** The prefix and payload were written separately,
  and a reader with a timeout saw half a line. Emit each record in one write; reassemble lines.
- **Under-voltage on the Pi** caused USB disconnects of the bridged node. Use a proper supply.
- **A WiFi-to-WiFi relay stall in the router** cut the Pi off from the plug, the solar dongle
  and the display at once, while wired clients still reached them. A router restart fixed
  it; a network cable to the Pi removes the dependency.
- **The time zone matters.** A zone with daylight saving shifted schedules by an hour. Set
  the Pi's zone deliberately.
