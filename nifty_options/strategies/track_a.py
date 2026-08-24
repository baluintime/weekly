"""Track A -- Intraday Debit Momentum.

Rules implemented verbatim from section 3 of the framework:

* Entry: price consolidates near the 20 EMA / Ichimoku cloud, then breaks out
  on a strong candle with the MACD histogram expanding above the zero line.
* Strike: slightly ITM / ATM, |delta| in [0.50, 0.60].
* Risk: <= 2% of capital (Rs 4,000) per trade, total outlay < Rs 15,000.
* Exit: MACD histogram flattens or reverses, or price closes back inside the
  20 EMA; target at 1:2 reward-to-risk. Everything is squared off intraday.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Sequence

from ..brokers.base import Side
from ..config import Config
from ..indicators import Candle, MACD, ichimoku, ema, macd
from ..upstox.instruments import OptionQuote, select_delta_band
from .base import Leg, MarketContext, Strategy, Trade, TradePlan

LOG = logging.getLogger(__name__)


class TrackAIntradayMomentum(Strategy):
    track = "Track A"
    name = "track_a_intraday_debit"

    def __init__(self, config: Config):
        super().__init__(config)
        self.params = config.track_a

    # ------------------------------------------------------------------ #
    # entry
    # ------------------------------------------------------------------ #
    def evaluate_entry(
        self, ctx: MarketContext, open_trades: Sequence[Trade]
    ) -> TradePlan | None:
        if not self.params.enabled:
            return None

        mine = [t for t in open_trades if t.track == self.track]
        if any(t.is_open for t in mine):
            return None                       # one directional bet at a time

        today = [t for t in mine if t.opened_at.date() == ctx.today]
        if len(today) >= self.params.max_trades_per_day:
            LOG.debug("Track A: daily trade cap (%d) reached.", self.params.max_trades_per_day)
            return None

        if self.in_cooldown(today, ctx.now):
            return None

        if not self.in_window(ctx.now, self.params.entry_window):
            return None

        direction = self.detect_breakout(ctx.candles)
        if direction is None:
            return None

        option_type = "CE" if direction > 0 else "PE"
        max_premium = self.params.max_outlay_per_trade / self.config.lot_size
        quote = select_delta_band(
            ctx.chain,
            option_type,                      # type: ignore[arg-type]
            self.params.min_delta,
            self.params.max_delta,
            max_premium=max_premium,
        )
        if quote is None:
            LOG.info("Track A: no strike in the %.2f-%.2f delta band.",
                     self.params.min_delta, self.params.max_delta)
            return None

        return self.build_plan(quote, direction, ctx)

    def in_cooldown(self, todays_trades: Sequence[Trade], now: datetime) -> bool:
        """Block an immediate re-entry on the same signal after an exit.

        The breakout candles that triggered the trade are still in the window
        right after a target or stop fills, so without this the engine would
        re-arm the identical setup on the very next tick.
        """
        minutes = self.params.reentry_cooldown_minutes
        if minutes <= 0:
            return False
        for trade in todays_trades:
            if trade.closed_at and (now - trade.closed_at).total_seconds() < minutes * 60:
                LOG.debug(
                    "Track A: in cooldown until %s.",
                    trade.closed_at + timedelta(minutes=minutes),
                )
                return True
        return False

    def detect_breakout(self, candles: Sequence[Candle]) -> int | None:
        """+1 for a bullish breakout, -1 for bearish, None for no setup."""
        slow = self.params.macd[1]
        if len(candles) < max(self.params.ichimoku[2], slow) + 5:
            return None

        closes = [c.close for c in candles]
        ema20 = ema(closes, self.params.ema_period)
        if ema20[-1] is None:
            return None

        macd_values = macd(closes, *self.params.macd)
        cloud = ichimoku(candles, *self.params.ichimoku)

        last, previous = candles[-1], candles[-2]
        histogram = macd_values.hist_at(-1)
        if histogram is None or not macd_values.histogram_expanding():
            return None

        if not self._consolidated_near_reference(candles, ema20, cloud):
            return None

        if not self._is_strong_candle(last, candles):
            return None

        bullish = (
            last.close > last.open
            and histogram > 0
            and last.close > ema20[-1]
            and last.close > previous.high
            and cloud.price_above_cloud(last.close)
        )
        if bullish:
            return 1

        bearish = (
            last.close < last.open
            and histogram < 0
            and last.close < ema20[-1]
            and last.close < previous.low
            and cloud.price_below_cloud(last.close)
        )
        if bearish:
            return -1
        return None

    def _consolidated_near_reference(
        self, candles: Sequence[Candle], ema20, cloud, lookback: int = 5
    ) -> bool:
        """The breakout must come out of a coil around the 20 EMA / cloud."""
        window = candles[-(lookback + 1): -1]
        if len(window) < lookback:
            return False
        for index, candle in enumerate(window):
            position = len(candles) - (lookback + 1) + index
            reference = ema20[position]
            if reference is None:
                return False
            near_ema = abs(candle.close - reference) / reference <= 0.0025
            if near_ema or cloud.price_inside_cloud(candle.close, position):
                return True
        return False

    @staticmethod
    def _is_strong_candle(last: Candle, candles: Sequence[Candle], lookback: int = 10) -> bool:
        """Breakout candle must have above-average range and close near its extreme."""
        window = candles[-(lookback + 1): -1]
        if not window:
            return False
        average_range = sum(c.high - c.low for c in window) / len(window)
        candle_range = last.high - last.low
        if average_range <= 0 or candle_range < average_range * 1.2:
            return False
        body = abs(last.close - last.open)
        return body >= candle_range * 0.5

    def build_plan(
        self, quote: OptionQuote, direction: int, ctx: MarketContext
    ) -> TradePlan | None:
        premium = quote.mid or quote.ltp
        if premium <= 0:
            return None

        lots = self.size_position(premium)
        if lots < 1:
            LOG.info(
                "Track A: %s at Rs %.2f cannot be sized inside the Rs %.0f outlay cap.",
                quote.symbol, premium, self.params.max_outlay_per_trade,
            )
            return None

        quantity = lots * self.config.lot_size
        risk_per_unit = self.params.risk_per_trade / quantity
        # Never risk more than the premium itself -- a long option's floor is 0.
        risk_per_unit = min(risk_per_unit, premium)
        stop_loss = round(max(premium - risk_per_unit, 0.05), 2)
        target = round(premium + risk_per_unit * self.params.reward_to_risk, 2)

        leg = Leg(
            instrument_key=quote.instrument_key,
            symbol=quote.symbol,
            side=Side.BUY,
            lots=lots,
            quantity=quantity,
            entry_price=premium,
            delta=quote.delta,
            strike=quote.strike,
            option_type=quote.option_type,
            role="debit",
        )
        bias = "bullish" if direction > 0 else "bearish"
        return TradePlan(
            track=self.track,
            strategy=self.name,
            description=f"Nifty {int(quote.strike)} {quote.option_type} (Buy)",
            legs=[leg],
            direction=1,
            stop_loss=stop_loss,
            target=target,
            max_loss=round(risk_per_unit * quantity, 2),
            rationale=(
                f"{bias} cloud/20EMA breakout, MACD histogram expanding; "
                f"delta {quote.delta:+.2f}, premium Rs {premium:.2f}, {lots} lot(s), "
                f"risk Rs {risk_per_unit * quantity:,.0f}, RR 1:{self.params.reward_to_risk:g}"
            ),
        )

    def size_position(self, premium: float) -> int:
        """Largest lot count satisfying both the outlay cap and the risk cap."""
        lot_value = premium * self.config.lot_size
        if lot_value <= 0:
            return 0
        by_outlay = int(self.params.max_outlay_per_trade // lot_value)
        # A long option can lose its whole premium, so cap total premium at the
        # 2% risk budget too -- whichever constraint binds first wins.
        by_risk = int(self.params.risk_per_trade // lot_value) or by_outlay
        return max(min(by_outlay, by_risk if by_risk else by_outlay), 0)

    # ------------------------------------------------------------------ #
    # exit
    # ------------------------------------------------------------------ #
    def evaluate_exit(self, trade: Trade, ctx: MarketContext) -> str | None:
        price = trade.current_price(ctx)

        if price <= trade.stop_loss:
            return f"stop-loss hit at {price:.2f} (stop {trade.stop_loss:.2f})"
        if trade.target and price >= trade.target:
            return f"target hit at {price:.2f} (1:{self.params.reward_to_risk:g} RR)"

        if ctx.now.time() >= self.parse_time(self.params.square_off_time):
            return f"intraday square-off at {self.params.square_off_time}"

        if len(ctx.candles) >= self.params.macd[1] + 5:
            closes = [c.close for c in ctx.candles]
            macd_values = macd(closes, *self.params.macd)
            leg = trade.legs[0]
            direction = 1 if leg.option_type == "CE" else -1
            if macd_values.momentum_fading(direction):
                return "MACD histogram flattened/reversed"

            ema20 = ema(closes, self.params.ema_period)
            reference = ema20[-1]
            last = ctx.candles[-1]
            if reference is not None:
                back_inside = (
                    last.close < reference if leg.option_type == "CE" else last.close > reference
                )
                if back_inside:
                    return f"price closed back inside the {self.params.ema_period} EMA"
        return None
