"""Expiry discovery and delta-based strike selection from the Upstox option chain.

Both tracks pick strikes by delta, exactly as the framework specifies:
Track A wants |delta| in [0.50, 0.60] (slightly ITM/ATM); Track B wants short
legs near |delta| 0.15 with a fixed 100-point protective wing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Literal, Sequence

from .client import UpstoxClient

LOG = logging.getLogger(__name__)

OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class OptionQuote:
    """One side (CE or PE) of a strike, flattened out of the chain response."""

    instrument_key: str
    strike: float
    option_type: OptionType
    expiry: str
    ltp: float
    bid: float
    ask: float
    delta: float
    theta: float
    vega: float
    gamma: float
    iv: float
    oi: float
    volume: float

    @property
    def abs_delta(self) -> float:
        return abs(self.delta)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 2)
        return self.ltp

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if mid <= 0 or self.bid <= 0 or self.ask <= 0:
            return 0.0
        return (self.ask - self.bid) / mid * 100.0

    @property
    def symbol(self) -> str:
        return f"NIFTY {int(self.strike)} {self.option_type}"


def parse_chain(chain: Sequence[dict[str, Any]]) -> list[OptionQuote]:
    """Flatten Upstox's `/option/chain` payload into OptionQuote rows."""
    quotes: list[OptionQuote] = []
    for row in chain:
        strike = float(row.get("strike_price", 0.0))
        expiry = row.get("expiry", "")
        for option_type, key in (("CE", "call_options"), ("PE", "put_options")):
            leg = row.get(key) or {}
            market = leg.get("market_data") or {}
            greeks = leg.get("option_greeks") or {}
            instrument_key = leg.get("instrument_key", "")
            if not instrument_key:
                continue
            quotes.append(
                OptionQuote(
                    instrument_key=instrument_key,
                    strike=strike,
                    option_type=option_type,  # type: ignore[arg-type]
                    expiry=expiry,
                    ltp=float(market.get("ltp", 0.0) or 0.0),
                    bid=float(market.get("bid_price", 0.0) or 0.0),
                    ask=float(market.get("ask_price", 0.0) or 0.0),
                    delta=float(greeks.get("delta", 0.0) or 0.0),
                    theta=float(greeks.get("theta", 0.0) or 0.0),
                    vega=float(greeks.get("vega", 0.0) or 0.0),
                    gamma=float(greeks.get("gamma", 0.0) or 0.0),
                    iv=float(greeks.get("iv", 0.0) or 0.0),
                    oi=float(market.get("oi", 0.0) or 0.0),
                    volume=float(market.get("volume", 0.0) or 0.0),
                )
            )
    return quotes


def select_by_delta(
    quotes: Iterable[OptionQuote],
    option_type: OptionType,
    target_delta: float,
    tolerance: float = 0.05,
    min_ltp: float = 0.5,
) -> OptionQuote | None:
    """Closest strike to `target_delta`, preferring anything inside tolerance."""
    candidates = [
        q for q in quotes
        if q.option_type == option_type and q.ltp >= min_ltp and q.abs_delta > 0
    ]
    if not candidates:
        return None
    within = [q for q in candidates if abs(q.abs_delta - target_delta) <= tolerance]
    pool = within or candidates
    return min(pool, key=lambda q: abs(q.abs_delta - target_delta))


def select_delta_band(
    quotes: Iterable[OptionQuote],
    option_type: OptionType,
    min_delta: float,
    max_delta: float,
    max_premium: float | None = None,
) -> OptionQuote | None:
    """Track A: cheapest liquid contract whose |delta| sits inside the band."""
    band = [
        q for q in quotes
        if q.option_type == option_type and min_delta <= q.abs_delta <= max_delta and q.ltp > 0
    ]
    if max_premium is not None:
        affordable = [q for q in band if q.ltp <= max_premium]
        band = affordable or band
    if not band:
        return select_by_delta(quotes, option_type, (min_delta + max_delta) / 2, tolerance=0.10)
    # Prefer the tightest spread, then the strike closest to the band's floor
    # (a lower delta costs less premium for the same directional exposure).
    return min(band, key=lambda q: (round(q.spread_pct, 1), q.abs_delta))


def find_strike(
    quotes: Iterable[OptionQuote], strike: float, option_type: OptionType
) -> OptionQuote | None:
    for quote in quotes:
        if quote.option_type == option_type and abs(quote.strike - strike) < 1e-6:
            return quote
    return None


def atm_strike(quotes: Sequence[OptionQuote], spot: float, step: int = 50) -> float:
    """Nearest listed strike to spot (falls back to rounding when chain is empty)."""
    if not quotes:
        return round(spot / step) * step
    return min({q.strike for q in quotes}, key=lambda s: abs(s - spot))


# ---------------------------------------------------------------------- #
# expiries
# ---------------------------------------------------------------------- #
def list_expiries(client: UpstoxClient, instrument_key: str) -> list[str]:
    contracts = client.get_option_contracts(instrument_key)
    return sorted({c.get("expiry", "") for c in contracts if c.get("expiry")})


def nearest_weekly_expiry(
    expiries: Sequence[str],
    today: date | None = None,
    min_days: int = 0,
    max_days: int = 7,
) -> str | None:
    """First expiry at least `min_days` out, within `max_days`.

    Track B wants 2-5 days to expiry, so on a Monday the current week's expiry
    qualifies; late in the week the selection rolls to the next one.
    """
    today = today or date.today()
    dated = []
    for value in expiries:
        try:
            dated.append((date.fromisoformat(value), value))
        except ValueError:
            continue
    for expiry_date, value in sorted(dated):
        days = (expiry_date - today).days
        if min_days <= days <= max_days:
            return value
    upcoming = [(d, v) for d, v in sorted(dated) if d >= today]
    return upcoming[0][1] if upcoming else None


def days_to_expiry(expiry: str, today: date | None = None) -> int:
    return (date.fromisoformat(expiry) - (today or date.today())).days


def fetch_chain(
    client: UpstoxClient, instrument_key: str, expiry: str
) -> list[OptionQuote]:
    quotes = parse_chain(client.get_option_chain(instrument_key, expiry))
    LOG.debug("Fetched %d option quotes for %s %s", len(quotes), instrument_key, expiry)
    return quotes
