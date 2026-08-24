"""End-to-end engine behaviour, offline, in both trading modes."""

from __future__ import annotations

from datetime import datetime

import pytest

from nifty_options.brokers import build_broker
from nifty_options.engine import Engine
from nifty_options.journal import Journal, comparison_report, evaluate
from nifty_options.risk import RiskManager

from .conftest import FakeUpstoxClient, this_weeks_monday
from .test_mode_switch import arm_live


def make_engine(config, client, mode=None):
    if mode:
        config = config.with_mode(mode)
    broker = build_broker(config, client, mode=mode)
    return Engine(config, broker, client)


def monday_11am() -> datetime:
    """This week's Monday, so journal dates stay inside the lookback window."""
    return datetime.combine(this_weeks_monday(), datetime.min.time()).replace(hour=11)


def later(hours: int = 0, minutes: int = 0, days: int = 0) -> datetime:
    from datetime import timedelta

    return monday_11am() + timedelta(days=days, hours=hours, minutes=minutes)


# ---------------------------------------------------------------------- #
def test_tick_with_no_setup_opens_nothing(config):
    client = FakeUpstoxClient(breakout=False)
    engine = make_engine(config, client)
    ctx = engine.build_context(now=later(days=1, hours=1))         # Tuesday
    report = engine.tick(ctx)
    assert report["status"] == "ok"
    assert report["opened"] == []


def test_track_a_breakout_opens_and_journals_a_round_trip(config):
    config.track_b.enabled = False
    client = FakeUpstoxClient(breakout=True)
    engine = make_engine(config, client)

    ctx = engine.build_context(now=monday_11am())
    opened = engine.tick(ctx)["opened"]
    assert opened and "CE" in opened[0]
    trade = engine.open_trades[0]

    # Move the option to its target and re-evaluate.
    leg = trade.legs[0]
    ctx = engine.build_context(now=later(minutes=30))
    ctx.prices[leg.instrument_key] = trade.target + 5
    closed = engine.tick(ctx)["closed"]

    assert closed and "target hit" in closed[0]
    rows = engine.journal.rows()
    assert len(rows) == 1
    assert rows[0]["Track"] == "Track A"
    assert float(rows[0]["Realized PnL (Rs)"]) > 0


def test_track_b_condor_opens_on_monday(config):
    config.track_a.enabled = False
    client = FakeUpstoxClient()
    engine = make_engine(config, client)

    report = engine.tick(engine.build_context(now=monday_11am()))
    assert report["opened"] and "Condor" in report["opened"][0]
    trade = engine.open_trades[0]
    assert len(trade.legs) == 4
    assert trade.direction == -1


def test_condor_profit_capture_is_journalled_as_a_gain(config):
    config.track_a.enabled = False
    client = FakeUpstoxClient()
    engine = make_engine(config, client)
    engine.tick(engine.build_context(now=monday_11am()))
    trade = engine.open_trades[0]

    for leg in trade.legs:                       # premiums decay
        client.set_price(leg.instrument_key, client.prices[leg.instrument_key] * 0.35)

    report = engine.tick(engine.build_context(now=later(days=1)))
    assert report["closed"]
    row = engine.journal.rows()[0]
    assert float(row["Net Points"]) > 0
    assert float(row["Realized PnL (Rs)"]) > 0
    assert row["Track"] == "Track B"


def test_open_trades_survive_a_restart(config):
    config.track_a.enabled = False
    client = FakeUpstoxClient()
    engine = make_engine(config, client)
    engine.tick(engine.build_context(now=monday_11am()))
    assert len(engine.open_trades) == 1

    reopened = Engine(config, engine.broker, client)
    assert len(reopened.open_trades) == 1
    assert reopened.open_trades[0].description == engine.open_trades[0].description
    assert len(reopened.open_trades[0].legs) == 4


def test_paper_and_live_take_the_same_decision(config, monkeypatch):
    """The switch must change only *where* orders go, never *what* is decided."""
    config.track_b.enabled = False

    paper_client = FakeUpstoxClient(breakout=True)
    paper = make_engine(config, paper_client)
    paper_report = paper.tick(paper.build_context(now=monday_11am()))

    live_config = config.with_mode("live")
    arm_live(live_config, monkeypatch)
    live_config.live.require_market_hours = False
    live_client = FakeUpstoxClient(breakout=True)
    live = Engine(live_config, build_broker(live_config, live_client, mode="live"), live_client)
    live_report = live.tick(live.build_context(now=monday_11am()))

    assert paper_report["opened"] == live_report["opened"]
    assert paper_client.placed == []             # paper never reaches the API
    assert live_client.placed != []              # live does
    assert live.journal.path.name == "tracking_sheet_live.csv"
    assert paper.journal.path.name == "tracking_sheet_paper.csv"


def test_journals_are_separate_per_mode(config, monkeypatch):
    paper = Journal(config.journal_dir, "paper")
    live = Journal(config.journal_dir, "live")
    assert paper.path != live.path


def test_kill_switch_blocks_new_entries(config):
    config.track_b.enabled = False
    client = FakeUpstoxClient(breakout=True)
    engine = make_engine(config, client)
    engine.risk.engage_kill_switch("test")
    report = engine.tick(engine.build_context(now=monday_11am()))
    assert report["opened"] == []


def test_daily_loss_limit_trips_the_kill_switch(config):
    client = FakeUpstoxClient()
    engine = make_engine(config, client)
    limit = engine.risk.daily_loss_limit
    engine.risk.record_trade(-limit - 1)
    assert engine.risk.kill_switch_active()
    assert engine.risk.status()["halted"] is True


def test_square_off_all_closes_and_journals(config):
    config.track_a.enabled = False
    client = FakeUpstoxClient()
    engine = make_engine(config, client)
    engine.tick(engine.build_context(now=monday_11am()))
    closed = engine.square_off_all("end of test")
    assert len(closed) == 1
    assert engine.open_trades == []
    assert engine.journal.rows()[0]["Exit Reason"] == "end of test"


def test_report_covers_both_tracks(config):
    from nifty_options.journal import JournalEntry

    journal = Journal(config.journal_dir, "paper")
    journal.record(JournalEntry("2026-08-25", "Track A", "24150 CE (Buy)", 112.5, 148.0, 35.5, 2662.5))
    journal.record(JournalEntry("2026-08-25", "Track B", "24000/24300 Condor", 42.0, 18.0, 24.0, 3600.0))
    journal.record(JournalEntry("2026-08-26", "Track A", "24200 PE (Buy)", 98.0, 76.0, -22.0, -1650.0))

    report = comparison_report(journal)
    assert "Track A" in report and "Track B" in report

    track_a = evaluate(journal.rows(), "Track A")
    assert track_a.trades == 2
    assert track_a.win_rate == 50.0
    assert track_a.gross_pnl == pytest.approx(1012.5)
    assert track_a.max_drawdown == pytest.approx(-1650.0)


# ---------------------------------------------------------------------- #
# entry caps across ticks and restarts
# ---------------------------------------------------------------------- #
def test_track_a_does_not_re_enter_on_the_same_tick(config):
    config.track_b.enabled = False
    client = FakeUpstoxClient(breakout=True)
    engine = make_engine(config, client)
    engine.tick(engine.build_context(now=monday_11am()))
    trade = engine.open_trades[0]

    client.set_price(trade.legs[0].instrument_key, trade.target + 5)
    report = engine.tick(engine.build_context(now=later(minutes=10)))

    assert report["closed"]
    assert report["opened"] == []          # cooldown holds the setup back
    assert report["open_trades"] == 0


def test_track_b_respects_the_weekly_cap(config):
    config.track_a.enabled = False
    config.track_b.max_trades_per_week = 1
    config.track_b.reentry_cooldown_minutes = 0
    client = FakeUpstoxClient()
    engine = make_engine(config, client)

    engine.tick(engine.build_context(now=monday_11am()))
    trade = engine.open_trades[0]
    for leg in trade.legs:
        client.set_price(leg.instrument_key, client.prices[leg.instrument_key] * 0.3)

    report = engine.tick(engine.build_context(now=later(minutes=20)))
    assert report["closed"]
    assert report["opened"] == []


def test_entry_caps_survive_a_restart(config):
    """A restart must not silently re-arm a setup the weekly cap retired."""
    config.track_a.enabled = False
    config.track_b.max_trades_per_week = 1
    config.track_b.reentry_cooldown_minutes = 0
    client = FakeUpstoxClient()
    engine = make_engine(config, client)

    engine.tick(engine.build_context(now=monday_11am()))
    trade = engine.open_trades[0]
    for leg in trade.legs:
        client.set_price(leg.instrument_key, client.prices[leg.instrument_key] * 0.3)
    engine.tick(engine.build_context(now=later(minutes=20)))
    assert engine.open_trades == []

    restarted = Engine(config, engine.broker, client)
    assert len(restarted.recent_closed) == 1
    assert restarted.recent_closed[0].track == "Track B"
