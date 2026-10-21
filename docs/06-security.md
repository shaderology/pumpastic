# 06 — Security

The threat model is modest: a home LAN, hobby hardware, and a repo you might share. The
things that actually go wrong are credentials leaking into git and services that trust
anything on the network.

## Secrets: where each one lives

| Secret | Lives in | Never in |
|---|---|---|
| Smart-plug cloud account (used for the local handshake) | `/etc/pumpastic.env`, root-only (mode 600), read by systemd via `EnvironmentFile=` | the unit file, config.yaml, docs, shell history |
| WiFi SSID/password, OTA password | `firmware/*/include/secrets.h` (gitignored); OTA upload reads `PUMPASTIC_OTA_PASSWORD` from the environment | `config.h`, `platformio.ini` |
| Inverter installer / hotspot passwords | your password manager | anywhere in the repo, including photos of labels |
| ntfy topic | `config.yaml` | the code (no default topic) |
| Site details for your agent | `CLAUDE.local.md` (gitignored) | `CLAUDE.md` |

Each has a tracked `*.example` template. The `.gitignore` covers the real files.

## Before you share or push a repo

1. **Scan the tree** for credentials and identifiers:
   ```bash
   git grep -nIiE "password|passwd|secret|token|api[_-]?key|@gmail|([0-9a-f]{2}:){5}[0-9a-f]{2}" -- . ':!*.example'
   ```
   Then look at every hit. MAC addresses and device serials identify hardware; photos of
   labels often contain passwords.
2. **Scan the history too.** `git log -p -S'<string>'` finds a secret in any commit. If
   one was ever committed, deleting it now isn't enough: publish a fresh history (an orphan
   branch with a single commit, or a new repo) and **rotate the secret**.
3. **Don't commit vendor material**: manuals, product photos and firmware dumps are
   copyrighted, and a full flash dump contains the device's NVS, which may include WiFi
   credentials.
4. **Check for personal details**: home directory paths, usernames, exact coordinates,
   hostnames.

## Network exposure

| Service | Exposure | Recommendation |
|---|---|---|
| Mosquitto :1883 | anonymous, LAN | Fine on a trusted home LAN. Anyone on it can publish a fake level and start the pump, so add `password_file` + ACLs if the LAN isn't trusted (guest devices, shared flats) |
| Dashboard :8080 | no auth, LAN | Has Force OFF, Clear Fault, Simulate and Config. **Never port-forward it.** Use a VPN (WireGuard, Tailscale) for remote access |
| SSH to the Pi | key only | Disable password login; use a dedicated key |
| ESP32 OTA | password | Only on WiFi nodes; USB-bridged nodes have no OTA at all |
| ntfy | public topic | A long random topic name; or self-host ntfy |

Isolate IoT devices on their own VLAN/SSID if your router supports it, and give each
device a DHCP reservation so addresses in config don't drift.

## Agent permissions

If you let Claude Code work on the live system:
- Keep router and network changes out of its reach. Let it query and recommend, not change.
- Keep plug credentials out of its context unless needed. The system check reads them
  from environment variables.
- Ask it to confirm before anything that can switch the pump (see `CLAUDE.md` hazards).
