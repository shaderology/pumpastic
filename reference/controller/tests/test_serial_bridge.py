"""Tests for the USB serial-to-MQTT bridge line parser.

Parsing only: no serial port, no broker, no plug. Safe to run anywhere. (Never add
tests that publish to the live broker: a published level can start the pump.)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from serial_bridge import BRIDGE_PREFIX, parse_reading  # noqa: E402


def line(payload: dict) -> str:
    return BRIDGE_PREFIX + json.dumps(payload)


VALID = {
    "sensor_id": "tank_a",
    "distance_cm": 111.1,
    "level_pct": 64.1,
    "volume_l": 3088,
    "link": "usb",
    "uptime_s": 58,
}


def test_parses_a_well_formed_reading():
    assert parse_reading(line(VALID)) == VALID


def test_ignores_ordinary_log_chatter():
    for noise in (
        "[SENSOR] Distance: 111.1 cm | Level: 64.1% | Volume: 3088 L",
        "[BRIDGE] Serial bridge mode — WiFi disabled",
        "ESP-ROM:esp32s3-20210327",
        "",
    ):
        assert parse_reading(noise) is None


def test_tolerates_boot_noise_prefixed_to_the_marker():
    # The ESP32 prints ROM chatter that can land on the same line as the first reading.
    assert parse_reading("rst:0x15 (USB_UART_CHIP_RESET)" + line(VALID)) == VALID


def test_rejects_malformed_json_without_raising():
    assert parse_reading(BRIDGE_PREFIX + '{"sensor_id": "tank_a", ') is None


def test_rejects_json_that_is_not_an_object():
    assert parse_reading(BRIDGE_PREFIX + "[1, 2, 3]") is None


@pytest.mark.parametrize("missing", ["level_pct", "sensor_id"])
def test_rejects_readings_missing_required_fields(missing):
    payload = dict(VALID)
    del payload[missing]
    assert parse_reading(line(payload)) is None


def test_keeps_a_zero_level_reading():
    # 0% is a legitimate, safety-relevant value — it must not be dropped as falsy.
    payload = dict(VALID, level_pct=0.0)
    assert parse_reading(line(payload)) == payload


def test_handles_trailing_whitespace_and_carriage_return():
    assert parse_reading(line(VALID) + "\r\n  ") == VALID


# ── Line assembly ─────────────────────────────────────────────────────────────
# USB delivers arbitrary chunks. A record split across two reads used to come back
# truncated and the reading was silently dropped (observed 2026-09-18).

from serial_bridge import LineAssembler  # noqa: E402


def test_assembles_a_record_split_across_chunks():
    asm = LineAssembler()
    payload = line(VALID)
    half = len(payload) // 2
    assert asm.feed(payload[:half].encode()) == []
    assert asm.feed((payload[half:] + "\n").encode()) == [payload]


def test_returns_multiple_records_arriving_in_one_chunk():
    asm = LineAssembler()
    blob = (line(VALID) + "\n" + line(VALID) + "\n").encode()
    assert asm.feed(blob) == [line(VALID), line(VALID)]


def test_split_record_still_parses_into_a_reading():
    asm = LineAssembler()
    payload = line(VALID)
    asm.feed(payload[:40].encode())
    lines = asm.feed((payload[40:] + "\n").encode())
    assert [parse_reading(x) for x in lines] == [VALID]


def test_empty_chunk_is_harmless():
    assert LineAssembler().feed(b"") == []


def test_drops_runaway_unterminated_data():
    asm = LineAssembler()
    assert asm.feed(b"x" * (LineAssembler.MAX_BUFFER + 10)) == []
    # Buffer was cleared, so a following complete record still parses.
    assert asm.feed((line(VALID) + "\n").encode()) == [line(VALID)]
