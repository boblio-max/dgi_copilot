"""Ground entry point: python run_ground.py --host <zero-ip> [options]."""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, ".")

from copilot_link.common.config import LinkConfig
from copilot_link.ground.client import ChaosConfig, CopilotClient


def setup_logging(level: str, log_file: str | None) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        filename=log_file or None,
        force=True,
    )


def main() -> None:
    cfg = LinkConfig.from_env()
    ap = argparse.ArgumentParser(description="Copilot ground client (Pi 4B/5). Simulated data only.")
    ap.add_argument("--host", default="127.0.0.1" if cfg.server_host == "0.0.0.0" else cfg.server_host,
                    help="aircraft IP (Zero 2 W)")
    ap.add_argument("--port", type=int, default=cfg.server_port)
    ap.add_argument("--rate", type=float, default=cfg.command_rate_hz, help="commands per second (default 50)")
    ap.add_argument("--count", type=int, default=0, help="packets to send then exit (0=forever)")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds to run then exit (0=forever)")
    ap.add_argument("--verbose", action="store_true", help="print every packet + STATUS")
    ap.add_argument("--log-file", default=cfg.log_file)
    # stress-test knobs
    ap.add_argument("--stress-test", action="store_true",
                    help="preset chaos: 5%% loss, 2%% duplicates, 2%% reorder, 20ms delay on 5%%")
    ap.add_argument("--loss-rate", type=float, default=0.0)
    ap.add_argument("--delay", type=float, default=0.0, help="extra delay seconds for delayed packets")
    ap.add_argument("--delay-rate", type=float, default=0.0)
    ap.add_argument("--duplicate-rate", type=float, default=0.0)
    ap.add_argument("--reorder-rate", type=float, default=0.0)
    ap.add_argument("--malformed-rate", type=float, default=0.0)
    ap.add_argument("--drop-every", type=float, default=0.0, help="drop connection every N sec (0=off)")
    ap.add_argument("--drop-for", type=float, default=0.0, help="drop duration seconds")
    args = ap.parse_args()

    cfg.server_host = args.host
    cfg.server_port = args.port
    cfg.command_rate_hz = args.rate
    cfg.log_level = "DEBUG" if args.verbose else cfg.log_level
    cfg.log_file = args.log_file

    chaos = ChaosConfig(
        loss_rate=args.loss_rate,
        delay_s=args.delay,
        delay_rate=args.delay_rate,
        duplicate_rate=args.duplicate_rate,
        reorder_rate=args.reorder_rate,
        malformed_rate=args.malformed_rate,
        drop_connection_s=args.drop_every,
        drop_duration_s=args.drop_for,
    )
    if args.stress_test:
        chaos.loss_rate = chaos.loss_rate or 0.05
        chaos.duplicate_rate = chaos.duplicate_rate or 0.02
        chaos.reorder_rate = chaos.reorder_rate or 0.02
        chaos.delay_s = chaos.delay_s or 0.02
        chaos.delay_rate = chaos.delay_rate or 0.05

    setup_logging(cfg.log_level, cfg.log_file)
    client = CopilotClient(cfg, chaos, verbose=args.verbose)
    try:
        client.run(count=args.count, duration_s=args.duration)
    except KeyboardInterrupt:
        print("\n[ground] shutting down", flush=True)
        client.print_summary()
    finally:
        client.stop()


if __name__ == "__main__":
    main()
