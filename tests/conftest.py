"""Shared fixtures: a synthetic Upstox client so the whole engine can be
exercised offline, with no network and no credentials.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from nifty_options.config import Config


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config.load()
    cfg.journal_dir = tmp_path / "journal"
    cfg.state_dir = tmp_path / "state"
    cfg.risk.kill_switch_file = tmp_path / "KILL_SWITCH"
    cfg.upstox.token_path = tmp_path / "token.json"
    cfg.ensure_dirs()
    return cfg


def build_candles(
    count: int = 120,
    start: float = 24_000.0,
    breakout: bool = False,
    interval_minutes: int = 5,
) -> list[list]:
    """Upstox-shaped candles: a coil around a level, optionally breaking out.

    Returned newest-first, exactly like the API, so the client wrapper's
    reversal is exercised too.
    """
    base = datetime(2026, 8, 24, 9, 15)
    rows = []
    price = start
    for i in range(count):
        if breakout and i >= count - 3:
            price += 45.0                       # sharp expansion candles
            high, low = price + 12, price - 40
            open_price = price - 42
        else:
            price = start + math.sin(i / 3.0) * 8.0     # tight chop
            open_price = price - 1.5
            high, low = price + 4, price - 4
        rows.append(
            [
                (base + timedelta(minutes=interval_minutes * i)).isoformat(),
                round(open_price, 2),
                round(high, 2),
                round(low, 2),
                round(price, 2),
                100_000 + i * 10,
                0,
            ]
        )
    return list(reversed(rows))


def this_weeks_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today - timedelta(days=today.weekday())


def this_weeks_expiry(today: date | None = None) -> str:
    """Thursday of the current week -- the usual Nifty weekly expiry."""
    return (this_weeks_monday(today) + timedelta(days=3)).isoformat()


def build_chain(spot: float = 24_000.0, expiry: str = "2026-08-27") -> list[dict]:
    """Option chain with plausible premiums and a monotonic delta ladder."""
    rows = []
    for offset in range(-1000, 1050, 50):
        strike = spot + offset
        moneyness = offset / 400.0
        call_delta = round(1 / (1 + math.exp(moneyness * 2.2)), 4)
        put_delta = round(-(1 - call_delta), 4)
        call_ltp = round(max(spot - strike, 0) + 90 * math.exp(-abs(moneyness) ** 2), 2)
        put_ltp = round(max(strike - spot, 0) + 90 * math.exp(-abs(moneyness) ** 2), 2)
        rows.append(
            {
                "expiry": expiry,
                "strike_price": strike,
                "underlying_spot_price": spot,
                "call_options": _leg(f"NSE_FO|C{int(strike)}", call_ltp, call_delta),
                "put_options": _leg(f"NSE_FO|P{int(strike)}", put_ltp, put_delta),
            }
        )
    return rows


def _leg(instrument_key: str, ltp: float, delta: float) -> dict:
    ltp = max(ltp, 0.5)
    return {
        "instrument_key": instrument_key,
        "market_data": {
            "ltp": ltp,
            "bid_price": round(ltp - 0.5, 2),
            "ask_price": round(ltp + 0.5, 2),
            "oi": 250_000,
            "volume": 90_000,
        },
        "option_greeks": {"delta": delta, "theta": -8.5, "vega": 12.0, "gamma": 0.0004, "iv": 13.5},
    }


class FakeUpstoxClient:
    """Stands in for UpstoxClient; records every order it is asked to place."""

    def __init__(
        self,
        spot: float = 24_000.0,
        breakout: bool = False,
        expiry: str | None = None,
    ):
        self.spot = spot
        self.breakout = breakout
        self.expiry = expiry or this_weeks_expiry()
        self.chain = build_chain(spot, expiry)
        self.placed: list[dict] = []
        self.prices: dict[str, float] = {}
        for row in self.chain:
            for key in ("call_options", "put_options"):
                leg = row[key]
                self.prices[leg["instrument_key"]] = leg["market_data"]["ltp"]

    # -- market data ---------------------------------------------------- #
    def get_ltp(self, instrument_keys):
        keys = [instrument_keys] if isinstance(instrument_keys, str) else list(instrument_keys)
        out = {}
        for key in keys:
            out[key] = self.spot if "INDEX" in key else self.prices.get(key, 0.0)
        return out

    def set_price(self, instrument_key: str, price: float) -> None:
        self.prices[instrument_key] = price

    def get_intraday_candles(self, instrument_key, unit="minutes", interval=5):
        rows = build_candles(breakout=self.breakout, interval_minutes=interval)
        return list(reversed(rows))          # mirror UpstoxClient's reversal

    def get_option_chain(self, instrument_key, expiry_date):
        return self.chain

    def get_option_contracts(self, instrument_key, expiry_date=None):
        return [{"expiry": self.expiry}]

    def get_charges_margin(self, instruments):
        return {"required_margin": 58_000.0}

    # -- orders --------------------------------------------------------- #
    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order_ids": [f"LIVE-{len(self.placed)}"]}

    def place_multi_order(self, orders):
        self.placed.extend(orders)
        return {"payload": [{"order_id": f"LIVE-{i}"} for i, _ in enumerate(orders)]}

    def get_order_details(self, order_id):
        return {"status": "complete", "filled_quantity": 75, "average_price": 100.0}

    def cancel_order(self, order_id):
        return {"order_id": order_id}

    def get_positions(self):
        return []

    def available_margin(self):
        return 400_000.0


@pytest.fixture
def fake_client():
    return FakeUpstoxClient()
