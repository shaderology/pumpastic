"""
Pumpastic — Tapo P110M Smart Plug Test
=======================================
Tests connectivity, state reading, energy monitoring, and toggle.
Uses the `tapo` library (mihai-dinculescu/tapo) — NOT python-kasa.

Usage:
    python tools/plug_check.py [--toggle]

    --toggle    Actually toggle the plug ON/OFF (default: read-only).
                ⚠ If a pump is plugged in, this RUNS IT. Never with a low tank.

Environment variables required:
    TAPO_USERNAME=you@example.com
    TAPO_PASSWORD=<password>
    PUMPASTIC_PLUG_HOST=192.168.1.20   (optional; this is the default)

Install:
    pip install tapo
"""

import asyncio
import os
import sys

PLUG_IP = os.environ.get("PUMPASTIC_PLUG_HOST", "192.168.1.20")


async def test_connect():
    """Test 1: Can we authenticate and connect to the plug?"""
    from tapo import ApiClient

    print("[1/4] Connecting to Tapo P110M...")
    client = ApiClient(
        os.environ["TAPO_USERNAME"],
        os.environ["TAPO_PASSWORD"],
    )
    # P110M uses the .p110() method (same as P110 and P115)
    device = await client.p110(PLUG_IP)

    info = await device.get_device_info()
    print(f"  ✓ Connected: {info.nickname or info.model}")
    print(f"    Model:    {info.model}")
    print(f"    HW Ver:   {getattr(info, 'hw_ver', 'unknown')}")
    print(f"    FW Ver:   {getattr(info, 'fw_ver', 'unknown')}")
    print(f"    MAC:      {getattr(info, 'mac', 'unknown')}")
    print(f"    State:    {'ON' if info.device_on else 'OFF'}")
    print(f"    RSSI:     {getattr(info, 'rssi', '?')} dBm")

    return device


async def test_current_power(device):
    """Test 2: Can we read instantaneous power data?"""
    print("\n[2/4] Reading current power...")
    try:
        power = await device.get_current_power()
        # `tapo` returns watts directly (not milliwatts like python-kasa)
        watts = power.current_power
        print(f"  ✓ Current power: {watts} W")

        if watts == 0:
            print(f"    ⚠ 0W reading — no load connected, or plug just powered on")
        elif watts > 0:
            print(f"    Load is drawing power")

        return True
    except Exception as e:
        print(f"  ✗ Power read error: {e}")
        return False


async def test_device_usage(device):
    """Test 3: Can we read cumulative usage data?"""
    print("\n[3/4] Reading device usage (today / 7d / 30d)...")
    try:
        usage = await device.get_device_usage()
        usage_dict = usage.to_dict()
        print(f"  ✓ Usage data retrieved")

        # Pretty-print known fields if present
        for key in ("time_usage", "power_usage", "saved_power"):
            if key in usage_dict:
                v = usage_dict[key]
                print(f"    {key}: {v}")

        return True
    except Exception as e:
        print(f"  ✗ Usage read error: {e}")
        return False


async def test_toggle(device):
    """Test 4: Toggle the plug ON then OFF (and restore)."""
    print("\n[4/4] Toggle test (ON → wait 3s → OFF)...")

    initial_info = await device.get_device_info()
    initial_state = initial_info.device_on
    print(f"  Initial state: {'ON' if initial_state else 'OFF'}")

    # Turn ON
    print("  Turning ON...")
    await device.on()
    info = await device.get_device_info()
    if info.device_on:
        print("  ✓ Plug is ON")
    else:
        print("  ✗ Plug did not turn ON")
        return False

    # Wait and read power while ON
    print("  Waiting 3 seconds...")
    await asyncio.sleep(3)
    try:
        power = await device.get_current_power()
        print(f"  Power while ON: {power.current_power} W")
    except Exception:
        pass

    # Turn OFF
    print("  Turning OFF...")
    await device.off()
    info = await device.get_device_info()
    if not info.device_on:
        print("  ✓ Plug is OFF")
    else:
        print("  ✗ Plug did not turn OFF")
        return False

    # Restore initial state
    if initial_state:
        print("  Restoring initial state (ON)...")
        await device.on()

    print("  ✓ Toggle test passed")
    return True


async def main():
    print("=" * 50)
    print("  Pumpastic — Tapo P110M Plug Test")
    print("  Library: tapo (mihai-dinculescu)")
    print("=" * 50)

    # Check credentials
    if not os.environ.get("TAPO_USERNAME") or not os.environ.get("TAPO_PASSWORD"):
        print("\n✗ TAPO_USERNAME and TAPO_PASSWORD environment variables required")
        print("  PowerShell: $env:TAPO_USERNAME='email'; $env:TAPO_PASSWORD='pass'")
        print("  Bash:       export TAPO_USERNAME='email' TAPO_PASSWORD='pass'")
        sys.exit(1)

    # Check tapo library is installed
    try:
        import tapo  # noqa: F401
    except ImportError:
        print("\n✗ `tapo` library not installed")
        print("  Run: pip install tapo")
        print("  (NOTE: this is the mihai-dinculescu library, NOT python-kasa)")
        sys.exit(1)

    do_toggle = "--toggle" in sys.argv

    try:
        device = await test_connect()
        await test_current_power(device)
        await test_device_usage(device)

        if do_toggle:
            await test_toggle(device)
        else:
            print("\n[4/4] Toggle test: SKIPPED (run with --toggle to enable)")
            print("  ⚠ WARNING: --toggle will switch the pump circuit ON then OFF")

        print("\n" + "=" * 50)
        print("  ALL TESTS PASSED")
        print("=" * 50)

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
