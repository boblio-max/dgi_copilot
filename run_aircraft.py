"""Aircraft entry point: python run_aircraft.py [--host ...] [--port ...]."""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, ".")

from copilot_link.aircraft.server import CopilotServer
from copilot_link.common.config import LinkConfig


def setup_logging(level: str, log_file: str | None) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        filename=log_file or None,
        force=True,
    )


def main() -> None:
    cfg = LinkConfig.from_env()
    ap = argparse.ArgumentParser(description="Copilot aircraft server (Pi Zero 2 W). Simulated data only.")
    ap.add_argument("--host", default=cfg.server_host)
    ap.add_argument("--port", type=int, default=cfg.server_port)
    ap.add_argument("--hb-timeout", type=float, default=cfg.heartbeat_timeout_s,
                    help="heartbeat timeout seconds (default 0.5)")
    ap.add_argument("--max-age", type=float, default=cfg.max_packet_age_s)
    ap.add_argument("--verbose", action="store_true", help="debug logging")
    ap.add_argument("--log-file", default=cfg.log_file)
    args = ap.parse_args()

    cfg.server_host = args.host
    cfg.server_port = args.port
    cfg.heartbeat_timeout_s = args.hb_timeout
    cfg.max_packet_age_s = args.max_age
    cfg.log_level = "DEBUG" if args.verbose else cfg.log_level
    cfg.log_file = args.log_file

    setup_logging(cfg.log_level, cfg.log_file)
    server = CopilotServer(cfg)
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n[aircraft] shutting down", flush=True)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
