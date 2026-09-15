"""Message builders for the copilot link protocol.

All builders return plain dicts ready for protocol.encode_packet().
Simulated data only -- nothing here touches RC hardware or a flight
controller.
"""

from __future__ import annotations

import math

from .protocol import (
    TYPE_ACK,
    TYPE_COMMAND,
    TYPE_HEARTBEAT,
    TYPE_STATUS,
    PROTOCOL_VERSION,
    utc_now,
)


def build_command(
    sequence: int,
    pilot: dict,
    copilot: dict,
    mode: str,
    timestamp: float | None = None,
) -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_COMMAND,
        "sequence": sequence,
        "timestamp": timestamp if timestamp is not None else utc_now(),
        "payload": {"pilot": dict(pilot), "copilot": dict(copilot), "mode": mode},
    }


def build_heartbeat(sequence: int, timestamp: float | None = None) -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_HEARTBEAT,
        "sequence": sequence,
        "timestamp": timestamp if timestamp is not None else utc_now(),
        "payload": {},
    }


def build_ack(
    ack_seq: int,
    ok: bool,
    reason: str = "ok",
    sequence: int = 0,
    timestamp: float | None = None,
) -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_ACK,
        "sequence": sequence,
        "timestamp": timestamp if timestamp is not None else utc_now(),
        "payload": {
            "ack_seq": ack_seq,
            "result": "ok" if ok else "rejected",
            "reason": reason,
            "server_time": utc_now(),
        },
    }


def build_status(stats: dict, sequence: int = 0, timestamp: float | None = None) -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_STATUS,
        "sequence": sequence,
        "timestamp": timestamp if timestamp is not None else utc_now(),
        "payload": dict(stats),
    }


def simulated_command(sequence: int, t_s: float) -> dict:
    """Generate deterministic simulated stick data for tests/demos.

    Gentle sine wobble around 1500 so latency/plot output looks alive.
    Pure function of (sequence, t_s); no hardware involved.
    """
    pitch = 1500 + int(150 * math.sin(t_s * 1.7))
    yaw_corr = int(40 * math.sin(t_s * 0.9))
    pilot = {"roll": 1500, "pitch": pitch, "yaw": 1500, "throttle": 1400}
    copilot = {
        "roll_correction": 0,
        "pitch_correction": 0,
        "yaw_correction": yaw_corr,
        "throttle_correction": 0,
    }
    return build_command(sequence, pilot, copilot, "HEADING_HOLD")
