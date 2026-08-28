"""Contract facts come from the exchange, never from assumptions in the code.

NSE has changed all of these: the Nifty lot has been 75, 50, 25 and 65, and
weekly expiry has moved between Thursday and Tuesday.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from nifty_options.brokers import build_broker
from nifty_options.engine import Engine
from nifty_options.upstox.contracts import (
    ContractSpec,
    build_spec,
    expiry_in_window,
    fallback_spec,
    normalise_tick_size,
    parse_contracts,
    strike_interval,
)

from .conftest import FakeUpstoxClient, build_contracts, condor_entry_day, expiry_calendar


def engine_for(config, client) -> Engine:
    return Engine(config, build_broker(config, client), client)


def entry_11am() -> datetime:
    return datetime.combine(condor_entry_day(), datetime.min.time()).replace(hour=11)


# ---------------------------------------------------------------------- #
# parsing
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [(5.0, 0.05), (0.05, 0.05), (25.0, 0.25), (0, 0.05)])
def test_tick_size_is_normalised_from_paise(raw, expected):
    """Upstox reports F&O ticks in paise; a 5.00 tick would be nonsense."""
    assert normalise_tick_size(raw) == expected


def test_strike_interval_is_the_modal_gap():
    assert strike_interval([24000, 24050, 24100, 24150, 25000]) == 50.0
    assert strike_interval([24000, 24100, 24200, 24300]) == 100.0


def test_strike_interval_survives_a_single_strike():
    assert strike_interval([24000]) == 50.0


def test_spec_is_built_from_the_contract_master():
    expiry = expiry_calendar()[0]
    rows = build_contracts([expiry], lot_size=65)
    spec = build_spec(rows, expiry, "NIFTY")
    assert spec.lot_size == 65
    assert spec.tick_size == 0.05
    assert spec.strike_interval == 50.0
    assert spec.freeze_quantity == 1800
    assert spec.from_exchange is True


def test_expiry_weekday_is_read_not_assumed():
    expiry = expiry_calendar()[0]
    spec = build_spec(build_contracts([expiry]), expiry, "NIFTY")
    assert spec.expiry_weekday == date.fromisoformat(expiry).strftime("%a").upper()


def test_freeze_quantity_bounds_a_single_order():
    expiry = expiry_calendar()[0]
    spec = build_spec(build_contracts([expiry], lot_size=65), expiry, "NIFTY")
    assert spec.max_lots_per_order() == 1800 // 65


def test_parse_groups_every_listed_expiry():
    expiries = expiry_calendar(count=3)
    parsed = parse_contracts(build_contracts(expiries))
    assert [e.expiry for e in parsed["expiries"]] == sorted(expiries)
    assert all(e.lot_size == 65 for e in parsed["expiries"])


def test_fallback_is_marked_as_not_from_the_exchange():
    spec = fallback_spec("NIFTY", "", 75, 0.05)
    assert spec.from_exchange is False
    assert "fallback" in spec.describe()
    assert spec.expiry_weekday == "?"          # must not crash without an expiry


# ---------------------------------------------------------------------- #
# expiry selection
# ---------------------------------------------------------------------- #
def test_window_picks_the_longest_hold_available():
    today = date(2026, 8, 27)                  # Thursday
    parsed = parse_contracts(build_contracts(expiry_calendar(today, count=3)))
    chosen = expiry_in_window(parsed["expiries"], today, 2, 5)
    assert chosen is not None
    assert 2 <= chosen.days_to_expiry(today) <= 5


def test_window_is_empty_when_every_expiry_is_too_far():
    today = date(2026, 8, 27)
    parsed = parse_contracts(build_contracts(["2026-12-29"]))
    assert expiry_in_window(parsed["expiries"], today, 2, 5) is None


def test_window_follows_a_changed_expiry_weekday():
    """A Tuesday expiry moves the entry day; the window logic must not care."""
    tuesday = date(2026, 9, 1)
    thursday = date(2026, 9, 3)
    for expiry_day in (tuesday, thursday):
        parsed = parse_contracts(build_contracts([expiry_day.isoformat()]))
        entry = expiry_day - timedelta(days=3)
        assert expiry_in_window(parsed["expiries"], entry, 2, 5) is not None


# ---------------------------------------------------------------------- #
# the engine applies what it fetched
# ---------------------------------------------------------------------- #
def test_engine_overrides_a_stale_configured_lot_size(config):
    config.lot_size = 75                       # the old, wrong value
    client = FakeUpstoxClient(lot_size=65)
    engine = engine_for(config, client)
    engine.build_context(now=entry_11am())
    assert engine.spec.lot_size == 65
    assert config.lot_size == 65               # pushed to everything downstream


def test_engine_adopts_the_exchange_tick_size(config):
    config.paper.tick_size = 0.10
    client = FakeUpstoxClient()
    engine = engine_for(config, client)
    engine.build_context(now=entry_11am())
    assert config.paper.tick_size == 0.05


def test_orders_are_sized_in_exchange_lots(config):
    config.track_b.enabled = False
    config.lot_size = 75
    client = FakeUpstoxClient(breakout=True, lot_size=65)
    engine = engine_for(config, client)
    engine.tick(engine.build_context(now=entry_11am()))
    trade = engine.open_trades[0]
    assert trade.lot_size == 65
    assert trade.quantity % 65 == 0
    assert client.placed == [] or all(o["quantity"] % 65 == 0 for o in client.placed)


def test_engine_trades_the_expiry_the_exchange_lists(config):
    client = FakeUpstoxClient(breakout=True)
    engine = engine_for(config, client)
    engine.tick(engine.build_context(now=entry_11am()))
    assert engine.spec.expiry == expiry_calendar()[0]
    for trade in engine.open_trades:
        assert trade.expiry == expiry_calendar()[0]


def test_spec_is_refetched_on_a_new_session_day(config):
    client = FakeUpstoxClient()
    engine = engine_for(config, client)
    engine.build_context(now=entry_11am())
    first = engine._spec_day
    engine.build_context(now=entry_11am() + timedelta(days=1))
    assert engine._spec_day != first


def test_engine_falls_back_loudly_when_the_master_is_unreachable(config, caplog):
    from nifty_options.upstox.client import UpstoxAPIError

    client = FakeUpstoxClient()

    def boom(*args, **kwargs):
        raise UpstoxAPIError("service unavailable")

    client.get_option_contracts = boom
    engine = engine_for(config, client)
    engine.build_context(now=entry_11am())
    assert engine.spec is not None
    assert engine.spec.from_exchange is False
    assert engine.spec.lot_size == config.lot_size


def test_condor_uses_the_calendar_not_a_hardcoded_weekday(config):
    """Entry follows days-to-expiry, so a Tuesday expiry still trades."""
    config.track_a.enabled = False
    assert config.track_b.entry_days == ()      # calendar-driven by default
    client = FakeUpstoxClient()
    engine = engine_for(config, client)
    report = engine.tick(engine.build_context(now=entry_11am()))
    assert report["opened"], report["waiting_on"]
    trade = engine.open_trades[0]
    assert 2 <= (date.fromisoformat(trade.expiry) - entry_11am().date()).days <= 5
