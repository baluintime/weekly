"""Pure-Python implementations of the indicators named in the framework:
20 EMA, MACD (12/26/9) and the Ichimoku Cloud (9/26/52).

Everything works on plain lists so the package has no numeric dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    open_interest: float = 0.0

    @classmethod
    def from_upstox(cls, row: Sequence[Any]) -> "Candle":
        """Upstox candle: [timestamp, open, high, low, close, volume, oi]."""
        return cls(
            timestamp=datetime.fromisoformat(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
            open_interest=float(row[6]) if len(row) > 6 else 0.0,
        )


def to_candles(rows: Sequence[Sequence[Any]]) -> list[Candle]:
    return [Candle.from_upstox(row) for row in rows]


# ---------------------------------------------------------------------- #
# moving averages
# ---------------------------------------------------------------------- #
def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Standard EMA seeded with the first `period` SMA."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    multiplier = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    previous = seed
    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        out[i] = previous
    return out


# ---------------------------------------------------------------------- #
# MACD
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class MACD:
    macd: list[float | None]
    signal: list[float | None]
    histogram: list[float | None]

    def hist_at(self, index: int = -1) -> float | None:
        return self.histogram[index] if self.histogram else None

    def histogram_expanding(self, lookback: int = 2) -> bool:
        """True when the last `lookback` histogram bars are strictly growing."""
        tail = [h for h in self.histogram[-(lookback + 1):] if h is not None]
        if len(tail) < lookback + 1:
            return False
        return all(abs(tail[i]) > abs(tail[i - 1]) for i in range(1, len(tail)))

    def histogram_flattening(self, lookback: int = 2) -> bool:
        """Direction-agnostic stall: the histogram stopped growing or flipped.

        Used where only the *magnitude* of momentum matters (Track B's
        "directional move exhausts" entry).
        """
        tail = [h for h in self.histogram[-(lookback + 1):] if h is not None]
        if len(tail) < 2:
            return False
        latest, previous = tail[-1], tail[-2]
        if latest == 0.0:                    # momentum exactly neutral
            return True
        if latest * previous <= 0 and latest != previous:
            return True                      # crossed or left the zero line
        return abs(latest) <= abs(previous)

    def momentum_fading(self, direction: int, lookback: int = 1) -> bool:
        """Track A's exit trigger: momentum stopped favouring the position.

        Direction-aware, because a histogram that has already flipped against
        a long call keeps *expanding* in magnitude -- momentum is not
        "flattening" by magnitude, but the trade is dead. `direction` is +1 for
        a long call, -1 for a long put.
        """
        tail = [h for h in self.histogram[-(lookback + 1):] if h is not None]
        if len(tail) < 2:
            return False
        if direction > 0:
            return tail[-1] <= tail[-2] or tail[-1] < 0
        return tail[-1] >= tail[-2] or tail[-1] > 0


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> MACD:
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    defined = [(i, v) for i, v in enumerate(line) if v is not None]
    signal_line: list[float | None] = [None] * len(line)
    if len(defined) >= signal:
        values = [v for _, v in defined]
        smoothed = ema(values, signal)
        for (index, _), value in zip(defined, smoothed):
            signal_line[index] = value

    histogram: list[float | None] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(line, signal_line)
    ]
    return MACD(line, signal_line, histogram)


# ---------------------------------------------------------------------- #
# Ichimoku
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class Ichimoku:
    tenkan: list[float | None]
    kijun: list[float | None]
    senkou_a: list[float | None]
    senkou_b: list[float | None]
    chikou: list[float | None]

    def cloud_at(self, index: int = -1) -> tuple[float | None, float | None]:
        """(cloud_top, cloud_bottom) at `index`; (None, None) if undefined."""
        a, b = self.senkou_a[index], self.senkou_b[index]
        if a is None or b is None:
            return None, None
        return max(a, b), min(a, b)

    def price_above_cloud(self, price: float, index: int = -1) -> bool:
        top, _ = self.cloud_at(index)
        return top is not None and price > top

    def price_below_cloud(self, price: float, index: int = -1) -> bool:
        _, bottom = self.cloud_at(index)
        return bottom is not None and price < bottom

    def price_inside_cloud(self, price: float, index: int = -1) -> bool:
        top, bottom = self.cloud_at(index)
        return top is not None and bottom is not None and bottom <= price <= top


def ichimoku(
    candles: Sequence[Candle], tenkan_p: int = 9, kijun_p: int = 26, senkou_p: int = 52
) -> Ichimoku:
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    n = len(candles)

    def midpoint(period: int) -> list[float | None]:
        out: list[float | None] = [None] * n
        for i in range(period - 1, n):
            window_high = max(highs[i - period + 1: i + 1])
            window_low = min(lows[i - period + 1: i + 1])
            out[i] = (window_high + window_low) / 2
        return out

    tenkan = midpoint(tenkan_p)
    kijun = midpoint(kijun_p)

    # Senkou spans are plotted `kijun_p` bars ahead; shift so index i holds the
    # cloud value that is in force at bar i (i.e. computed kijun_p bars ago).
    raw_a: list[float | None] = [
        (t + k) / 2 if (t is not None and k is not None) else None
        for t, k in zip(tenkan, kijun)
    ]
    raw_b = midpoint(senkou_p)
    senkou_a = _shift_forward(raw_a, kijun_p)
    senkou_b = _shift_forward(raw_b, kijun_p)
    chikou = _shift_back(closes, kijun_p)

    return Ichimoku(tenkan, kijun, senkou_a, senkou_b, chikou)


def _shift_forward(values: Sequence[float | None], offset: int) -> list[float | None]:
    return [None] * offset + list(values[: len(values) - offset]) if offset else list(values)


def _shift_back(values: Sequence[float], offset: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values) - offset):
        out[i] = values[i + offset]
    return out
