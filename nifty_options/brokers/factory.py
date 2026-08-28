"""The paper <-> actual trading switch.

This is the single place in the codebase that decides whether orders are
simulated or real. Strategies, the engine and the risk manager all receive a
:class:`~nifty_options.brokers.base.Broker` and cannot tell the difference,
so the behaviour of a live run is identical to the paper run that preceded
it apart from where the orders land.
"""

from __future__ import annotations

import logging

from ..config import Config, ConfigError, TradingMode
from ..upstox.auth import load_token
from ..upstox.client import UpstoxClient
from .base import Broker
from .live import LiveBroker
from .paper import PaperBroker

LOG = logging.getLogger(__name__)

LIVE_BANNER = """
================================================================
  LIVE TRADING ARMED -- ORDERS WILL USE REAL MONEY
  Capital at risk : Rs {capital:,.0f}
  Max order value : Rs {max_order:,.0f}
  Kill switch     : touch {kill_switch}
================================================================
"""

SHADOW_BANNER = """
----------------------------------------------------------------
  SHADOW MODE -- live data, simulated fills
  Connected to the live Upstox account for quotes, the option
  chain, contract specs and margin. Fills are settled internally,
  so no order ever reaches the exchange.
  Journal: tracking_sheet_shadow.csv
----------------------------------------------------------------
"""


def build_client(config: Config, required: bool = False) -> UpstoxClient | None:
    """Create an authenticated Upstox client, if a token is available.

    Paper mode runs without one (no live quotes, fills come from the prices
    the strategy already holds); live mode always requires it.
    """
    token = load_token(config)
    if token is None:
        if required:
            raise ConfigError(
                "No valid Upstox access token. Run:  python -m nifty_options login"
            )
        LOG.warning(
            "No Upstox token found -- paper mode will run without live market data."
        )
        return None
    return UpstoxClient(token.access_token)


def build_broker(
    config: Config,
    client: UpstoxClient | None = None,
    mode: str | TradingMode | None = None,
) -> Broker:
    """Return the broker for the effective mode, enforcing the live guards."""
    if mode is not None:
        config = config.with_mode(mode)

    config.ensure_dirs()

    if not config.mode.is_live:
        client = client or build_client(config, required=False)
        LOG.info("Trading mode: PAPER (simulated fills, no real orders)")
        return PaperBroker(config, client)

    # ---- live path: every guard must pass before we get a live broker ----
    config.assert_live_allowed()
    client = client or build_client(config, required=True)
    assert client is not None

    if config.live.dry_run:
        # Shadow: armed live and reading the live account, but handed a
        # PaperBroker, which has no order-placing code path at all. The
        # exchange is unreachable from here by construction, not by a flag.
        LOG.warning(SHADOW_BANNER)
        return PaperBroker(config, client)

    LOG.warning(
        LIVE_BANNER.format(
            capital=config.track_a.capital + config.track_b.capital,
            max_order=config.risk.live_max_order_value,
            kill_switch=config.risk.kill_switch_file,
        )
    )
    return LiveBroker(config, client)


def describe_mode(config: Config) -> str:
    """Human-readable answer to 'am I about to trade real money?'."""
    if not config.mode.is_live:
        return (
            "PAPER -- live market data, simulated fills. "
            "Nothing is sent to the exchange."
        )
    try:
        config.assert_live_allowed()
    except ConfigError as exc:
        return f"LIVE requested but BLOCKED: {exc}"
    if config.live.dry_run:
        return (
            "SHADOW -- armed live and reading the live account, but fills are "
            "simulated. No order can reach the exchange."
        )
    return "LIVE -- real orders will be placed on Upstox."
