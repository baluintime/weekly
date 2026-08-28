"""Live contract specification, fetched from the exchange rather than assumed.

Lot size, tick size, freeze quantity, strike interval and the expiry calendar
are all NSE-controlled and all of them have changed before -- the Nifty lot has
been 75, 50, 25 and 65, and the weekly expiry has moved between Thursday and
Tuesday. Nothing here is hardcoded: every value comes from Upstox's
`/v2/option/contract` response for the underlying being traded.

The configured values in config.yaml are fallbacks, used only when the API is
unreachable, and using one is logged loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from .client import UpstoxAPIError, UpstoxClient

LOG = logging.getLogger(__name__)

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


@dataclass(frozen=True)
class ExpiryInfo:
    """One listed expiry, as the exchange currently has it."""

    expiry: str
    lot_size: int
    weekly: bool = True
    contracts: int = 0

    @property
    def as_date(self) -> date:
        return date.fromisoformat(self.expiry)

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.as_date.weekday()]

    def days_to_expiry(self, today: date | None = None) -> int:
        return (self.as_date - (today or date.today())).days


@dataclass(frozen=True)
class ContractSpec:
    """Everything about the tradable contract that the exchange decides."""

    underlying: str
    expiry: str
    lot_size: int
    tick_size: float
    freeze_quantity: int
    strike_interval: float
    weekly: bool
    source: str                      # "exchange" or "config fallback"
    expiries: tuple[ExpiryInfo, ...] = field(default_factory=tuple)
    fetched_on: str = ""

    @property
    def expiry_weekday(self) -> str:
        if not self.expiry:
            return "?"
        return WEEKDAYS[date.fromisoformat(self.expiry).weekday()]

    @property
    def from_exchange(self) -> bool:
        return self.source == "exchange"

    def days_to_expiry(self, today: date | None = None) -> int:
        if not self.expiry:
            return -1
        return (date.fromisoformat(self.expiry) - (today or date.today())).days

    def max_lots_per_order(self) -> int:
        """Freeze quantity caps a single order; larger sizes must be sliced."""
        if self.freeze_quantity <= 0 or self.lot_size <= 0:
            return 0
        return int(self.freeze_quantity // self.lot_size)

    def describe(self) -> str:
        return (
            f"{self.underlying} {self.expiry or 'no expiry'} ({self.expiry_weekday}) | "
            f"lot {self.lot_size} | tick {self.tick_size} | "
            f"strikes every {self.strike_interval:g} | "
            f"freeze {self.freeze_quantity} | {self.source}"
        )


def normalise_tick_size(raw: float) -> float:
    """Upstox reports F&O tick size in paise (5.0 meaning Rs 0.05).

    Index-option ticks are 0.05, never 5.00, so a value of 1 or more is read as
    paise. Getting this wrong would place limit orders at invalid prices.
    """
    value = float(raw or 0)
    if value <= 0:
        return 0.05
    return round(value / 100.0, 4) if value >= 1 else round(value, 4)


def strike_interval(strikes: Sequence[float]) -> float:
    """Smallest positive gap between adjacent listed strikes."""
    unique = sorted({float(s) for s in strikes if s})
    gaps = [
        round(b - a, 4) for a, b in zip(unique, unique[1:]) if b - a > 0
    ]
    if not gaps:
        return 50.0
    # The modal gap, so a sparse far-OTM tail cannot skew it.
    return max(set(gaps), key=lambda gap: (gaps.count(gap), -gap))


def parse_contracts(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a `/option/contract` payload to the facts we trade on."""
    by_expiry: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        expiry = row.get("expiry")
        if expiry:
            by_expiry.setdefault(expiry, []).append(row)

    expiries = []
    for expiry, contracts in sorted(by_expiry.items()):
        lot_sizes = [int(c.get("lot_size", 0) or 0) for c in contracts]
        lot_sizes = [size for size in lot_sizes if size > 0]
        expiries.append(
            ExpiryInfo(
                expiry=expiry,
                lot_size=max(set(lot_sizes), key=lot_sizes.count) if lot_sizes else 0,
                weekly=bool(contracts[0].get("weekly", True)),
                contracts=len(contracts),
            )
        )
    return {"by_expiry": by_expiry, "expiries": expiries}


def build_spec(
    rows: Sequence[dict[str, Any]],
    expiry: str,
    underlying: str,
    expiries: Sequence[ExpiryInfo] = (),
    today: date | None = None,
) -> ContractSpec | None:
    """Assemble the spec for one expiry from its contract rows."""
    contracts = [r for r in rows if r.get("expiry") == expiry]
    if not contracts:
        return None

    lot_sizes = [int(c.get("lot_size", 0) or 0) for c in contracts if c.get("lot_size")]
    ticks = [normalise_tick_size(c.get("tick_size", 0)) for c in contracts]
    freezes = [int(float(c.get("freeze_quantity", 0) or 0)) for c in contracts]
    strikes = [float(c.get("strike_price", 0) or 0) for c in contracts]

    if not lot_sizes:
        return None

    return ContractSpec(
        underlying=underlying,
        expiry=expiry,
        lot_size=max(set(lot_sizes), key=lot_sizes.count),
        tick_size=max(set(ticks), key=ticks.count) if ticks else 0.05,
        freeze_quantity=max(freezes) if freezes else 0,
        strike_interval=strike_interval(strikes),
        weekly=bool(contracts[0].get("weekly", True)),
        source="exchange",
        expiries=tuple(expiries),
        fetched_on=(today or date.today()).isoformat(),
    )


def fetch_spec(
    client: UpstoxClient,
    instrument_key: str,
    underlying: str,
    expiry: str | None = None,
    today: date | None = None,
) -> tuple[ContractSpec | None, list[ExpiryInfo]]:
    """Pull the whole contract master for the underlying, live.

    Returns the spec for `expiry` (or the nearest listed one) plus the full
    expiry calendar, so callers can see what the exchange is actually listing
    rather than assuming a weekday.
    """
    try:
        rows = client.get_option_contracts(instrument_key)
    except UpstoxAPIError as exc:
        LOG.error("Could not fetch option contracts for %s: %s", instrument_key, exc)
        return None, []

    parsed = parse_contracts(rows)
    expiries: list[ExpiryInfo] = parsed["expiries"]
    if not expiries:
        LOG.error("Upstox returned no option contracts for %s", instrument_key)
        return None, []

    today = today or date.today()
    upcoming = [e for e in expiries if e.days_to_expiry(today) >= 0]
    chosen = expiry or (upcoming[0].expiry if upcoming else expiries[0].expiry)

    spec = build_spec(rows, chosen, underlying, expiries, today)
    if spec is None:
        LOG.error("No contracts found for %s expiry %s", instrument_key, chosen)
    return spec, expiries


def fallback_spec(
    underlying: str, expiry: str, lot_size: int, tick_size: float
) -> ContractSpec:
    """Last resort when the contract master cannot be read."""
    LOG.warning(
        "Using CONFIGURED lot size %d and tick %.2f for %s -- these are fallbacks, "
        "not exchange data. Verify them before trading live.",
        lot_size, tick_size, underlying,
    )
    return ContractSpec(
        underlying=underlying,
        expiry=expiry,
        lot_size=lot_size,
        tick_size=tick_size,
        freeze_quantity=0,
        strike_interval=50.0,
        weekly=True,
        source="config fallback",
    )


def expiry_in_window(
    expiries: Sequence[ExpiryInfo],
    today: date,
    min_days: int,
    max_days: int,
    weekly_only: bool = True,
) -> ExpiryInfo | None:
    """The furthest expiry inside [min_days, max_days] -- the first chance to
    put on a positional structure with the intended holding period."""
    candidates = [
        e for e in expiries
        if (e.weekly or not weekly_only)
        and min_days <= e.days_to_expiry(today) <= max_days
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.days_to_expiry(today))
