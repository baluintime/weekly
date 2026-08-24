"""Broker interface shared by the paper and live implementations.

The engine is written against this interface only. Switching between
simulated and actual trading therefore swaps one object -- no strategy or
risk code changes -- which is what makes the mode switch safe to flip.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Sequence

from ..config import TradingMode


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class OrderStatus(str, Enum):
    PENDING = "pending"
    COMPLETE = "complete"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class OrderRequest:
    instrument_key: str
    symbol: str
    side: Side
    quantity: int                       # in units, i.e. lots * lot_size
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0
    trigger_price: float = 0.0
    product: str = "I"
    tag: str = "nifty-options"
    strategy: str = ""
    leg: str = ""

    @property
    def notional(self) -> float:
        return abs(self.price * self.quantity)


@dataclass
class OrderResult:
    order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_quantity: int = 0
    average_price: float = 0.0
    charges: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.COMPLETE and self.filled_quantity > 0

    @property
    def value(self) -> float:
        return self.average_price * self.filled_quantity


@dataclass
class BrokerPosition:
    instrument_key: str
    symbol: str
    quantity: int                       # signed: +long, -short
    average_price: float
    last_price: float = 0.0
    realized_pnl: float = 0.0
    strategy: str = ""

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.average_price) * self.quantity

    @property
    def is_open(self) -> bool:
        return self.quantity != 0


class Broker(ABC):
    """Everything the engine is allowed to ask a broker to do."""

    mode: TradingMode

    @property
    def is_live(self) -> bool:
        return self.mode.is_live

    @property
    def name(self) -> str:
        return f"{type(self).__name__}[{self.mode.value}]"

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    def place_basket(self, requests: Sequence[OrderRequest]) -> list[OrderResult]:
        """Place legs together; default is sequential placement."""
        return [self.place_order(request) for request in requests]

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def get_ltp(self, instrument_keys: str | Sequence[str]) -> dict[str, float]: ...

    @abstractmethod
    def available_margin(self) -> float: ...

    def square_off_all(self) -> list[OrderResult]:
        """Flatten every open position at market."""
        results: list[OrderResult] = []
        for position in self.get_positions():
            if not position.is_open:
                continue
            results.append(
                self.place_order(
                    OrderRequest(
                        instrument_key=position.instrument_key,
                        symbol=position.symbol,
                        side=Side.SELL if position.quantity > 0 else Side.BUY,
                        quantity=abs(position.quantity),
                        order_type=OrderType.MARKET,
                        strategy=position.strategy,
                        tag="square-off",
                    )
                )
            )
        return results


# ---------------------------------------------------------------------- #
# NSE F&O cost model -- shared so paper PnL is comparable to live PnL
# ---------------------------------------------------------------------- #
def option_charges(
    turnover: float,
    side: Side,
    brokerage_per_order: float = 20.0,
    apply_statutory: bool = True,
) -> float:
    """Approximate all-in cost of one NSE index-option order.

    Rates as applicable to NSE F&O options: brokerage (flat), STT 0.1% on the
    sell-side premium, exchange transaction charge 0.03503%, SEBI turnover fee
    Rs 10/crore, stamp duty 0.003% on buys, and 18% GST on
    (brokerage + transaction + SEBI).
    """
    brokerage = brokerage_per_order if turnover else 0.0
    if not apply_statutory:
        return round(brokerage, 2)

    stt = turnover * 0.001 if side is Side.SELL else 0.0
    transaction = turnover * 0.0003503
    sebi = turnover * 0.000001
    stamp = turnover * 0.00003 if side is Side.BUY else 0.0
    gst = (brokerage + transaction + sebi) * 0.18
    return round(brokerage + stt + transaction + sebi + stamp + gst, 2)
