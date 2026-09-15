"""Central configuration. Env vars override defaults.

Env vars (all optional):
    COPILOT_HOST, COPILOT_PORT, COPILOT_RATE_HZ,
    COPILOT_HB_INTERVAL_S, COPILOT_HB_TIMEOUT_S,
    COPILOT_MAX_AGE_S, COPILOT_LOG_LEVEL, COPILOT_LOG_FILE
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _getenv_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name]) if name in os.environ else default
    except ValueError:
        return default


def _getenv_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name]) if name in os.environ else default
    except ValueError:
        return default


@dataclass
class LinkConfig:
    server_host: str = "0.0.0.0"
    server_port: int = 5555
    command_rate_hz: float = 50.0
    heartbeat_interval_s: float = 0.1  # 100 ms
    heartbeat_timeout_s: float = 0.5  # 500 ms
    max_packet_age_s: float = 1.0
    status_interval_s: float = 1.0
    log_level: str = "INFO"
    log_file: str | None = None

    @classmethod
    def from_env(cls) -> "LinkConfig":
        return cls(
            server_host=os.environ.get("COPILOT_HOST", "0.0.0.0"),
            server_port=_getenv_int("COPILOT_PORT", 5555),
            command_rate_hz=_getenv_float("COPILOT_RATE_HZ", 50.0),
            heartbeat_interval_s=_getenv_float("COPILOT_HB_INTERVAL_S", 0.1),
            heartbeat_timeout_s=_getenv_float("COPILOT_HB_TIMEOUT_S", 0.5),
            max_packet_age_s=_getenv_float("COPILOT_MAX_AGE_S", 1.0),
            log_level=os.environ.get("COPILOT_LOG_LEVEL", "INFO"),
            log_file=os.environ.get("COPILOT_LOG_FILE") or None,
        )
