---
name: smart-plug
description: Use when working with the TP-Link Tapo P110M smart plug that switches the pump — the tapo Python library, ApiClient, on/off, power monitoring, authentication, connectivity — or when choosing between a smart plug and a relay/contactor, or discussing motor inrush and dry-run detection by power.
---

# Smart plug (TP-Link Tapo P110M)

## Library: `tapo`, not `python-kasa`

[`tapo`](https://github.com/mihai-dinculescu/tapo) (Rust core, Python wrapper) talks to the
plug locally after a handshake with the owning TP-Link account. `python-kasa` does not
support the P110M's newer firmware. aarch64 wheels exist, so it installs on 64-bit Pi OS.

**Enable Third-Party Services** in the Tapo app (Me › Third-Party Services) or
authentication fails.

## Credentials

Environment only: `TAPO_USERNAME`, `TAPO_PASSWORD` (`KASA_*` also accepted by the
controller). On the Pi: `/etc/pumpastic.env` via `EnvironmentFile=`. In PowerShell, use
single quotes around a password containing `$`.

## In the reference code

`reference/controller/src/plug_control.py`, class `PlugControl(host, username, password)`:
`connect()`, `turn_on()`, `turn_off()`, `is_online()`, `get_power_watts()`,
`get_device_status()`. On/off make up to 3 attempts, reconnecting between them, and
return False instead of raising; the state machine turns a failure into a FAULT.

Read-only check: `python reference/tools/plug_check.py` (`PUMPASTIC_PLUG_HOST` sets the
address). `--toggle` switches the plug: **with a pump attached that runs the pump.**

## API cheatsheet

```python
import asyncio, os
from tapo import ApiClient

async def main():
    client = ApiClient(os.environ["TAPO_USERNAME"], os.environ["TAPO_PASSWORD"])
    plug = await client.p110(os.environ.get("PUMPASTIC_PLUG_HOST", "192.168.1.20"))
    info = await plug.get_device_info()          # .device_on, .model, .nickname
    power = await plug.get_current_power()       # .current_power in watts
    print(info.device_on, power.current_power)
    # await plug.on() / await plug.off()   ← moves real water if the pump is connected

asyncio.run(main())
```

Energy history: `get_energy_data(EnergyDataInterval.Hourly, start_date=...)`,
`get_power_data(PowerDataInterval.Minutes5, start_date=..., end_date=...)` from `tapo.requests`.

## Safety notes

- **Motor loads:** inrush is several times running current, and plug relays wear faster
  on inductive loads. Fine for a ~600 W pump while prototyping; for permanent use, a
  metering DIN relay (e.g. Shelly Pro 1PM) driving an AC-3 contactor, installed by an electrician.
- **Dry-run detection:** measure running watts first (reference: ~620 W), then set
  `dry_run_watt_threshold` to ~30% of that.
- **After a power cut the plug comes back OFF.** Keep that default.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: tapo` | `pip install tapo` |
| authentication error | the account that set the plug up; Third-Party Services on |
| timeout | wrong IP (give it a DHCP reservation), plug offline, or router WiFi-to-WiFi trouble |
| commands fail after a plug firmware update | `pip install --upgrade tapo` |
| 0 W while ON and pumping | dry run, or wiring/pump fault: stop and look |
