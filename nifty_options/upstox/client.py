"""Thin, typed wrapper over the Upstox REST API (v2 + v3 endpoints).

Only the endpoints this framework needs are wrapped. Every call goes through
:meth:`UpstoxClient._request`, which centralises auth headers, retries on
transient failures, and error unwrapping so callers see a plain dict.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence

import requests

LOG = logging.getLogger(__name__)

API_V2 = "https://api.upstox.com/v2"
API_V3 = "https://api.upstox.com/v3"
SANDBOX_V2 = "https://api-sandbox.upstox.com/v2"
SANDBOX_V3 = "https://api-sandbox.upstox.com/v3"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class UpstoxAPIError(RuntimeError):
    def __init__(self, message: str, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class UpstoxClient:
    def __init__(
        self,
        access_token: str,
        sandbox: bool = False,
        timeout: int = 20,
        max_retries: int = 3,
    ):
        self.access_token = access_token
        self.sandbox = sandbox
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Api-Version": "2.0",
                "Authorization": f"Bearer {access_token}",
            }
        )

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    @property
    def v2(self) -> str:
        return SANDBOX_V2 if self.sandbox else API_V2

    @property
    def v3(self) -> str:
        return SANDBOX_V3 if self.sandbox else API_V3

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:      # network blip
                last_error = exc
                self._backoff(attempt, f"{method} {url}: {exc}")
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries - 1:
                self._backoff(attempt, f"{method} {url} -> HTTP {response.status_code}")
                continue

            return self._unwrap(response)

        raise UpstoxAPIError(f"{method} {url} failed after retries: {last_error}")

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = 2 ** attempt
        LOG.warning("Upstox request retry in %ss (%s)", delay, reason)
        time.sleep(delay)

    @staticmethod
    def _unwrap(response: requests.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            raise UpstoxAPIError(
                f"Non-JSON response [{response.status_code}]: {response.text[:200]}",
                response.status_code,
            )

        if response.status_code >= 400 or payload.get("status") == "error":
            errors = payload.get("errors") or [{"message": response.text[:200]}]
            first = errors[0]
            raise UpstoxAPIError(
                f"[{first.get('errorCode', response.status_code)}] {first.get('message')}",
                response.status_code,
                payload,
            )
        return payload.get("data", payload)

    # ------------------------------------------------------------------ #
    # user / funds
    # ------------------------------------------------------------------ #
    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", f"{self.v2}/user/profile")

    def get_funds(self) -> dict[str, Any]:
        return self._request("GET", f"{self.v2}/user/get-funds-and-margin", params={"segment": "SEC"})

    def available_margin(self) -> float:
        funds = self.get_funds()
        block = funds.get("equity") or next(iter(funds.values()), {})
        return float(block.get("available_margin", 0.0))

    # ------------------------------------------------------------------ #
    # market data
    # ------------------------------------------------------------------ #
    def get_ltp(self, instrument_keys: str | Sequence[str]) -> dict[str, float]:
        keys = [instrument_keys] if isinstance(instrument_keys, str) else list(instrument_keys)
        data = self._request(
            "GET", f"{self.v3}/market-quote/ltp", params={"instrument_key": ",".join(keys)}
        )
        return {
            entry.get("instrument_token", name): float(entry.get("last_price", 0.0))
            for name, entry in data.items()
        }

    def get_quotes(self, instrument_keys: str | Sequence[str]) -> dict[str, Any]:
        keys = [instrument_keys] if isinstance(instrument_keys, str) else list(instrument_keys)
        return self._request(
            "GET", f"{self.v2}/market-quote/quotes", params={"instrument_key": ",".join(keys)}
        )

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list[dict[str, Any]]:
        """Full option chain with per-strike greeks -- drives strike selection."""
        return self._request(
            "GET",
            f"{self.v2}/option/chain",
            params={"instrument_key": instrument_key, "expiry_date": expiry_date},
        )

    def get_option_contracts(
        self, instrument_key: str, expiry_date: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"instrument_key": instrument_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        return self._request("GET", f"{self.v2}/option/contract", params=params)

    def get_intraday_candles(
        self, instrument_key: str, unit: str = "minutes", interval: int = 5
    ) -> list[list[Any]]:
        data = self._request(
            "GET", f"{self.v3}/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
        )
        return list(reversed(data.get("candles", [])))   # API returns newest-first

    def get_historical_candles(
        self,
        instrument_key: str,
        from_date: str,
        to_date: str,
        unit: str = "days",
        interval: int = 1,
    ) -> list[list[Any]]:
        data = self._request(
            "GET",
            f"{self.v3}/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}",
        )
        return list(reversed(data.get("candles", [])))

    def get_market_status(self, exchange: str = "NSE") -> dict[str, Any]:
        return self._request("GET", f"{self.v2}/market/status/{exchange}")

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def place_order(
        self,
        instrument_token: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
        product: str = "I",
        price: float = 0.0,
        trigger_price: float = 0.0,
        validity: str = "DAY",
        disclosed_quantity: int = 0,
        is_amo: bool = False,
        slice_order: bool = True,
        tag: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "quantity": quantity,
            "product": product,
            "validity": validity,
            "price": price,
            "tag": tag or "nifty-options",
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": disclosed_quantity,
            "trigger_price": trigger_price,
            "is_amo": is_amo,
            "slice": slice_order,
        }
        return self._request("POST", f"{self.v3}/order/place", json_body=body)

    def place_multi_order(self, orders: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Basket placement -- used to send all four condor legs together."""
        return self._request("POST", f"{self.v3}/order/multi/place", json_body=list(orders))

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"{self.v3}/order/cancel", params={"order_id": order_id})

    def get_order_details(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"{self.v2}/order/details", params={"order_id": order_id})

    def get_order_book(self) -> list[dict[str, Any]]:
        return self._request("GET", f"{self.v2}/order/retrieve-all")

    def get_trades(self) -> list[dict[str, Any]]:
        return self._request("GET", f"{self.v2}/order/trades/get-trades-for-day")

    def get_positions(self) -> list[dict[str, Any]]:
        return self._request("GET", f"{self.v2}/portfolio/short-term-positions")

    def get_charges_margin(self, instruments: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Pre-trade margin estimate for a basket (used to size condor lots)."""
        return self._request(
            "POST", f"{self.v2}/charges/margin", json_body={"instruments": list(instruments)}
        )

    def exit_all_positions(self, segment: str | None = None) -> dict[str, Any]:
        body = {"segment": segment} if segment else {}
        return self._request("POST", f"{self.v3}/order/positions/exit", json_body=body)
