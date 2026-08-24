"""Live broker: sends real, money-moving orders to Upstox.

Every order passes a pre-trade gate before it reaches the API. The gate is
deliberately independent of the config-level guards in
:meth:`Config.assert_live_allowed` -- config arms live trading once, this
rejects individual orders that look wrong at execution time.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any, Sequence

from ..config import Config, TradingMode
from ..upstox.client import UpstoxAPIError, UpstoxClient
from .base import (
    Broker,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
)

LOG = logging.getLogger(__name__)

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


class OrderRejected(RuntimeError):
    """A pre-trade guard refused to send the order."""


class LiveBroker(Broker):
    mode = TradingMode.LIVE

    def __init__(self, config: Config, client: UpstoxClient):
        self.config = config
        self.client = client
        self.guards = config.live
        self._orders_today = 0
        self._day = datetime.now().date()

    # ------------------------------------------------------------------ #
    # pre-trade gate
    # ------------------------------------------------------------------ #
    def _check(self, request: OrderRequest) -> None:
        if self.config.risk.kill_switch_file.exists():
            raise OrderRejected(
                f"Kill switch active ({self.config.risk.kill_switch_file}); "
                "no live orders will be sent."
            )

        if request.quantity <= 0:
            raise OrderRejected(f"Non-positive quantity for {request.symbol}.")

        if request.quantity % self.config.lot_size != 0:
            raise OrderRejected(
                f"{request.symbol}: quantity {request.quantity} is not a multiple of "
                f"lot size {self.config.lot_size}."
            )

        if not request.instrument_key.startswith(("NSE_FO|", "BSE_FO|")):
            raise OrderRejected(
                f"Refusing to trade non-F&O instrument {request.instrument_key!r}. "
                "This engine only trades Nifty index options."
            )

        reference = request.price
        if reference <= 0:
            reference = next(iter(self.get_ltp(request.instrument_key).values()), 0.0)
        order_value = reference * request.quantity
        if order_value > self.config.risk.live_max_order_value:
            raise OrderRejected(
                f"{request.symbol}: order value Rs {order_value:,.0f} exceeds "
                f"risk.live_max_order_value Rs {self.config.risk.live_max_order_value:,.0f}."
            )

        self._roll_day()
        if self._orders_today >= self.config.risk.live_max_daily_orders:
            raise OrderRejected(
                f"Daily live order cap reached ({self.config.risk.live_max_daily_orders})."
            )

        if self.guards.require_market_hours and not self.is_market_open():
            raise OrderRejected("Market is closed; refusing to send a live order.")

    def _roll_day(self) -> None:
        today = datetime.now().date()
        if today != self._day:
            self._day = today
            self._orders_today = 0

    @staticmethod
    def is_market_open(now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        return MARKET_OPEN <= now.time() <= MARKET_CLOSE

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def place_order(self, request: OrderRequest) -> OrderResult:
        try:
            self._check(request)
        except OrderRejected as exc:
            LOG.error("[LIVE] blocked: %s", exc)
            return OrderResult(
                order_id="", request=request, status=OrderStatus.REJECTED, message=str(exc)
            )

        if self.guards.dry_run:
            LOG.warning(
                "[LIVE dry-run] would send %s %s x%d %s",
                request.side.value, request.symbol, request.quantity,
                request.order_type.value,
            )
            return OrderResult(
                order_id="DRYRUN",
                request=request,
                status=OrderStatus.PENDING,
                message="live.dry_run enabled -- order not sent",
            )

        LOG.warning(
            "[LIVE] sending REAL order: %s %s x%d %s @ %.2f",
            request.side.value, request.symbol, request.quantity,
            request.order_type.value, request.price,
        )
        try:
            data = self.client.place_order(
                instrument_token=request.instrument_key,
                quantity=request.quantity,
                transaction_type=request.side.value,
                order_type=request.order_type.value,
                product=request.product,
                price=request.price if request.order_type is OrderType.LIMIT else 0.0,
                trigger_price=request.trigger_price,
                tag=request.tag,
            )
        except UpstoxAPIError as exc:
            LOG.error("[LIVE] order failed for %s: %s", request.symbol, exc)
            return OrderResult(
                order_id="", request=request, status=OrderStatus.REJECTED, message=str(exc)
            )

        self._orders_today += 1
        order_ids = data.get("order_ids") or [data.get("order_id", "")]
        order_id = order_ids[0] if order_ids else ""
        return self._resolve(order_id, request, data)

    def _resolve(
        self, order_id: str, request: OrderRequest, raw: dict[str, Any]
    ) -> OrderResult:
        """Read back the order to learn the actual fill price."""
        status, filled, average, message = OrderStatus.PENDING, 0, 0.0, ""
        if order_id:
            try:
                details = self.client.get_order_details(order_id)
                api_status = str(details.get("status", "")).lower()
                filled = int(details.get("filled_quantity", 0) or 0)
                average = float(details.get("average_price", 0.0) or 0.0)
                message = details.get("status_message", "") or ""
                if api_status in ("complete", "filled"):
                    status = OrderStatus.COMPLETE
                elif api_status in ("rejected", "cancelled"):
                    status = OrderStatus.REJECTED if "reject" in api_status else OrderStatus.CANCELLED
            except UpstoxAPIError as exc:
                message = f"placed but status unavailable: {exc}"

        return OrderResult(
            order_id=order_id,
            request=request,
            status=status,
            filled_quantity=filled,
            average_price=average,
            message=message,
            raw=raw,
        )

    def place_basket(self, requests: Sequence[OrderRequest]) -> list[OrderResult]:
        """Send all legs in one multi-order call so a condor goes on atomically."""
        for request in requests:
            try:
                self._check(request)
            except OrderRejected as exc:
                LOG.error("[LIVE] basket blocked: %s", exc)
                return [
                    OrderResult(
                        order_id="", request=r, status=OrderStatus.REJECTED, message=str(exc)
                    )
                    for r in requests
                ]

        if self.guards.dry_run:
            return [
                OrderResult(
                    order_id="DRYRUN", request=r, status=OrderStatus.PENDING,
                    message="live.dry_run enabled -- basket not sent",
                )
                for r in requests
            ]

        payload = [
            {
                "quantity": r.quantity,
                "product": r.product,
                "validity": "DAY",
                "price": r.price if r.order_type is OrderType.LIMIT else 0.0,
                "tag": r.tag,
                "instrument_token": r.instrument_key,
                "order_type": r.order_type.value,
                "transaction_type": r.side.value,
                "disclosed_quantity": 0,
                "trigger_price": r.trigger_price,
                "is_amo": False,
                "slice": True,
                "correlation_id": f"{r.strategy}-{r.leg}"[:20],
            }
            for r in requests
        ]
        LOG.warning("[LIVE] sending REAL basket of %d legs", len(payload))
        try:
            data = self.client.place_multi_order(payload)
        except UpstoxAPIError as exc:
            LOG.error("[LIVE] basket failed: %s", exc)
            return [
                OrderResult(order_id="", request=r, status=OrderStatus.REJECTED, message=str(exc))
                for r in requests
            ]

        self._orders_today += len(payload)
        summary = data.get("payload") if isinstance(data, dict) else None
        order_ids = [
            entry.get("order_id", "")
            for entry in (summary or [])
            if isinstance(entry, dict)
        ]
        results = []
        for index, request in enumerate(requests):
            order_id = order_ids[index] if index < len(order_ids) else ""
            results.append(self._resolve(order_id, request, data if isinstance(data, dict) else {}))
        return results

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order(order_id)
            return True
        except UpstoxAPIError as exc:
            LOG.error("[LIVE] cancel failed for %s: %s", order_id, exc)
            return False

    # ------------------------------------------------------------------ #
    # portfolio
    # ------------------------------------------------------------------ #
    def get_positions(self) -> list[BrokerPosition]:
        positions = []
        for row in self.client.get_positions():
            quantity = int(row.get("quantity", 0) or 0)
            if quantity == 0:
                continue
            positions.append(
                BrokerPosition(
                    instrument_key=row.get("instrument_token", ""),
                    symbol=row.get("tradingsymbol", ""),
                    quantity=quantity,
                    average_price=float(row.get("average_price", 0.0) or 0.0),
                    last_price=float(row.get("last_price", 0.0) or 0.0),
                    realized_pnl=float(row.get("realised", 0.0) or 0.0),
                )
            )
        return positions

    def get_ltp(self, instrument_keys: str | Sequence[str]) -> dict[str, float]:
        try:
            return self.client.get_ltp(instrument_keys)
        except UpstoxAPIError as exc:
            LOG.warning("[LIVE] LTP lookup failed: %s", exc)
            return {}

    def available_margin(self) -> float:
        try:
            return self.client.available_margin()
        except UpstoxAPIError as exc:
            LOG.warning("[LIVE] funds lookup failed: %s", exc)
            return 0.0

    def summary(self) -> dict[str, Any]:
        positions = self.get_positions()
        return {
            "mode": self.mode.value,
            "dry_run": self.guards.dry_run,
            "available_margin": round(self.available_margin(), 2),
            "open_positions": len(positions),
            "unrealized_pnl": round(sum(p.unrealized_pnl for p in positions), 2),
            "realized_pnl": round(sum(p.realized_pnl for p in positions), 2),
            "orders_today": self._orders_today,
        }
