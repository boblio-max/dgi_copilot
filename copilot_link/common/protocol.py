"""Shared protocol constants and packet encode/decode/validation.

Transport framing (beta): JSON + newline ("JSON lines") over TCP.
Each TCP segment may contain zero or more newline-terminated JSON objects.
This keeps the transport replaceable: only the socket send/recv loop
depends on TCP; everything else works on dict packets.
"""

from __future__ import annotations

import json
import time

PROTOCOL_VERSION = 1

TYPE_COMMAND = "command"
TYPE_HEARTBEAT = "heartbeat"
TYPE_ACK = "ack"
TYPE_STATUS = "status"

VALID_TYPES = frozenset({TYPE_COMMAND, TYPE_HEARTBEAT, TYPE_ACK, TYPE_STATUS})

# Channel ranges for simulated RC data (microseconds-style, Betaflight-like).
CHANNEL_MIN = 1000
CHANNEL_MAX = 2000
CORRECTION_MIN = -500
CORRECTION_MAX = 500

VALID_MODES = frozenset({"MANUAL", "HEADING_HOLD", "ALT_HOLD", "COPILOT_ASSIST"})

# Freshness envelope: reject packets older than this (seconds) or
# stamped too far in the future (clock skew guard).
DEFAULT_MAX_AGE_S = 1.0
FUTURE_SKEW_S = 0.5


def utc_now() -> float:
    """Return current time as Unix epoch seconds (float)."""
    return time.time()


def encode_packet(packet: dict) -> bytes:
    """Serialize a packet dict to newline-terminated JSON bytes."""
    return (json.dumps(packet, separators=(",", ":")) + "\n").encode("utf-8")


def decode_packet(raw: bytes | str) -> dict:
    """Parse one JSON packet. Raises ValueError on malformed input."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"packet is not valid utf-8: {exc}") from exc
    raw = raw.strip()
    if not raw:
        raise ValueError("empty packet")
    try:
        packet = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"packet is not valid JSON: {exc}") from exc
    if not isinstance(packet, dict):
        raise ValueError("packet must be a JSON object")
    return packet


def validate_packet(
    packet: dict,
    *,
    now: float | None = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> tuple[bool, str]:
    """Validate envelope + type-specific payload.

    Returns (True, "ok") or (False, reason). Never raises on bad data
    (raises TypeError only if `packet` itself is not a dict, which is a
    programmer error, not wire data).
    """
    if not isinstance(packet, dict):
        raise TypeError("packet must be a dict")
    if now is None:
        now = utc_now()

    # --- envelope ---
    if packet.get("version") != PROTOCOL_VERSION:
        return False, f"unsupported version: {packet.get('version')!r}"
    msg_type = packet.get("type")
    if msg_type not in VALID_TYPES:
        return False, f"unknown type: {msg_type!r}"
    seq = packet.get("sequence")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return False, f"bad sequence: {seq!r}"
    ts = packet.get("timestamp")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False, f"bad timestamp: {ts!r}"
    age = now - float(ts)
    if age > max_age_s:
        return False, f"stale packet (age={age:.3f}s > {max_age_s}s)"
    if age < -FUTURE_SKEW_S:
        return False, f"timestamp too far in future (age={age:.3f}s)"
    payload = packet.get("payload")
    if not isinstance(payload, dict):
        return False, "payload must be an object"

    if msg_type == TYPE_COMMAND:
        return _validate_command_payload(payload)
    if msg_type == TYPE_HEARTBEAT:
        return True, "ok"
    if msg_type == TYPE_ACK:
        return _validate_ack_payload(payload)
    if msg_type == TYPE_STATUS:
        return True, "ok"  # status is informational; envelope already checked
    return False, f"unknown type: {msg_type!r}"  # unreachable


def _check_channel(name: str, value: object) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return f"{name} must be int in [{CHANNEL_MIN},{CHANNEL_MAX}], got {value!r}"
    if not (CHANNEL_MIN <= value <= CHANNEL_MAX):
        return f"{name} out of range [{CHANNEL_MIN},{CHANNEL_MAX}]: {value}"
    return None


def _check_correction(name: str, value: object) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return f"{name} must be int in [{CORRECTION_MIN},{CORRECTION_MAX}], got {value!r}"
    if not (CORRECTION_MIN <= value <= CORRECTION_MAX):
        return f"{name} out of range [{CORRECTION_MIN},{CORRECTION_MAX}]: {value}"
    return None


def _validate_command_payload(payload: dict) -> tuple[bool, str]:
    pilot = payload.get("pilot")
    copilot = payload.get("copilot")
    mode = payload.get("mode")
    if not isinstance(pilot, dict):
        return False, "payload.pilot must be an object"
    if not isinstance(copilot, dict):
        return False, "payload.copilot must be an object"
    for axis in ("roll", "pitch", "yaw", "throttle"):
        err = _check_channel(f"pilot.{axis}", pilot.get(axis))
        if err:
            return False, err
    for axis in ("roll_correction", "pitch_correction", "yaw_correction", "throttle_correction"):
        err = _check_correction(f"copilot.{axis}", copilot.get(axis))
        if err:
            return False, err
    if mode not in VALID_MODES:
        return False, f"bad mode: {mode!r} (expected one of {sorted(VALID_MODES)})"
    return True, "ok"


def _validate_ack_payload(payload: dict) -> tuple[bool, str]:
    ack_seq = payload.get("ack_seq")
    if not isinstance(ack_seq, int) or isinstance(ack_seq, bool) or ack_seq < 0:
        return False, f"bad ack_seq: {ack_seq!r}"
    if payload.get("result") not in ("ok", "rejected"):
        return False, f"bad ack result: {payload.get('result')!r}"
    return True, "ok"
