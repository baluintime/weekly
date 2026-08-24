"""Execution engine: pulls market data, runs both tracks, routes orders.

The engine holds a :class:`~nifty_options.brokers.base.Broker` handed to it by
:func:`~nifty_options.brokers.factory.build_broker`. It never inspects the
trading mode to decide *what* to trade -- only to label journal rows -- so a
live session executes exactly the strategy logic the paper session validated.
"""

from __future__ import annotations

import json
import logging
import time as time_module
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from .brokers.base import Broker, OrderRequest, OrderResult, Side
from .config import Config
from .indicators import to_candles
from .journal import Journal, JournalEntry
from .risk import RiskManager
from .strategies.base import Leg, MarketContext, Strategy, Trade, TradePlan
from .strategies.track_a import TrackAIntradayMomentum
from .strategies.track_b import TrackBWeeklyCondor
from .upstox.client import UpstoxAPIError, UpstoxClient
from .upstox.instruments import (
    OptionQuote,
    fetch_chain,
    list_expiries,
    nearest_weekly_expiry,
)

LOG = logging.getLogger(__name__)


def _parse_time(value: str, fallback_day: date) -> datetime:
    """Journal timestamps are ISO; fall back to midnight on the trade date."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.combine(fallback_day, datetime.min.time())


class Engine:
    def __init__(self, config: Config, broker: Broker, client: UpstoxClient | None = None):
        self.config = config
        self.broker = broker
        self.client = client or getattr(broker, "client", None)
        self.risk = RiskManager(config, broker)
        self.journal = Journal(config.journal_dir, config.mode.value)
        self.strategies: list[Strategy] = []
        if config.track_a.enabled:
            self.strategies.append(TrackAIntradayMomentum(config))
        if config.track_b.enabled:
            self.strategies.append(TrackBWeeklyCondor(config))
        self.open_trades: list[Trade] = []
        self.recent_closed: list[Trade] = []
        self.state_file = config.state_dir / f"open_trades_{config.mode.value}.json"
        self._expiry_cache: tuple[date, str] | None = None
        self._chain_cache: tuple[float, list[OptionQuote]] | None = None
        self._load_state()
        self._load_recent_closed()

    # ------------------------------------------------------------------ #
    # state persistence
    # ------------------------------------------------------------------ #
    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text())
        except json.JSONDecodeError:
            LOG.warning("Could not parse %s; starting with no open trades.", self.state_file)
            return
        for item in raw:
            legs = [
                Leg(**{**leg, "side": Side(leg["side"])}) for leg in item.pop("legs", [])
            ]
            item["opened_at"] = datetime.fromisoformat(item["opened_at"])
            if item.get("closed_at"):
                item["closed_at"] = datetime.fromisoformat(item["closed_at"])
            self.open_trades.append(Trade(legs=legs, **item))
        LOG.info("Restored %d open trade(s) from %s", len(self.open_trades), self.state_file)

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for trade in self.open_trades:
            if not trade.is_open:
                continue
            item = asdict(trade)
            item["opened_at"] = trade.opened_at.isoformat()
            item["closed_at"] = None
            for leg in item["legs"]:
                leg["side"] = leg["side"].value if hasattr(leg["side"], "value") else leg["side"]
            payload.append(item)
        self.state_file.write_text(json.dumps(payload, indent=2, default=str))

    def _prune_recent(self, today: date, days: int = 8) -> None:
        self.recent_closed = [
            t for t in self.recent_closed
            if t.closed_at and (today - t.closed_at.date()).days <= days
        ]

    def _load_recent_closed(self, days: int = 8) -> None:
        """Rebuild the last week of closed trades from the journal.

        Daily and weekly entry caps are counted off this list, so they have to
        survive a restart -- otherwise stopping and restarting the engine would
        silently reset them and re-arm a setup the rules had retired.
        """
        today = date.today()
        for row in self.journal.rows():
            try:
                traded_on = date.fromisoformat(row.get("Date", ""))
            except ValueError:
                continue
            if (today - traded_on).days > days:
                continue
            opened_at = _parse_time(row.get("Entry Time", ""), traded_on)
            closed_at = _parse_time(row.get("Exit Time", ""), traded_on)
            self.recent_closed.append(
                Trade(
                    trade_id=f"journal-{traded_on}-{len(self.recent_closed)}",
                    track=row.get("Track", ""),
                    strategy="",
                    description=row.get("Strategy / Legs", ""),
                    legs=[],
                    direction=1,
                    opened_at=opened_at,
                    entry_price=float(row.get("Entry", 0) or 0),
                    lots=int(row.get("Lots", 1) or 1),
                    lot_size=self.config.lot_size,
                    closed_at=closed_at,
                    exit_price=float(row.get("Exit", 0) or 0),
                    exit_reason=row.get("Exit Reason", ""),
                    mode=row.get("Mode", self.config.mode.value),
                )
            )
        if self.recent_closed:
            LOG.info(
                "Loaded %d recent closed trade(s) from the journal for entry-cap counting.",
                len(self.recent_closed),
            )

    # ------------------------------------------------------------------ #
    # market data
    # ------------------------------------------------------------------ #
    def build_context(self, now: datetime | None = None) -> MarketContext | None:
        if self.client is None:
            LOG.error("No Upstox client available -- cannot fetch market data.")
            return None

        now = now or datetime.now()
        try:
            spot = next(iter(self.client.get_ltp(self.config.instrument_key).values()), 0.0)
            rows = self.client.get_intraday_candles(
                self.config.instrument_key,
                "minutes",
                self.config.track_a.candle_interval_minutes,
            )
        except UpstoxAPIError as exc:
            LOG.error("Market data fetch failed: %s", exc)
            return None

        candles = to_candles(rows)
        if spot <= 0 and candles:
            spot = candles[-1].close

        expiry = self.current_expiry(now.date())
        chain = self.current_chain(expiry, spot)

        prices = {quote.instrument_key: quote.ltp for quote in chain}
        # The chain snapshot is cached between ticks, so open legs are always
        # re-marked from the live feed -- exits must never price off a stale
        # chain.
        held = sorted(
            {
                leg.instrument_key
                for trade in self.open_trades
                if trade.is_open
                for leg in trade.legs
            }
        )
        if held:
            try:
                prices.update(self.client.get_ltp(held))
            except UpstoxAPIError as exc:
                LOG.warning("Could not refresh prices for open legs: %s", exc)

        return MarketContext(
            now=now, spot=spot, candles=candles, chain=chain, expiry=expiry, prices=prices
        )

    def current_expiry(self, today: date) -> str:
        if self._expiry_cache and self._expiry_cache[0] == today:
            return self._expiry_cache[1]
        assert self.client is not None
        try:
            expiries = list_expiries(self.client, self.config.instrument_key)
        except UpstoxAPIError as exc:
            LOG.error("Expiry lookup failed: %s", exc)
            return ""
        expiry = nearest_weekly_expiry(
            expiries,
            today,
            min_days=0,
            max_days=max(self.config.track_b.max_days_to_expiry, 7),
        ) or ""
        self._expiry_cache = (today, expiry)
        LOG.info("Trading expiry: %s", expiry or "none found")
        return expiry

    def current_chain(self, expiry: str, spot: float) -> list[OptionQuote]:
        """Refetch the chain when spot has moved by more than half a strike."""
        if not expiry or self.client is None:
            return []
        if self._chain_cache and abs(self._chain_cache[0] - spot) < 25:
            return self._chain_cache[1]
        try:
            chain = fetch_chain(self.client, self.config.instrument_key, expiry)
        except UpstoxAPIError as exc:
            LOG.error("Option chain fetch failed: %s", exc)
            return self._chain_cache[1] if self._chain_cache else []
        self._chain_cache = (spot, chain)
        return chain

    def margin_per_lot(self, plan: TradePlan) -> float | None:
        """Ask Upstox what one lot of this structure actually blocks."""
        if self.client is None:
            return None
        instruments = [
            {
                "instrument_key": leg.instrument_key,
                "quantity": self.config.lot_size,
                "transaction_type": leg.side.value,
                "product": self.config.product,
                "price": leg.entry_price,
            }
            for leg in plan.legs
        ]
        try:
            data = self.client.get_charges_margin(instruments)
        except UpstoxAPIError as exc:
            LOG.debug("Margin quote unavailable: %s", exc)
            return None
        required = (data.get("margins") or [{}])[0] if isinstance(data, dict) else {}
        total = data.get("required_margin") if isinstance(data, dict) else None
        value = total or required.get("total_margin")
        return float(value) if value else None

    # ------------------------------------------------------------------ #
    # one evaluation tick
    # ------------------------------------------------------------------ #
    def tick(self, ctx: MarketContext | None = None) -> dict[str, Any]:
        ctx = ctx or self.build_context()
        if ctx is None:
            return {"status": "no-data"}

        self.risk.roll_day(ctx.today)
        self._prune_recent(ctx.today)
        closed = self.manage_open_trades(ctx)
        self.recent_closed.extend(closed)
        opened = self.look_for_entries(ctx)
        self._save_state()

        return {
            "status": "ok",
            "time": ctx.now.isoformat(timespec="seconds"),
            "spot": ctx.spot,
            "expiry": ctx.expiry,
            "opened": [t.description for t in opened],
            "closed": [f"{t.description}: {t.exit_reason}" for t in closed],
            "open_trades": len([t for t in self.open_trades if t.is_open]),
            "risk": self.risk.status(),
        }

    # ------------------------------------------------------------------ #
    def look_for_entries(self, ctx: MarketContext) -> list[Trade]:
        opened: list[Trade] = []
        for strategy in self.strategies:
            # Strategies see closed trades too, so daily caps and cooldowns
            # survive within the session.
            plan = strategy.evaluate_entry(ctx, self.open_trades + self.recent_closed)
            if plan is None:
                continue

            if isinstance(strategy, TrackBWeeklyCondor):
                margin = self.margin_per_lot(plan)
                if margin:
                    resized = strategy.build_condor(ctx, margin_per_lot=margin)
                    if resized is None:
                        continue
                    plan = resized

            trade = self.open_trade(plan, ctx)
            if trade:
                opened.append(trade)
        return opened

    def open_trade(self, plan: TradePlan, ctx: MarketContext) -> Trade | None:
        orders = plan.to_orders(self.config.product)
        for order in orders:
            allowed, reason = self.risk.can_open(order, opening=True)
            if not allowed:
                LOG.warning("Entry blocked for %s: %s", plan.description, reason)
                return None

        LOG.info("ENTRY %s | %s | %s", plan.track, plan.description, plan.rationale)
        results = self.broker.place_basket(orders)
        filled = [r for r in results if r.is_filled]

        if len(filled) != len(orders):
            LOG.error(
                "Partial/failed entry for %s (%d/%d legs). Unwinding filled legs.",
                plan.description, len(filled), len(orders),
            )
            self._unwind(filled)
            return None

        entry_price, charges = self._net_price(plan.legs, results)
        trade = Trade.from_plan(
            plan, ctx.now, entry_price, self.config.lot_size, self.config.mode.value, ctx.expiry
        )
        trade.charges = charges
        self.open_trades.append(trade)
        LOG.info(
            "OPENED %s at net %.2f (%d lot(s), max loss Rs %.0f)",
            trade.description, entry_price, trade.lots, plan.max_loss,
        )
        return trade

    def _net_price(
        self, legs: Sequence[Leg], results: Sequence[OrderResult]
    ) -> tuple[float, float]:
        """Net premium per unit from actual fills, plus total charges."""
        by_key = {r.request.instrument_key: r for r in results if r.is_filled}
        flow = 0.0
        for leg in legs:
            result = by_key.get(leg.instrument_key)
            if result is None:
                continue
            leg.entry_price = result.average_price or leg.entry_price
            flow += leg.side.sign * leg.entry_price
        charges = sum(r.charges for r in results)
        return abs(round(flow, 2)), round(charges, 2)

    def _unwind(self, filled: Sequence[OrderResult]) -> None:
        for result in filled:
            request = result.request
            self.broker.place_order(
                OrderRequest(
                    instrument_key=request.instrument_key,
                    symbol=request.symbol,
                    side=request.side.opposite,
                    quantity=result.filled_quantity,
                    product=request.product,
                    tag="unwind",
                    strategy=request.strategy,
                    leg=f"unwind-{request.leg}",
                )
            )

    # ------------------------------------------------------------------ #
    def manage_open_trades(self, ctx: MarketContext) -> list[Trade]:
        closed: list[Trade] = []
        for trade in [t for t in self.open_trades if t.is_open]:
            strategy = self._strategy_for(trade)
            if strategy is None:
                continue
            reason = strategy.evaluate_exit(trade, ctx)
            if reason:
                self.close_trade(trade, ctx, reason)
                closed.append(trade)
        self.open_trades = [t for t in self.open_trades if t.is_open]
        return closed

    def _strategy_for(self, trade: Trade) -> Strategy | None:
        for strategy in self.strategies:
            if strategy.name == trade.strategy:
                return strategy
        return None

    def close_trade(self, trade: Trade, ctx: MarketContext, reason: str) -> Trade:
        LOG.info("EXIT %s | %s | %s", trade.track, trade.description, reason)
        results = self.broker.place_basket(
            trade.exit_orders(self.config.product, prices=ctx.prices)
        )

        flow = 0.0
        for leg in trade.legs:
            result = next(
                (r for r in results if r.request.instrument_key == leg.instrument_key), None
            )
            price = (
                result.average_price
                if result and result.is_filled and result.average_price
                else ctx.price_of(leg.instrument_key, leg.entry_price)
            )
            leg.exit_price = price
            # Exit reverses the leg, so the flow sign flips.
            flow += -leg.side.sign * price

        trade.exit_price = abs(round(flow, 2))
        trade.exit_reason = reason
        trade.closed_at = ctx.now
        trade.charges = round(trade.charges + sum(r.charges for r in results), 2)

        pnl = trade.realized_pnl()
        self.risk.record_trade(pnl)
        self.journal.record(
            JournalEntry(
                date=ctx.today.isoformat(),
                track=trade.track,
                strategy=trade.description,
                entry=trade.entry_price,
                exit=trade.exit_price,
                net_points=trade.net_points(),
                realized_pnl=pnl,
                mode=self.config.mode.value,
                lots=trade.lots,
                charges=trade.charges,
                exit_reason=reason,
                entry_time=trade.opened_at.isoformat(timespec="seconds"),
                exit_time=ctx.now.isoformat(timespec="seconds"),
            )
        )
        LOG.info(
            "CLOSED %s at %.2f -> %+.2f pts, Rs %+.2f",
            trade.description, trade.exit_price, trade.net_points(), pnl,
        )
        return trade

    # ------------------------------------------------------------------ #
    def square_off_all(self, reason: str = "manual square-off") -> list[Trade]:
        ctx = self.build_context()
        if ctx is None:
            LOG.error("Cannot mark exits without market data; flattening via broker only.")
            self.broker.square_off_all()
            return []
        closed = [self.close_trade(t, ctx, reason) for t in self.open_trades if t.is_open]
        self.open_trades = []
        self._save_state()
        return closed

    def run(self, poll_seconds: int = 60, max_ticks: int | None = None) -> None:
        LOG.info(
            "Starting engine | mode=%s | broker=%s | tracks=%s",
            self.config.mode.value,
            self.broker.name,
            ", ".join(s.track for s in self.strategies) or "none",
        )
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            try:
                report = self.tick()
                if report.get("opened") or report.get("closed"):
                    LOG.info("Tick: %s", report)
                else:
                    LOG.debug("Tick: %s", report)
            except KeyboardInterrupt:
                LOG.warning("Interrupted -- leaving open positions untouched.")
                break
            except Exception:                       # keep the loop alive
                LOG.exception("Unhandled error during tick; continuing.")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            time_module.sleep(poll_seconds)
