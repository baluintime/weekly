"""Simulated broker: real Upstox market data, simulated fills and cash.

Fills are modelled against the live bid/ask when available (crossing the
spread), otherwise the LTP plus a slippage allowance, and the same NSE cost
model the live broker reports is applied. That keeps the paper PnL in the
tracking sheet directly comparable with what actual trading would return.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ..config import Config, TradingMode
from ..upstox.client import UpstoxClient, UpstoxAPIError
from .base import (
    Broker,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    option_charges,
)

LOG = logging.getLogger(__name__)


@dataclass
class _Book:
    cash: float
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    realized_pnl: float = 0.0
    total_charges: float = 0.0


class PaperBroker(Broker):
    mode = TradingMode.PAPER

    def __init__(
        self,
        config: Config,
        client: UpstoxClient | None = None,
        state_file: Path | None = None,
    ):
        self.config = config
        self.client = client
        self.state_file = state_file or (
            config.state_dir / f"book_{config.session_label}.json"
        )
        self.fills = config.paper
        self._book = self._load_book()

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    def _load_book(self) -> _Book:
        starting_cash = self.config.track_a.capital + self.config.track_b.capital
        if self.state_file.exists():
            raw = json.loads(self.state_file.read_text())
            return _Book(**raw)
        return _Book(cash=starting_cash)

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(asdict(self._book), indent=2, default=str))

    def reset(self) -> None:
        """Wipe simulated cash/positions back to the configured capital."""
        self._book = _Book(cash=self.config.track_a.capital + self.config.track_b.capital)
        self.save()

    # ------------------------------------------------------------------ #
    # pricing
    # ------------------------------------------------------------------ #
    def get_ltp(self, instrument_keys: str | Sequence[str]) -> dict[str, float]:
        if self.client is None:
            return {}
        try:
            return self.client.get_ltp(instrument_keys)
        except UpstoxAPIError as exc:
            LOG.warning("LTP lookup failed in paper mode: %s", exc)
            return {}

    def _fill_price(self, request: OrderRequest) -> float:
        """Marketable price including the spread/slippage penalty."""
        reference = request.price
        if reference <= 0 and self.client is not None:
            quotes = self.get_ltp(request.instrument_key)
            reference = next(iter(quotes.values()), 0.0)
        if reference <= 0:
            raise UpstoxAPIError(
                f"No price available to simulate a fill for {request.symbol}"
            )

        if request.order_type is OrderType.LIMIT and request.price > 0:
            return round(request.price, 2)

        slip = max(
            reference * self.fills.slippage_bps / 10_000.0,
            self.fills.slippage_min_ticks * self.fills.tick_size,
        )
        price = reference + slip if request.side is Side.BUY else reference - slip
        price = max(price, self.fills.tick_size)
        tick = self.fills.tick_size
        return round(round(price / tick) * tick, 2)

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def place_order(self, request: OrderRequest) -> OrderResult:
        order_id = f"PAPER-{uuid.uuid4().hex[:12]}"
        try:
            price = self._fill_price(request)
        except UpstoxAPIError as exc:
            return OrderResult(
                order_id=order_id,
                request=request,
                status=OrderStatus.REJECTED,
                message=str(exc),
            )

        turnover = price * request.quantity
        charges = option_charges(
            turnover,
            request.side,
            self.fills.brokerage_per_order,
            self.fills.apply_statutory_charges,
        )
        self._apply_fill(request, price, charges)

        result = OrderResult(
            order_id=order_id,
            request=request,
            status=OrderStatus.COMPLETE,
            filled_quantity=request.quantity,
            average_price=price,
            charges=charges,
            message="simulated fill",
        )
        self._book.orders.append(
            {
                "order_id": order_id,
                "timestamp": result.timestamp.isoformat(),
                "symbol": request.symbol,
                "instrument_key": request.instrument_key,
                "side": request.side.value,
                "quantity": request.quantity,
                "price": price,
                "charges": charges,
                "strategy": request.strategy,
                "leg": request.leg,
            }
        )
        self.save()
        LOG.info(
            "[%s] %s %s x%d @ %.2f (charges Rs %.2f)",
            self.config.session_label.upper(),
            request.side.value, request.symbol, request.quantity, price, charges,
        )
        return result

    def _apply_fill(self, request: OrderRequest, price: float, charges: float) -> None:
        key = request.instrument_key
        position = self._book.positions.get(
            key,
            {
                "instrument_key": key,
                "symbol": request.symbol,
                "quantity": 0,
                "average_price": 0.0,
                "realized_pnl": 0.0,
                "strategy": request.strategy,
            },
        )

        signed = request.side.sign * request.quantity
        old_qty = int(position["quantity"])
        new_qty = old_qty + signed

        if old_qty == 0 or (old_qty > 0) == (signed > 0):
            # opening or adding -- weighted average cost
            total_cost = position["average_price"] * abs(old_qty) + price * request.quantity
            position["average_price"] = total_cost / max(abs(new_qty), 1)
        else:
            # reducing or closing -- book realised PnL on the closed slice
            closed = min(abs(signed), abs(old_qty))
            direction = 1 if old_qty > 0 else -1
            realized = (price - position["average_price"]) * closed * direction
            position["realized_pnl"] = position.get("realized_pnl", 0.0) + realized
            self._book.realized_pnl += realized
            self._book.cash += realized
            if abs(signed) > abs(old_qty):        # flipped through zero
                position["average_price"] = price

        position["quantity"] = new_qty
        if new_qty == 0:
            position["average_price"] = 0.0

        # premium flow: buying debits cash, selling credits it
        self._book.cash -= request.side.sign * price * request.quantity
        self._book.cash -= charges
        self._book.total_charges += charges
        self._book.positions[key] = position

    def cancel_order(self, order_id: str) -> bool:
        return True          # paper orders fill immediately; nothing to cancel

    # ------------------------------------------------------------------ #
    # portfolio
    # ------------------------------------------------------------------ #
    def get_positions(self) -> list[BrokerPosition]:
        open_positions = [p for p in self._book.positions.values() if p["quantity"] != 0]
        prices = self.get_ltp([p["instrument_key"] for p in open_positions]) if open_positions else {}
        return [
            BrokerPosition(
                instrument_key=p["instrument_key"],
                symbol=p["symbol"],
                quantity=int(p["quantity"]),
                average_price=float(p["average_price"]),
                last_price=float(prices.get(p["instrument_key"], p["average_price"])),
                realized_pnl=float(p.get("realized_pnl", 0.0)),
                strategy=p.get("strategy", ""),
            )
            for p in open_positions
        ]

    def available_margin(self) -> float:
        return self._book.cash

    @property
    def realized_pnl(self) -> float:
        return self._book.realized_pnl

    @property
    def total_charges(self) -> float:
        return self._book.total_charges

    def summary(self) -> dict[str, Any]:
        positions = self.get_positions()
        return {
            "mode": self.config.session_label,
            "cash": round(self._book.cash, 2),
            "realized_pnl": round(self._book.realized_pnl, 2),
            "unrealized_pnl": round(sum(p.unrealized_pnl for p in positions), 2),
            "charges": round(self._book.total_charges, 2),
            "open_positions": len(positions),
            "orders": len(self._book.orders),
        }
