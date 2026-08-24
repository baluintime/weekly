"""Indicator maths: 20 EMA, MACD (12/26/9) and Ichimoku (9/26/52)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nifty_options.indicators import Candle, ema, ichimoku, macd, sma, to_candles


def candles(closes: list[float]) -> list[Candle]:
    base = datetime(2026, 8, 24, 9, 15)
    return [
        Candle(base + timedelta(minutes=5 * i), c - 1, c + 3, c - 3, c)
        for i, c in enumerate(closes)
    ]


def test_sma_warms_up_then_averages():
    values = sma([1, 2, 3, 4, 5], 3)
    assert values[:2] == [None, None]
    assert values[2] == pytest.approx(2.0)
    assert values[-1] == pytest.approx(4.0)


def test_ema_is_seeded_with_the_sma():
    values = ema([1, 2, 3, 4, 5], 3)
    assert values[1] is None
    assert values[2] == pytest.approx(2.0)          # SMA seed


def test_ema_tracks_a_constant_series():
    values = ema([50.0] * 40, 20)
    assert values[-1] == pytest.approx(50.0)


def test_ema_is_shorter_than_the_period():
    assert ema([1, 2], 20) == [None, None]


def test_ema_reacts_faster_than_sma_after_a_step():
    closes = [100.0] * 40 + [130.0] * 5
    assert ema(closes, 20)[-1] > sma(closes, 20)[-1]


def test_ema_and_sma_agree_on_a_linear_ramp():
    closes = list(range(1, 61))
    assert ema(closes, 20)[-1] == pytest.approx(sma(closes, 20)[-1])


def test_macd_is_positive_in_an_uptrend():
    closes = [100 + i * 1.5 for i in range(80)]
    result = macd(closes)
    assert result.macd[-1] > 0


def test_macd_is_negative_in_a_downtrend():
    closes = [200 - i * 1.5 for i in range(80)]
    assert macd(closes).macd[-1] < 0


def test_macd_histogram_is_macd_minus_signal():
    closes = [100 + i * 1.2 for i in range(80)]
    result = macd(closes)
    assert result.histogram[-1] == pytest.approx(result.macd[-1] - result.signal[-1])


def test_histogram_expanding_detects_acceleration():
    closes = [100 + i for i in range(60)] + [160 + i * 8 for i in range(1, 6)]
    assert macd(closes).histogram_expanding() is True


def test_histogram_flattening_detects_a_stall():
    closes = [100 + i * 3 for i in range(60)] + [280.0] * 2
    assert macd(closes).histogram_flattening() is True


def test_momentum_fading_catches_an_established_reversal():
    """A long-call exit must fire even once the flip is several bars old."""
    closes = [100 + i * 3 for i in range(60)] + [280.0] * 8
    result = macd(closes)
    assert result.histogram[-1] < result.histogram[-2]      # still expanding down
    assert result.histogram_flattening() is False           # magnitude is growing
    assert result.momentum_fading(direction=1) is True      # but the long is dead


def test_momentum_fading_holds_a_healthy_trend():
    closes = [100 + i * 2 for i in range(60)] + [220 + i * 9 for i in range(1, 5)]
    assert macd(closes).momentum_fading(direction=1) is False


def test_ichimoku_cloud_bounds_are_ordered():
    cloud = ichimoku(candles([100 + i * 0.4 for i in range(120)]))
    top, bottom = cloud.cloud_at(-1)
    assert top >= bottom


def test_price_above_cloud_in_a_strong_uptrend():
    cloud = ichimoku(candles([100 + i * 2 for i in range(120)]))
    assert cloud.price_above_cloud(100 + 119 * 2) is True
    assert cloud.price_below_cloud(100 + 119 * 2) is False


def test_price_below_cloud_in_a_downtrend():
    closes = [400 - i * 2 for i in range(120)]
    cloud = ichimoku(candles(closes))
    assert cloud.price_below_cloud(closes[-1]) is True


def test_price_inside_a_flat_cloud():
    cloud = ichimoku(candles([100.0] * 120))
    top, bottom = cloud.cloud_at(-1)
    assert cloud.price_inside_cloud((top + bottom) / 2) is True


def test_cloud_is_undefined_before_warmup():
    assert ichimoku(candles([100.0] * 10)).cloud_at(-1) == (None, None)


def test_upstox_candle_rows_parse():
    rows = [["2026-08-24T09:15:00+05:30", 100, 105, 99, 103, 12000, 5]]
    parsed = to_candles(rows)
    assert parsed[0].close == 103
    assert parsed[0].volume == 12000
    assert parsed[0].timestamp.hour == 9
