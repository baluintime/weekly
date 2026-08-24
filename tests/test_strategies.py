"""Track A and Track B rules, checked against the framework's numbers."""

from __future__ import annotations

from datetime import datetime

import pytest

from nifty_options.brokers.base import Side
from nifty_options.indicators import to_candles
from nifty_options.strategies.base import MarketContext, Trade
from nifty_options.strategies.track_a import TrackAIntradayMomentum
from nifty_options.strategies.track_b import TrackBWeeklyCondor
from nifty_options.upstox.instruments import parse_chain

from .conftest import build_candles, build_chain


def context(now: datetime, breakout: bool = False, spot: float = 24_000.0) -> MarketContext:
    rows = list(reversed(build_candles(breakout=breakout)))     # oldest-first
    chain = parse_chain(build_chain(spot))
    return MarketContext(
        now=now,
        spot=spot,
        candles=to_candles(rows),
        chain=chain,
        expiry="2026-08-27",
        prices={q.instrument_key: q.ltp for q in chain},
    )


# ---------------------------------------------------------------------- #
# Track A
# ---------------------------------------------------------------------- #
def test_no_entry_without_a_breakout(config):
    strategy = TrackAIntradayMomentum(config)
    assert strategy.evaluate_entry(context(datetime(2026, 8, 24, 11, 0)), []) is None


def test_breakout_produces_a_debit_plan(config):
    strategy = TrackAIntradayMomentum(config)
    plan = strategy.evaluate_entry(context(datetime(2026, 8, 24, 11, 0), breakout=True), [])
    assert plan is not None
    assert plan.track == "Track A"
    assert plan.direction == 1
    assert len(plan.legs) == 1
    assert plan.legs[0].side is Side.BUY


def test_selected_strike_sits_in_the_delta_band(config):
    strategy = TrackAIntradayMomentum(config)
    plan = strategy.evaluate_entry(context(datetime(2026, 8, 24, 11, 0), breakout=True), [])
    delta = abs(plan.legs[0].delta)
    assert config.track_a.min_delta <= delta <= config.track_a.max_delta


def test_outlay_stays_under_the_cap(config):
    strategy = TrackAIntradayMomentum(config)
    plan = strategy.evaluate_entry(context(datetime(2026, 8, 24, 11, 0), breakout=True), [])
    outlay = plan.legs[0].entry_price * plan.legs[0].quantity
    assert outlay <= config.track_a.max_outlay_per_trade


def test_risk_never_exceeds_two_percent(config):
    strategy = TrackAIntradayMomentum(config)
    plan = strategy.evaluate_entry(context(datetime(2026, 8, 24, 11, 0), breakout=True), [])
    assert plan.max_loss <= config.track_a.risk_per_trade + 1e-6
    assert config.track_a.risk_per_trade == 4_000.0     # 2% of Rs 2,00,000


def test_target_is_two_times_the_risk(config):
    strategy = TrackAIntradayMomentum(config)
    plan = strategy.evaluate_entry(context(datetime(2026, 8, 24, 11, 0), breakout=True), [])
    entry = plan.legs[0].entry_price
    risk = entry - plan.stop_loss
    reward = plan.target - entry
    assert reward == pytest.approx(risk * config.track_a.reward_to_risk, rel=0.02)


def test_no_entry_outside_the_window(config):
    strategy = TrackAIntradayMomentum(config)
    assert strategy.evaluate_entry(
        context(datetime(2026, 8, 24, 15, 20), breakout=True), []
    ) is None


def test_only_one_track_a_trade_at_a_time(config):
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper")
    assert strategy.evaluate_entry(ctx, [trade]) is None


def test_stop_loss_exit(config):
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper")
    ctx.prices[plan.legs[0].instrument_key] = plan.stop_loss - 1
    assert "stop-loss" in strategy.evaluate_exit(trade, ctx)


def test_target_exit(config):
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper")
    ctx.prices[plan.legs[0].instrument_key] = plan.target + 1
    assert "target hit" in strategy.evaluate_exit(trade, ctx)


def test_square_off_time_forces_an_exit(config):
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper")
    ctx.now = datetime(2026, 8, 24, 15, 15)
    assert "square-off" in strategy.evaluate_exit(trade, ctx)


def test_sizing_rejects_an_unaffordable_premium(config):
    strategy = TrackAIntradayMomentum(config)
    assert strategy.size_position(500.0) == 0      # 500 * 75 = Rs 37,500 > cap


# ---------------------------------------------------------------------- #
# Track B
# ---------------------------------------------------------------------- #
def monday(hour: int = 10) -> datetime:
    day = datetime(2026, 8, 24, hour, 0)           # a Monday
    assert day.weekday() == 0
    return day


def test_condor_has_four_legs_with_correct_sides(config):
    strategy = TrackBWeeklyCondor(config)
    plan = strategy.evaluate_entry(context(monday()), [])
    assert plan is not None
    roles = {leg.role: leg for leg in plan.legs}
    assert set(roles) == {"short_call", "long_call", "short_put", "long_put"}
    assert roles["short_call"].side is Side.SELL
    assert roles["long_call"].side is Side.BUY
    assert roles["short_put"].side is Side.SELL
    assert roles["long_put"].side is Side.BUY


def test_short_legs_sit_near_delta_015(config):
    strategy = TrackBWeeklyCondor(config)
    plan = strategy.evaluate_entry(context(monday()), [])
    for leg in plan.legs:
        if leg.role.startswith("short"):
            assert abs(abs(leg.delta) - 0.15) <= config.track_b.delta_tolerance


def test_wings_are_a_hundred_points_wide(config):
    strategy = TrackBWeeklyCondor(config)
    plan = strategy.evaluate_entry(context(monday()), [])
    roles = {leg.role: leg for leg in plan.legs}
    assert roles["long_call"].strike - roles["short_call"].strike == 100
    assert roles["short_put"].strike - roles["long_put"].strike == 100


def test_structure_is_a_credit_with_capped_loss(config):
    strategy = TrackBWeeklyCondor(config)
    plan = strategy.evaluate_entry(context(monday()), [])
    assert plan.direction == -1
    credit = plan.net_premium
    quantity = plan.legs[0].quantity
    assert plan.max_loss == pytest.approx((100 - credit) * quantity, rel=0.01)


def test_no_entry_on_a_non_entry_day_without_exhaustion(config):
    strategy = TrackBWeeklyCondor(config)
    wednesday = datetime(2026, 8, 26, 10, 0)
    assert wednesday.weekday() == 2
    assert strategy.evaluate_entry(context(wednesday), []) is None


def test_no_entry_outside_the_dte_window(config):
    strategy = TrackBWeeklyCondor(config)
    ctx = context(monday())
    ctx.expiry = "2026-09-24"                      # a month out
    assert strategy.evaluate_entry(ctx, []) is None


def test_profit_target_exit_at_credit_capture(config):
    strategy = TrackBWeeklyCondor(config)
    ctx = context(monday())
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper", ctx.expiry)
    # Halve every leg's premium -> ~50%+ of the credit captured.
    for leg in plan.legs:
        ctx.prices[leg.instrument_key] = ctx.prices[leg.instrument_key] * 0.4
    reason = strategy.evaluate_exit(trade, ctx)
    assert reason and "credit captured" in reason


def test_short_strike_touch_triggers_an_exit(config):
    strategy = TrackBWeeklyCondor(config)
    ctx = context(monday())
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper", ctx.expiry)
    short_call = next(leg for leg in plan.legs if leg.role == "short_call")
    ctx.spot = short_call.strike + 5
    reason = strategy.evaluate_exit(trade, ctx)
    assert reason and "short call" in reason


def test_tested_legs_are_the_breached_vertical(config):
    strategy = TrackBWeeklyCondor(config)
    ctx = context(monday())
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper", ctx.expiry)
    short_put = next(leg for leg in plan.legs if leg.role == "short_put")
    legs = strategy.tested_legs(trade, short_put.strike - 10)
    assert {leg.role for leg in legs} == {"short_put", "long_put"}


def test_margin_allocation_matches_the_framework(config):
    strategy = TrackBWeeklyCondor(config)
    lots = strategy.size_position(credit=42.0, margin_per_lot=60_000)
    assert lots == 2                                # ~Rs 1.2 lakh of margin
    assert lots * 60_000 <= config.track_b.max_margin


# ---------------------------------------------------------------------- #
# Track A: daily cap and re-entry cooldown
# ---------------------------------------------------------------------- #
def _closed_trade(config, ctx, closed_at: datetime) -> Trade:
    strategy = TrackAIntradayMomentum(config)
    plan = strategy.evaluate_entry(ctx, [])
    trade = Trade.from_plan(plan, ctx.now, plan.net_premium, config.lot_size, "paper")
    trade.closed_at = closed_at
    trade.exit_price = plan.target
    return trade


def test_cooldown_blocks_an_immediate_re_entry(config):
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    closed = _closed_trade(config, ctx, datetime(2026, 8, 24, 11, 0))
    ctx.now = datetime(2026, 8, 24, 11, 5)          # 5 min later, same setup
    assert strategy.evaluate_entry(ctx, [closed]) is None


def test_entry_is_allowed_once_the_cooldown_lapses(config):
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    closed = _closed_trade(config, ctx, datetime(2026, 8, 24, 11, 0))
    ctx.now = datetime(2026, 8, 24, 11, 20)         # past the 15-min cooldown
    assert strategy.evaluate_entry(ctx, [closed]) is not None


def test_daily_trade_cap_is_enforced(config):
    config.track_a.max_trades_per_day = 2
    config.track_a.reentry_cooldown_minutes = 0
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    taken = [
        _closed_trade(config, ctx, datetime(2026, 8, 24, 10, 0)),
        _closed_trade(config, ctx, datetime(2026, 8, 24, 10, 30)),
    ]
    assert strategy.evaluate_entry(ctx, taken) is None


def test_yesterdays_trades_do_not_count_against_today(config):
    config.track_a.max_trades_per_day = 1
    config.track_a.reentry_cooldown_minutes = 0
    strategy = TrackAIntradayMomentum(config)
    ctx = context(datetime(2026, 8, 24, 11, 0), breakout=True)
    stale = _closed_trade(config, ctx, datetime(2026, 8, 21, 14, 0))
    stale.opened_at = datetime(2026, 8, 21, 13, 0)
    assert strategy.evaluate_entry(ctx, [stale]) is not None
