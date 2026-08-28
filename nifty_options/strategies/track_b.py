"""Track B -- Weekly Credit Decay (Iron Condor).

Rules from section 3 of the framework:

* Entry: Monday morning, or after an initial directional move exhausts near
  major cloud support/resistance.
* Structure: sell 1 OTM call (delta ~0.15) + buy 1 OTM call 100 points higher;
  sell 1 OTM put (delta ~0.15) + buy 1 OTM put 100 points lower.
* Margin: ~Rs 1.2-1.5 lakh for 2-3 lots, rest held as buffer.
* Exit: close at 50-60% of the credit captured, or exit the tested side when
  spot touches a short strike.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..brokers.base import Side
from ..config import Config
from ..indicators import Candle, ichimoku, ema, macd
from ..upstox.contracts import expiry_in_window
from ..upstox.instruments import (
    OptionQuote,
    days_to_expiry,
    find_strike,
    select_by_delta,
)
from .base import Leg, MarketContext, Strategy, Trade, TradePlan

LOG = logging.getLogger(__name__)

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4}


class TrackBWeeklyCondor(Strategy):
    track = "Track B"
    name = "track_b_weekly_condor"

    def __init__(self, config: Config):
        super().__init__(config)
        self.params = config.track_b

    # ------------------------------------------------------------------ #
    # entry
    # ------------------------------------------------------------------ #
    def evaluate_entry(
        self, ctx: MarketContext, open_trades: Sequence[Trade]
    ) -> TradePlan | None:
        if not self.params.enabled:
            return self._skip("track disabled in config")

        mine = [t for t in open_trades if t.track == self.track]
        if any(t.is_open for t in mine):
            return self._skip("condor already open")

        week = ctx.today.isocalendar()[:2]
        this_week = [t for t in mine if t.opened_at.date().isocalendar()[:2] == week]
        if len(this_week) >= self.params.max_trades_per_week:
            return self._skip(
                f"weekly structural-trade cap reached ({self.params.max_trades_per_week})"
            )

        cooldown = self.params.reentry_cooldown_minutes
        if cooldown > 0 and any(
            t.closed_at and (ctx.now - t.closed_at).total_seconds() < cooldown * 60
            for t in this_week
        ):
            return self._skip(f"in re-entry cooldown ({cooldown} min after the last exit)")

        if not self.in_window(ctx.now, self.params.entry_window):
            start, end = self.params.entry_window
            return self._skip(f"outside the entry window ({start}-{end})")

        expiry = self.select_expiry(ctx)
        if not expiry:
            return self._skip(self.last_skip_reason or "no expiry in the holding window")

        if not self.is_entry_day(ctx):
            days = "/".join(self.params.entry_days)
            return self._skip(
                f"{ctx.now:%A} is not an entry day ({days}) and no exhausted move "
                "at a cloud edge"
            )

        return self.build_condor(ctx)

    def select_expiry(self, ctx: MarketContext) -> str:
        """The listed expiry that gives the framework's 2-5 day hold.

        Uses the exchange's real calendar when the engine supplied one, so a
        Tuesday, Thursday or holiday-shifted expiry all work unchanged.
        """
        low, high = self.params.min_days_to_expiry, self.params.max_days_to_expiry
        if ctx.expiries:
            chosen = expiry_in_window(ctx.expiries, ctx.today, low, high)
            if chosen is not None:
                return chosen.expiry
            listed = ", ".join(
                f"{e.expiry} ({e.weekday}, {e.days_to_expiry(ctx.today)}d)"
                for e in ctx.expiries[:3]
            )
            self._skip(
                f"no listed expiry {low}-{high} days out; nearest are {listed}"
            )
            return ""

        if not ctx.expiry:
            self._skip("no expiry found in the option contracts")
            return ""
        dte = days_to_expiry(ctx.expiry, ctx.today)
        if not (low <= dte <= high):
            self._skip(
                f"expiry {ctx.expiry} is {dte} days out, outside the {low}-{high} day window"
            )
            return ""
        return ctx.expiry

    def is_entry_day(self, ctx: MarketContext) -> bool:
        """Whether today may open a condor.

        With `entry_days` empty (the default) any day inside the
        days-to-expiry window qualifies, so the rule follows the exchange's
        actual expiry calendar. The framework's "deployed on Monday" assumed a
        Thursday expiry; when NSE moves expiry to Tuesday, Monday is 1 day out
        and the equivalent entry day becomes Thursday or Friday. Pinning a
        weekday would silently stop the track from ever trading.
        """
        if self.params.entry_days:
            allowed = {
                WEEKDAYS[d.upper()] for d in self.params.entry_days if d.upper() in WEEKDAYS
            }
            if ctx.now.weekday() in allowed:
                return True
            return self.move_exhausted(ctx)
        return True

    def move_exhausted(self, ctx: MarketContext) -> bool:
        """Alternative entry: a directional push stalling at cloud support/resistance."""
        candles = ctx.candles
        if len(candles) < 60:
            return False

        closes = [c.close for c in candles]
        cloud = ichimoku(candles)
        top, bottom = cloud.cloud_at(-1)
        if top is None or bottom is None:
            return False

        last = candles[-1].close
        # Price pressed into a cloud edge (within 0.4%) and MACD momentum faded.
        near_edge = (
            abs(last - top) / top <= 0.004 or abs(last - bottom) / bottom <= 0.004
        )
        faded = macd(closes).histogram_flattening()
        return near_edge and faded

    def build_condor(
        self, ctx: MarketContext, margin_per_lot: float | None = None
    ) -> TradePlan | None:
        target = self.params.short_leg_delta
        tolerance = self.params.delta_tolerance

        short_call = select_by_delta(ctx.chain, "CE", target, tolerance)
        short_put = select_by_delta(ctx.chain, "PE", target, tolerance)
        if short_call is None or short_put is None:
            return self._skip(f"no strikes near delta {target:.2f} in the chain")

        wing = self.params.wing_width_points
        long_call = find_strike(ctx.chain, short_call.strike + wing, "CE")
        long_put = find_strike(ctx.chain, short_put.strike - wing, "PE")
        if long_call is None or long_put is None:
            return self._skip(
                f"{wing}-point wings unavailable around "
                f"{int(short_put.strike)}/{int(short_call.strike)}"
            )

        if short_put.strike >= short_call.strike:
            return self._skip("short strikes inverted; chain looks wrong")

        credit = round(
            (short_call.mid + short_put.mid) - (long_call.mid + long_put.mid), 2
        )
        if credit <= 0:
            return self._skip(f"structure prices to a debit (Rs {credit:.2f}), not a credit")

        lots = self.size_position(credit, margin_per_lot)
        if lots < 1:
            return self._skip("margin per lot exceeds the Track B allocation")

        quantity = lots * self.config.lot_size
        max_loss_per_unit = round(wing - credit, 2)

        legs = [
            self._leg(short_call, Side.SELL, lots, quantity, "short_call"),
            self._leg(long_call, Side.BUY, lots, quantity, "long_call"),
            self._leg(short_put, Side.SELL, lots, quantity, "short_put"),
            self._leg(long_put, Side.BUY, lots, quantity, "long_put"),
        ]

        target_price = round(credit * (1 - self.params.profit_target_pct / 100.0), 2)
        description = (
            f"Nifty {int(short_put.strike)}/{int(short_call.strike)} Condor "
            f"(+{int(long_put.strike)}P/{int(long_call.strike)}C wings)"
        )
        return TradePlan(
            track=self.track,
            strategy=self.name,
            description=description,
            legs=legs,
            direction=-1,
            stop_loss=0.0,                    # risk is capped by the wings
            target=target_price,
            max_loss=round(max_loss_per_unit * quantity, 2),
            rationale=(
                f"Credit Rs {credit:.2f} x {quantity} = Rs {credit * quantity:,.0f}; "
                f"short deltas {short_call.delta:+.2f}/{short_put.delta:+.2f}; "
                f"{wing}-pt wings cap loss at Rs {max_loss_per_unit * quantity:,.0f}; "
                f"take profit at {self.params.profit_target_pct:.0f}% of credit"
            ),
        )

    def _leg(
        self, quote: OptionQuote, side: Side, lots: int, quantity: int, role: str
    ) -> Leg:
        return Leg(
            instrument_key=quote.instrument_key,
            symbol=quote.symbol,
            side=side,
            lots=lots,
            quantity=quantity,
            entry_price=quote.mid or quote.ltp,
            delta=quote.delta,
            strike=quote.strike,
            option_type=quote.option_type,
            role=role,
        )

    def size_position(self, credit: float, margin_per_lot: float | None = None) -> int:
        """Lots that fit the margin allocation, keeping the capital buffer.

        `margin_per_lot` should be a live quote from Upstox `/charges/margin`
        when one is available; otherwise the configured SPAN estimate is used.
        The framework allocates Rs 1.2-1.5 lakh for 2-3 lots and holds the
        remainder as buffer, which `min_margin_buffer` enforces.
        """
        if margin_per_lot is None or margin_per_lot <= 0:
            margin_per_lot = self.params.margin_per_lot_estimate
        # A hedged condor can never lose more than the wings allow, so the
        # margin actually blocked is never below that floor.
        margin_per_lot = max(
            margin_per_lot,
            (self.params.wing_width_points - credit) * self.config.lot_size,
        )
        budget = min(
            self.params.max_margin,
            max(self.params.capital - self.params.min_margin_buffer, 0.0),
        )
        affordable = int(budget // margin_per_lot)
        lots = min(self.params.lots, self.params.max_lots, affordable)
        if lots < 1:
            LOG.info(
                "Track B: margin per lot Rs %.0f exceeds the Rs %.0f budget; no entry.",
                margin_per_lot, budget,
            )
            return 0
        return lots

    # ------------------------------------------------------------------ #
    # exit
    # ------------------------------------------------------------------ #
    def evaluate_exit(self, trade: Trade, ctx: MarketContext) -> str | None:
        current = trade.current_price(ctx)
        captured = trade.entry_price - current
        capture_pct = (captured / trade.entry_price * 100.0) if trade.entry_price else 0.0

        if capture_pct >= self.params.profit_target_pct:
            return (
                f"{capture_pct:.0f}% of the Rs {trade.entry_price:.2f} credit captured "
                f"(target {self.params.profit_target_pct:.0f}%)"
            )

        if self.params.stop_on_short_strike_touch:
            tested = self.tested_side(trade, ctx.spot)
            if tested:
                return f"spot {ctx.spot:.0f} touched the short {tested} strike"

        if trade.expiry and days_to_expiry(trade.expiry, ctx.today) <= 0:
            return "expiry day -- closing the structure"

        return None

    @staticmethod
    def tested_side(trade: Trade, spot: float) -> str | None:
        for leg in trade.legs:
            if leg.role == "short_call" and spot >= leg.strike:
                return f"call {int(leg.strike)}"
            if leg.role == "short_put" and spot <= leg.strike:
                return f"put {int(leg.strike)}"
        return None

    @staticmethod
    def tested_legs(trade: Trade, spot: float) -> list[Leg]:
        """The two legs of the tested vertical, for a partial (one-side) exit."""
        for leg in trade.legs:
            if leg.role == "short_call" and spot >= leg.strike:
                return [l for l in trade.legs if l.role in ("short_call", "long_call")]
            if leg.role == "short_put" and spot <= leg.strike:
                return [l for l in trade.legs if l.role in ("short_put", "long_put")]
        return []
