"""Command-line entry point.

    python -m nifty_options login                  # one-time Upstox OAuth
    python -m nifty_options mode                   # what will happen if I run?
    python -m nifty_options run                    # paper (default)
    python -m nifty_options run --mode live        # actual trading, if armed
    python -m nifty_options run --live --dry-run   # live path, orders logged only
    python -m nifty_options status
    python -m nifty_options report
    python -m nifty_options panic                  # kill switch + flatten
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .brokers.factory import build_broker, build_client, describe_mode
from .config import Config, ConfigError, LiveTradingBlocked, TradingMode
from .engine import Engine
from .journal import Journal, comparison_report
from .logging_setup import setup_logging
from .upstox.auth import build_login_url, exchange_code, load_token

LOG = logging.getLogger("nifty_options.cli")

LIVE_PROMPT = (
    "\nYou are about to switch from PAPER to ACTUAL trading.\n"
    "Real orders will be sent to the exchange against real money.\n"
    "Type LIVE to continue, anything else to abort: "
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nifty_options",
        description="Nifty 50 dual-track options framework on Upstox (paper or live).",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="trading mode; overrides UPSTOX_TRADING_MODE and config.yaml",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="shorthand for --mode live (actual trading)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --live: run the live path but log orders instead of sending them",
    )
    parser.add_argument("--yes", action="store_true", help="skip the interactive live confirmation")
    parser.add_argument("--log-level", default=None)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="print the Upstox login URL / exchange an auth code")
    sub.add_parser("mode", help="show the effective trading mode and why")

    run = sub.add_parser("run", help="run the strategy engine")
    run.add_argument("--poll", type=int, default=60, help="seconds between evaluations")
    run.add_argument("--ticks", type=int, default=None, help="stop after N evaluations")
    run.add_argument(
        "--track", choices=["a", "b", "both"], default="both", help="which track(s) to run"
    )
    run.add_argument("--once", action="store_true", help="evaluate a single tick and exit")

    sub.add_parser("status", help="broker, risk and open-position snapshot")

    report = sub.add_parser("report", help="tracking sheet + comparison metrics")
    report.add_argument("--markdown", type=Path, default=None, help="write the report to a file")

    panic = sub.add_parser("panic", help="engage the kill switch and square everything off")
    panic.add_argument("--no-flatten", action="store_true", help="kill switch only, keep positions")

    sub.add_parser("resume", help="release the kill switch")
    return parser


def load_config(args: argparse.Namespace) -> Config:
    mode = args.mode or ("live" if args.live else None)
    config = Config.load(args.config, mode=mode)
    if args.dry_run:
        config.live.dry_run = True
    if args.log_level:
        config.log_level = args.log_level
    config.ensure_dirs()
    setup_logging(config.log_level, config.state_dir.parent / "logs")
    return config


def confirm_live(config: Config, skip: bool) -> bool:
    """Final human gate. `--yes` bypasses it for scheduled/automated runs."""
    if not config.mode.is_live or config.live.dry_run:
        return True
    if skip:
        LOG.warning("Live confirmation skipped via --yes.")
        return True
    if not sys.stdin.isatty():
        LOG.error("Live mode needs an interactive confirmation (or pass --yes).")
        return False
    return input(LIVE_PROMPT).strip() == "LIVE"


# ---------------------------------------------------------------------- #
# commands
# ---------------------------------------------------------------------- #
def cmd_login(config: Config) -> int:
    token = load_token(config)
    if token:
        print(f"Existing token is valid until {token.expires_at}.")
        if input("Re-authenticate anyway? [y/N]: ").strip().lower() != "y":
            return 0
    print("\n1. Open this URL, log in, and approve access:\n")
    print(f"   {build_login_url(config)}\n")
    print("2. Copy the ?code=... value from the redirect URL.\n")
    code = input("Authorization code: ").strip()
    if not code:
        print("No code entered.")
        return 1
    token = exchange_code(config, code)
    print(f"Token saved to {config.upstox.token_path} (valid until {token.expires_at}).")
    return 0


def cmd_mode(config: Config) -> int:
    print(f"Effective mode : {config.mode.value.upper()}")
    print(f"What happens   : {describe_mode(config)}")
    print(f"Journal        : {config.journal_dir / f'tracking_sheet_{config.mode.value}.csv'}")
    print(f"Kill switch    : {config.risk.kill_switch_file} "
          f"({'ACTIVE' if config.risk.kill_switch_file.exists() else 'clear'})")
    token = load_token(config)
    print(f"Upstox token   : {'valid until ' + token.expires_at if token else 'missing'}")
    if not config.mode.is_live:
        print(
            "\nTo switch to actual trading:\n"
            "  1. config.yaml -> live.enabled: true\n"
            f'  2. export UPSTOX_LIVE_CONFIRM="{config.live.confirmation_phrase}"\n'
            "  3. python -m nifty_options run --live"
        )
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    if args.track == "a":
        config.track_b.enabled = False
    elif args.track == "b":
        config.track_a.enabled = False

    # Evaluate the live guards before anything else, so an unarmed live run
    # reports *that* rather than an incidental missing-token error.
    config.assert_live_allowed()

    if not confirm_live(config, args.yes):
        print("Aborted -- still in paper mode.")
        return 1

    client = build_client(config, required=config.mode.is_live)
    broker = build_broker(config, client)
    engine = Engine(config, broker, client)

    if args.once:
        print(json.dumps(engine.tick(), indent=2, default=str))
        return 0
    engine.run(poll_seconds=args.poll, max_ticks=args.ticks)
    return 0


def cmd_status(config: Config) -> int:
    client = build_client(config, required=config.mode.is_live)
    broker = build_broker(config, client)
    engine = Engine(config, broker, client)

    print(f"Mode      : {describe_mode(config)}")
    print(f"Broker    : {broker.name}")
    print(f"Summary   : {json.dumps(getattr(broker, 'summary', dict)(), default=str)}")
    print(f"Risk      : {json.dumps(engine.risk.status(), default=str)}")

    open_trades = [t for t in engine.open_trades if t.is_open]
    if not open_trades:
        print("Positions : none")
        return 0
    print("Positions :")
    for trade in open_trades:
        print(
            f"  - [{trade.track}] {trade.description} | {trade.lots} lot(s) | "
            f"entry {trade.entry_price:.2f} | stop {trade.stop_loss:.2f} | "
            f"target {trade.target:.2f} | opened {trade.opened_at:%Y-%m-%d %H:%M}"
        )
    return 0


def cmd_report(config: Config, args: argparse.Namespace) -> int:
    journal = Journal(config.journal_dir, config.mode.value)
    report = comparison_report(journal)
    print(report)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(report)
        print(f"\nWritten to {args.markdown}")
    return 0


def cmd_panic(config: Config, args: argparse.Namespace) -> int:
    client = build_client(config, required=False)
    broker = build_broker(config, client)
    engine = Engine(config, broker, client)
    engine.risk.engage_kill_switch("manual panic command")
    if not args.no_flatten:
        closed = engine.square_off_all("panic square-off")
        print(f"Squared off {len(closed)} trade(s).")
    print(f"Kill switch engaged: {config.risk.kill_switch_file}")
    print("Release it with:  python -m nifty_options resume")
    return 0


def cmd_resume(config: Config) -> int:
    config.risk.kill_switch_file.unlink(missing_ok=True)
    print(f"Kill switch cleared ({config.risk.kill_switch_file}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "login":
            return cmd_login(config)
        if args.command == "mode":
            return cmd_mode(config)
        if args.command == "run":
            return cmd_run(config, args)
        if args.command == "status":
            return cmd_status(config)
        if args.command == "report":
            return cmd_report(config, args)
        if args.command == "panic":
            return cmd_panic(config, args)
        if args.command == "resume":
            return cmd_resume(config)
    except LiveTradingBlocked as exc:
        print(f"\nLIVE TRADING BLOCKED\n{exc}\n", file=sys.stderr)
        return 3
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0
