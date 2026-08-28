"""The paper <-> actual trading switch: resolution, guards and blast radius."""

from __future__ import annotations

import pytest

from nifty_options.brokers import LiveBroker, PaperBroker, build_broker, describe_mode
from nifty_options.brokers.base import OrderRequest, OrderStatus, Side
from nifty_options.config import Config, ConfigError, LiveTradingBlocked, TradingMode
from nifty_options.upstox.auth import Token, save_token

from .conftest import NIFTY_LOT_SIZE


def arm_live(config: Config, monkeypatch) -> None:
    """Satisfy every live guard so the switch is allowed to flip."""
    config.live.enabled = True
    config.upstox.api_key = "key"
    config.upstox.api_secret = "secret"
    monkeypatch.setenv("UPSTOX_LIVE_CONFIRM", config.live.confirmation_phrase)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "test-token")


# ---------------------------------------------------------------------- #
# mode resolution
# ---------------------------------------------------------------------- #
def test_default_mode_is_paper(config):
    assert config.mode is TradingMode.PAPER


def test_explicit_argument_beats_environment(monkeypatch):
    monkeypatch.setenv("UPSTOX_TRADING_MODE", "live")
    assert Config.resolve_mode("paper") is TradingMode.PAPER
    assert Config.resolve_mode(None) is TradingMode.LIVE


def test_environment_beats_config_file(monkeypatch):
    monkeypatch.setenv("UPSTOX_TRADING_MODE", "live")
    assert Config.resolve_mode(None, "paper") is TradingMode.LIVE


@pytest.mark.parametrize("alias", ["live", "LIVE", "real", "actual"])
def test_live_aliases(alias):
    assert Config.resolve_mode(alias) is TradingMode.LIVE


def test_unknown_mode_is_rejected():
    with pytest.raises(ConfigError):
        Config.resolve_mode("semi-live")


# ---------------------------------------------------------------------- #
# guards
# ---------------------------------------------------------------------- #
def test_live_blocked_when_not_enabled(config):
    live = config.with_mode("live")
    with pytest.raises(LiveTradingBlocked, match="live.enabled"):
        live.assert_live_allowed()


def test_live_blocked_without_confirmation_phrase(config, monkeypatch):
    monkeypatch.delenv("UPSTOX_LIVE_CONFIRM", raising=False)
    live = config.with_mode("live")
    live.live.enabled = True
    live.upstox.api_key = "key"
    live.upstox.api_secret = "secret"
    with pytest.raises(LiveTradingBlocked, match="UPSTOX_LIVE_CONFIRM"):
        live.assert_live_allowed()


def test_live_blocked_on_wrong_confirmation_phrase(config, monkeypatch):
    monkeypatch.setenv("UPSTOX_LIVE_CONFIRM", "yes go ahead")
    live = config.with_mode("live")
    live.live.enabled = True
    live.upstox.api_key = "key"
    live.upstox.api_secret = "secret"
    with pytest.raises(LiveTradingBlocked):
        live.assert_live_allowed()


def test_live_blocked_without_credentials(config, monkeypatch):
    monkeypatch.setenv("UPSTOX_LIVE_CONFIRM", config.live.confirmation_phrase)
    live = config.with_mode("live")
    live.live.enabled = True
    with pytest.raises(LiveTradingBlocked, match="UPSTOX_API_KEY"):
        live.assert_live_allowed()


def test_live_blocked_above_capital_ceiling(config, monkeypatch):
    live = config.with_mode("live")
    arm_live(live, monkeypatch)
    live.live.max_capital = 100_000
    with pytest.raises(LiveTradingBlocked, match="max_capital"):
        live.assert_live_allowed()


def test_paper_mode_never_needs_guards(config):
    config.assert_live_allowed()          # no exception


# ---------------------------------------------------------------------- #
# the factory
# ---------------------------------------------------------------------- #
def test_factory_returns_paper_broker_by_default(config, fake_client):
    broker = build_broker(config, fake_client)
    assert isinstance(broker, PaperBroker)
    assert broker.is_live is False


def test_factory_refuses_live_until_armed(config, fake_client):
    with pytest.raises(LiveTradingBlocked):
        build_broker(config, fake_client, mode="live")


def test_factory_returns_live_broker_once_armed(config, fake_client, monkeypatch):
    arm_live(config, monkeypatch)
    broker = build_broker(config, fake_client, mode="live")
    assert isinstance(broker, LiveBroker)
    assert broker.is_live is True


def test_switching_mode_does_not_mutate_the_original(config, fake_client, monkeypatch):
    arm_live(config, monkeypatch)
    live = config.with_mode("live")
    assert live.mode is TradingMode.LIVE
    assert config.mode is TradingMode.PAPER


def test_describe_mode_explains_the_block(config):
    assert "PAPER" in describe_mode(config)
    assert "BLOCKED" in describe_mode(config.with_mode("live"))


def test_describe_mode_when_armed(config, monkeypatch):
    arm_live(config, monkeypatch)
    assert "real orders" in describe_mode(config.with_mode("live")).lower()


# ---------------------------------------------------------------------- #
# live broker pre-trade gate
# ---------------------------------------------------------------------- #
def order(**overrides) -> OrderRequest:
    defaults = dict(
        instrument_key="NSE_FO|C24300",
        symbol="NIFTY 24300 CE",
        side=Side.BUY,
        quantity=NIFTY_LOT_SIZE,
        price=100.0,
        strategy="track_a_intraday_debit",
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


@pytest.fixture
def live_broker(config, fake_client, monkeypatch):
    arm_live(config, monkeypatch)
    live = config.with_mode("live")
    live.live.require_market_hours = False
    return build_broker(live, fake_client, mode="live")


def test_dry_run_sends_nothing(config, fake_client, monkeypatch):
    arm_live(config, monkeypatch)
    live = config.with_mode("live")
    live.live.dry_run = True
    live.live.require_market_hours = False
    broker = build_broker(live, fake_client, mode="live")
    result = broker.place_order(order())
    assert result.order_id == "DRYRUN"
    assert fake_client.placed == []


def test_kill_switch_blocks_live_orders(live_broker, fake_client, config):
    live_broker.config.risk.kill_switch_file.write_text("halt")
    result = live_broker.place_order(order())
    assert result.status is OrderStatus.REJECTED
    assert "kill switch" in result.message.lower()
    assert fake_client.placed == []


def test_non_fo_instrument_is_refused(live_broker, fake_client):
    result = live_broker.place_order(order(instrument_key="NSE_EQ|INE002A01018"))
    assert result.status is OrderStatus.REJECTED
    assert fake_client.placed == []


def test_odd_lot_quantity_is_refused(live_broker, fake_client):
    result = live_broker.place_order(order(quantity=NIFTY_LOT_SIZE - 1))
    assert result.status is OrderStatus.REJECTED
    assert "lot size" in result.message


def test_order_value_ceiling_is_enforced(live_broker, fake_client):
    result = live_broker.place_order(order(quantity=NIFTY_LOT_SIZE * 20, price=500.0))
    assert result.status is OrderStatus.REJECTED
    assert "live_max_order_value" in result.message
    assert fake_client.placed == []


def test_daily_order_cap_is_enforced(live_broker, fake_client):
    live_broker.config.risk.live_max_daily_orders = 2
    for _ in range(2):
        assert live_broker.place_order(order()).status is not OrderStatus.REJECTED
    blocked = live_broker.place_order(order())
    assert blocked.status is OrderStatus.REJECTED
    assert "Daily live order cap" in blocked.message


def test_market_hours_guard(config, fake_client, monkeypatch):
    arm_live(config, monkeypatch)
    live = config.with_mode("live")
    live.live.require_market_hours = True
    broker = build_broker(live, fake_client, mode="live")
    from datetime import datetime

    monkeypatch.setattr(
        type(broker), "is_market_open", staticmethod(lambda now=None: False)
    )
    result = broker.place_order(order())
    assert result.status is OrderStatus.REJECTED
    assert "Market is closed" in result.message


def test_armed_live_broker_actually_places(live_broker, fake_client):
    result = live_broker.place_order(order())
    assert result.status is OrderStatus.COMPLETE
    assert len(fake_client.placed) == 1
    assert fake_client.placed[0]["transaction_type"] == "BUY"
