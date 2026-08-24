"""Shared strategy primitives: legs, trades, plans and the strategy interface.

Strategies are pure decision-makers. They read a :class:`MarketContext` and
return a :class:`TradePlan` or an exit reason; they never touch a broker, so
the same code drives paper and live runs identically.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Sequence

from ..brokers.base import OrderRequest, OrderType, Side
from ..config import Config
from ..indicators import Candle
from ..upstox.instruments import OptionQuote


@dataclass
class MarketContext:
    """Everything a strategy is allowed to look at on one evaluation tick."""

    now: datetime
    spot: float
    candles: list[Candle] = field(default_factory=list)
    chain: list[OptionQuote] = field(default_factory=list)
    expiry: str = ""
    prices: dict[str, float] = field(default_factory=dict)

    @property
    def today(self) -> date:
        return self.now.date()

    def price_of(self, instrument_key: str, default: float = 0.0) -> float:
        if instrument_key in self.prices:
            return self.prices[instrument_key]
        for quote in self.chain:
            if quote.instrument_key == instrument_key:
                return quote.ltp
        return default


@dataclass
class Leg:
    instrument_key: str
    symbol: str
    side: Side
    lots: int
    quantity: int
    entry_price: float = 0.0
    exit_price: float = 0.0
    delta: float = 0.0
    strike: float = 0.0
    option_type: str = ""
    role: str = ""                # short_call / long_call / short_put / long_put / debit

    @property
    def signed_entry(self) -> float:
        """Premium flow at entry: positive = paid, negative = received."""
        return self.side.sign * self.entry_price


@dataclass
class TradePlan:
    track: str
    strategy: str
    description: str
    legs: list[Leg]
    direction: int                # +1 debit (pay to open), -1 credit (receive)
    stop_loss: float = 0.0        # net premium level that closes the trade
    target: float = 0.0
    max_loss: float = 0.0
    rationale: str = ""

    @property
    def net_premium(self) -> float:
        """Net premium per unit: positive for a debit, positive credit magnitude."""
        flow = sum(leg.signed_entry for leg in self.legs)
        return abs(round(flow, 2))

    @property
    def lots(self) -> int:
        return max((leg.lots for leg in self.legs), default=0)

    def to_orders(self, product: str, tag: str = "nifty-options") -> list[OrderRequest]:
        """Entry orders. Protective (long) legs go first so a partial basket
        never leaves a naked short."""
        ordered = sorted(self.legs, key=lambda leg: 0 if leg.side is Side.BUY else 1)
        return [
            OrderRequest(
                instrument_key=leg.instrument_key,
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
                order_type=OrderType.MARKET,
                price=leg.entry_price,
                product=product,
                tag=tag,
                strategy=self.strategy,
                leg=leg.role,
            )
            for leg in ordered
        ]


@dataclass
class Trade:
    """An open or closed position built from a plan."""

    trade_id: str
    track: str
    strategy: str
    description: str
    legs: list[Leg]
    direction: int
    opened_at: datetime
    entry_price: float
    stop_loss: float = 0.0
    target: float = 0.0
    lots: int = 1
    lot_size: int = 75
    expiry: str = ""
    closed_at: datetime | None = None
    exit_price: float = 0.0
    exit_reason: str = ""
    charges: float = 0.0
    mode: str = "paper"
    peak_price: float = 0.0

    @classmethod
    def from_plan(
        cls,
        plan: TradePlan,
        now: datetime,
        entry_price: float,
        lot_size: int,
        mode: str,
        expiry: str = "",
    ) -> "Trade":
        return cls(
            trade_id=f"{plan.track.replace(' ', '')}-{uuid.uuid4().hex[:8]}",
            track=plan.track,
            strategy=plan.strategy,
            description=plan.description,
            legs=plan.legs,
            direction=plan.direction,
            opened_at=now,
            entry_price=entry_price,
            stop_loss=plan.stop_loss,
            target=plan.target,
            lots=plan.lots,
            lot_size=lot_size,
            expiry=expiry,
            mode=mode,
            peak_price=entry_price,
        )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    def current_price(self, ctx: MarketContext) -> float:
        """Mark the whole structure at the current market."""
        total = 0.0
        for leg in self.legs:
            price = ctx.price_of(leg.instrument_key, leg.entry_price)
            total += leg.side.sign * price
        return abs(round(total, 2))

    def net_points(self, exit_price: float | None = None) -> float:
        """Points gained: exit-entry for a debit, entry-exit for a credit."""
        exit_price = self.exit_price if exit_price is None else exit_price
        if self.direction > 0:
            return round(exit_price - self.entry_price, 2)
        return round(self.entry_price - exit_price, 2)

    def gross_pnl(self, exit_price: float | None = None) -> float:
        return round(self.net_points(exit_price) * self.quantity, 2)

    def realized_pnl(self) -> float:
        return round(self.gross_pnl() - self.charges, 2)

    def unrealized_pnl(self, ctx: MarketContext) -> float:
        return round(self.gross_pnl(self.current_price(ctx)) - self.charges, 2)

    def exit_orders(
        self,
        product: str,
        tag: str = "square-off",
        prices: dict[str, float] | None = None,
    ) -> list[OrderRequest]:
        """Reverse every leg; shorts are covered first to stay hedged.

        `prices` carries the marks the exit decision was taken on, so the paper
        broker simulates against the same market the strategy saw. Live orders
        go out as MARKET and ignore the reference price.
        """
        prices = prices or {}
        ordered = sorted(self.legs, key=lambda leg: 0 if leg.side is Side.SELL else 1)
        return [
            OrderRequest(
                instrument_key=leg.instrument_key,
                symbol=leg.symbol,
                side=leg.side.opposite,
                quantity=leg.quantity,
                order_type=OrderType.MARKET,
                price=prices.get(leg.instrument_key, leg.entry_price),
                product=product,
                tag=tag,
                strategy=self.strategy,
                leg=f"exit-{leg.role}",
            )
            for leg in ordered
        ]


class Strategy(ABC):
    track: str = ""
    name: str = ""

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def evaluate_entry(self, ctx: MarketContext, open_trades: Sequence[Trade]) -> TradePlan | None:
        """Return a plan when the entry conditions are met, else None."""

    @abstractmethod
    def evaluate_exit(self, trade: Trade, ctx: MarketContext) -> str | None:
        """Return a human-readable exit reason when the trade should close."""

    # -- helpers -------------------------------------------------------- #
    @staticmethod
    def parse_time(value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)

    def in_window(self, now: datetime, window: tuple[str, str]) -> bool:
        start, end = (self.parse_time(v) for v in window)
        return start <= now.time() <= end
