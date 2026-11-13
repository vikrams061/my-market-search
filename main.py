"""Entry point for the Groww Real-Time Market Watch application.

Usage:
    python main.py          # terminal UI (rich live table)
    python main.py --web    # web dashboard at http://127.0.0.1:8000

Requires a ``.env`` file with Groww API credentials. See ``.env.example``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from config import Settings, build_watchlist, create_client, setup_logging
from market_watch import MarketWatch


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Groww real-time market watch (terminal or web UI)."
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="serve the web dashboard instead of the terminal UI",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web UI bind host")
    parser.add_argument("--port", type=int, default=8000, help="web UI port")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run with simulated market data (no Groww credentials needed)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.demo:
        os.environ["DEMO_MODE"] = "true"

    if args.web:
        from ui import run_web

        return run_web(host=args.host, port=args.port)

    logger = setup_logging()

    try:
        settings = Settings.from_env()
        logger.setLevel(settings.log_level)
    except EnvironmentError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if settings.demo_mode:
        logger.warning("DEMO MODE — showing simulated data, not real market prices.")
        groww = None
    elif settings.data_source == "yahoo":
        logger.info("Data source: Yahoo Finance (delayed data, no Groww auth needed).")
        groww = None
    else:
        try:
            groww = create_client(settings, logger)
        except Exception as exc:
            logger.error("Authentication failed: %s", exc)
            return 3

    instruments = build_watchlist(settings)
    logger.info(
        "Watchlist: %s",
        ", ".join(f"{i.symbol}({i.kind})" for i in instruments),
    )

    watch = MarketWatch(groww=groww, instruments=instruments, settings=settings, logger=logger)
    try:
        watch.run()
    except Exception as exc:
        logger.exception("Fatal error in market watch: %s", exc)
        return 1
    finally:
        watch.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
