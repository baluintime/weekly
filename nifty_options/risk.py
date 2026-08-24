"""Portfolio-level risk limits shared by both tracks and both trading modes.

The per-trade sizing rules live in the strategies (2% risk / Rs 15,000 outlay
for Track A, margin caps for Track B); this module owns the limits that apply
across strategies: daily loss cap, position count, and the kill switch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .brokers.base import Broker, OrderRequest, Side
from .config import Config

LOG = logging.getLogger(__name__)


@dataclass
class RiskState:
    day: date = field(default_factory=date.today)
    realized_pnl: float = 0.0
    trades_taken: int = 0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, config: Config, broker: Broker):
        self.config = config
        self.broker = broker
        self.state = RiskState()

    # ------------------------------------------------------------------ #
    @property
    def total_capital(self) -> float:
        return self.config.track_a.capital + self.config.track_b.capital

    @property
    def daily_loss_limit(self) -> float:
        return self.total_capital * self.config.risk.daily_loss_limit_pct / 100.0

    def roll_day(self, today: date | None = None) -> None:
        today = today or date.today()
        if today != self.state.day:
            LOG.info("New session %s -- resetting daily risk counters.", today)
            self.state = RiskState(day=today)

    # ------------------------------------------------------------------ #
    def kill_switch_active(self) -> bool:
        return self.config.risk.kill_switch_file.exists()

    def engage_kill_switch(self, reason: str) -> None:
        path = self.config.risk.kill_switch_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{date.today().isoformat()}: {reason}\n")
        self.state.halted = True
        self.state.halt_reason = reason
        LOG.critical("KILL SWITCH ENGAGED: %s", reason)

    def release_kill_switch(self) -> None:
        self.config.risk.kill_switch_file.unlink(missing_ok=True)
        self.state.halted = False
        self.state.halt_reason = ""
        LOG.warning("Kill switch released.")

    # ------------------------------------------------------------------ #
    def record_trade(self, realized_pnl: float) -> None:
        self.state.realized_pnl += realized_pnl
        self.state.trades_taken += 1
        if self.state.realized_pnl <= -self.daily_loss_limit:
            self.engage_kill_switch(
                f"Daily loss limit hit: Rs {self.state.realized_pnl:,.0f} "
                f"vs limit Rs {-self.daily_loss_limit:,.0f}"
            )

    def can_open(self, request: OrderRequest, opening: bool = True) -> tuple[bool, str]:
        """Gate a *new* exposure. Closing orders are never blocked."""
        if not opening:
            return True, ""

        if self.kill_switch_active():
            return False, f"kill switch active ({self.config.risk.kill_switch_file})"

        if self.state.halted:
            return False, f"session halted: {self.state.halt_reason}"

        if self.state.realized_pnl <= -self.daily_loss_limit:
            return False, (
                f"daily loss limit reached (Rs {self.state.realized_pnl:,.0f})"
            )

        open_positions = [p for p in self.broker.get_positions() if p.is_open]
        if len(open_positions) >= self.config.risk.max_open_positions:
            return False, (
                f"max open positions reached ({self.config.risk.max_open_positions})"
            )

        if request.side is Side.BUY and request.notional > self.config.track_a.max_outlay_per_trade:
            # Only debit (buy-to-open) legs are capped by the outlay rule.
            if request.strategy.lower().startswith("track_a"):
                return False, (
                    f"outlay Rs {request.notional:,.0f} exceeds cap "
                    f"Rs {self.config.track_a.max_outlay_per_trade:,.0f}"
                )

        return True, ""

    def status(self) -> dict[str, object]:
        return {
            "day": self.state.day.isoformat(),
            "realized_pnl": round(self.state.realized_pnl, 2),
            "daily_loss_limit": round(-self.daily_loss_limit, 2),
            "trades_taken": self.state.trades_taken,
            "kill_switch": self.kill_switch_active(),
            "halted": self.state.halted or self.kill_switch_active(),
            "halt_reason": self.state.halt_reason,
        }
