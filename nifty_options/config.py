"""Configuration loading and the paper/live trading-mode switch.

Mode resolution precedence (highest first):

    1. explicit argument (CLI ``--mode`` / ``--live``)
    2. ``UPSTOX_TRADING_MODE`` environment variable
    3. ``mode:`` in config.yaml
    4. paper  (the default -- live is never reached by accident)

Resolving to LIVE additionally requires every one of the guards in
:meth:`Config.assert_live_allowed` to pass, so flipping a single flag is
never enough to start sending real orders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .credentials import load_env

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


class TradingMode(str, Enum):
    """Which broker implementation the engine talks to."""

    PAPER = "paper"
    LIVE = "live"

    @property
    def is_live(self) -> bool:
        return self is TradingMode.LIVE


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unsafe."""


class LiveTradingBlocked(ConfigError):
    """Raised when live mode was requested but a safety guard rejected it."""


@dataclass
class UpstoxCredentials:
    api_key: str = ""
    api_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:5000/callback"
    token_path: Path = REPO_ROOT / "data" / "upstox_token.json"
    sandbox_access_token: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UpstoxCredentials":
        return cls(
            api_key=os.getenv("UPSTOX_API_KEY", raw.get("api_key", "")),
            api_secret=os.getenv("UPSTOX_API_SECRET", raw.get("api_secret", "")),
            redirect_uri=os.getenv(
                "UPSTOX_REDIRECT_URI", raw.get("redirect_uri", cls.redirect_uri)
            ),
            token_path=Path(
                os.getenv("UPSTOX_TOKEN_PATH", raw.get("token_path", str(cls.token_path)))
            ).expanduser(),
            sandbox_access_token=os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", ""),
        )


@dataclass
class TrackAConfig:
    """Track A -- intraday debit momentum (PDF section 3)."""

    enabled: bool = True
    capital: float = 200_000.0
    risk_per_trade_pct: float = 2.0          # -> Rs 4,000 on Rs 2,00,000
    max_outlay_per_trade: float = 15_000.0
    min_delta: float = 0.50
    max_delta: float = 0.60
    reward_to_risk: float = 2.0
    candle_interval_minutes: int = 5
    ema_period: int = 20
    macd: tuple[int, int, int] = (12, 26, 9)
    ichimoku: tuple[int, int, int] = (9, 26, 52)
    max_trades_per_day: int = 4
    reentry_cooldown_minutes: int = 15
    entry_window: tuple[str, str] = ("09:30", "14:45")
    square_off_time: str = "15:15"

    @property
    def risk_per_trade(self) -> float:
        return self.capital * self.risk_per_trade_pct / 100.0


@dataclass
class TrackBConfig:
    """Track B -- weekly credit decay / iron condor (PDF section 3)."""

    enabled: bool = True
    capital: float = 200_000.0
    short_leg_delta: float = 0.15
    delta_tolerance: float = 0.05
    wing_width_points: int = 100
    lots: int = 2
    max_lots: int = 3
    max_margin: float = 150_000.0
    min_margin_buffer: float = 50_000.0
    # SPAN+exposure for one hedged Nifty condor lot. Used only when a live
    # margin quote from /charges/margin is unavailable.
    margin_per_lot_estimate: float = 60_000.0
    profit_target_pct: float = 55.0          # "50%-60% of credit captured"
    stop_on_short_strike_touch: bool = True
    max_trades_per_week: int = 2
    reentry_cooldown_minutes: int = 60
    # Empty = any day inside the days-to-expiry window, which follows the
    # exchange's real expiry calendar. Naming weekdays here pins the rule and
    # will silently stop the track when NSE moves expiry.
    entry_days: tuple[str, ...] = ()
    entry_window: tuple[str, str] = ("09:30", "11:30")
    min_days_to_expiry: int = 2
    max_days_to_expiry: int = 5


@dataclass
class RiskConfig:
    daily_loss_limit_pct: float = 4.0
    max_open_positions: int = 6
    kill_switch_file: Path = REPO_ROOT / "data" / "KILL_SWITCH"
    # Live-only ceilings, applied on top of the per-track limits.
    live_max_order_value: float = 60_000.0
    live_max_daily_orders: int = 40


@dataclass
class PaperFillConfig:
    """How the paper broker simulates a real fill."""

    slippage_bps: float = 25.0            # on the option premium
    slippage_min_ticks: float = 1.0
    tick_size: float = 0.05
    brokerage_per_order: float = 20.0
    apply_statutory_charges: bool = True
    reject_outside_market_hours: bool = True


@dataclass
class LiveGuardConfig:
    """Guards that must all pass before a single real order is sent."""

    enabled: bool = False
    confirmation_phrase: str = "I UNDERSTAND THIS TRADES REAL MONEY"
    max_capital: float = 400_000.0
    require_market_hours: bool = True
    # Shadow: armed live, connected to the live account, fills simulated.
    # No order-placing code path is reachable while this is true.
    dry_run: bool = False


@dataclass
class Config:
    mode: TradingMode = TradingMode.PAPER
    instrument_key: str = "NSE_INDEX|Nifty 50"
    underlying_symbol: str = "NIFTY"
    exchange: str = "NSE_FO"
    lot_size: int = 65                     # fallback only; the live value is
                                           # read from the contract master
    product: str = "I"                     # I = intraday, D = delivery/carry-forward
    upstox: UpstoxCredentials = field(default_factory=UpstoxCredentials)
    track_a: TrackAConfig = field(default_factory=TrackAConfig)
    track_b: TrackBConfig = field(default_factory=TrackBConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper: PaperFillConfig = field(default_factory=PaperFillConfig)
    live: LiveGuardConfig = field(default_factory=LiveGuardConfig)
    journal_dir: Path = REPO_ROOT / "data" / "journal"
    state_dir: Path = REPO_ROOT / "data" / "state"
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        mode: str | TradingMode | None = None,
    ) -> "Config":
        # `.env` is loaded before anything reads os.environ, so stored
        # credentials behave exactly like exported ones.
        load_env()
        path = Path(path or os.getenv("NIFTY_CONFIG", DEFAULT_CONFIG_PATH))
        raw: dict[str, Any] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}

        cfg = cls(
            instrument_key=raw.get("instrument_key", cls.instrument_key),
            underlying_symbol=raw.get("underlying_symbol", cls.underlying_symbol),
            exchange=raw.get("exchange", cls.exchange),
            lot_size=int(raw.get("lot_size", cls.lot_size)),
            product=raw.get("product", cls.product),
            upstox=UpstoxCredentials.from_dict(raw.get("upstox", {})),
            track_a=_build(TrackAConfig, raw.get("track_a", {})),
            track_b=_build(TrackBConfig, raw.get("track_b", {})),
            risk=_build(RiskConfig, raw.get("risk", {})),
            paper=_build(PaperFillConfig, raw.get("paper", {})),
            live=_build(LiveGuardConfig, raw.get("live", {})),
            timezone=raw.get("timezone", cls.timezone),
            log_level=os.getenv("LOG_LEVEL", raw.get("log_level", cls.log_level)),
        )
        cfg.journal_dir = Path(raw.get("journal_dir", cfg.journal_dir)).expanduser()
        cfg.state_dir = Path(raw.get("state_dir", cfg.state_dir)).expanduser()
        cfg.mode = cls.resolve_mode(mode, raw.get("mode"))
        return cfg

    @staticmethod
    def resolve_mode(
        explicit: str | TradingMode | None = None,
        from_file: str | None = None,
    ) -> TradingMode:
        """Apply the documented precedence and return the effective mode."""
        for candidate in (explicit, os.getenv("UPSTOX_TRADING_MODE"), from_file):
            if candidate in (None, ""):
                continue
            if isinstance(candidate, TradingMode):
                return candidate
            value = str(candidate).strip().lower()
            if value in ("live", "real", "actual"):
                return TradingMode.LIVE
            if value in ("paper", "sim", "simulated", "dry"):
                return TradingMode.PAPER
            raise ConfigError(
                f"Unknown trading mode {candidate!r}; expected 'paper' or 'live'."
            )
        return TradingMode.PAPER

    # ------------------------------------------------------------------ #
    # live-mode guards
    # ------------------------------------------------------------------ #
    def assert_live_allowed(self) -> None:
        """Reject live trading unless every independent guard is satisfied.

        Four separate switches must line up, so no single typo, stale env var
        or committed config file can move the engine onto real money:

        * mode resolves to live,
        * ``live.enabled: true`` in config.yaml,
        * ``UPSTOX_LIVE_CONFIRM`` matches ``live.confirmation_phrase``,
        * API credentials are actually present.
        """
        if not self.mode.is_live:
            return

        if not self.live.enabled:
            raise LiveTradingBlocked(
                "Live mode requested but 'live.enabled' is false in config.yaml. "
                "Set live.enabled: true to arm actual trading."
            )

        confirm = os.getenv("UPSTOX_LIVE_CONFIRM", "").strip()
        if confirm != self.live.confirmation_phrase:
            raise LiveTradingBlocked(
                "Live mode requested but UPSTOX_LIVE_CONFIRM does not match "
                "live.confirmation_phrase. Export:\n"
                f'  export UPSTOX_LIVE_CONFIRM="{self.live.confirmation_phrase}"'
            )

        if not (self.upstox.api_key and self.upstox.api_secret):
            raise LiveTradingBlocked(
                "Live mode requires UPSTOX_API_KEY and UPSTOX_API_SECRET."
            )

        deployed = self.track_a.capital + self.track_b.capital
        if deployed > self.live.max_capital:
            raise LiveTradingBlocked(
                f"Configured capital Rs {deployed:,.0f} exceeds live.max_capital "
                f"Rs {self.live.max_capital:,.0f}. Lower the track capital or raise "
                "the ceiling deliberately."
            )

    @property
    def session_label(self) -> str:
        """paper | shadow | live -- names the run for journals and state.

        Shadow is live-armed and reads everything from the live account, but
        settles fills internally, so its record is kept apart from both the
        pure paper book and the real one.
        """
        if not self.mode.is_live:
            return "paper"
        return "shadow" if self.live.dry_run else "live"

    @property
    def sends_real_orders(self) -> bool:
        return self.mode.is_live and not self.live.dry_run

    def with_mode(self, mode: str | TradingMode) -> "Config":
        """Return a copy of this config switched to another mode."""
        import copy

        clone = copy.deepcopy(self)
        clone.mode = self.resolve_mode(mode)
        return clone

    def ensure_dirs(self) -> None:
        for directory in (self.journal_dir, self.state_dir, self.upstox.token_path.parent):
            directory.mkdir(parents=True, exist_ok=True)


def _build(cls, raw: dict[str, Any]):
    """Instantiate a dataclass from a YAML mapping, ignoring unknown keys."""
    if not raw:
        return cls()
    fields = {f.name: f for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in fields:
            continue
        current = getattr(cls(), key)
        if isinstance(current, Path):
            value = Path(value).expanduser()
        elif isinstance(current, tuple) and isinstance(value, list):
            value = tuple(value)
        kwargs[key] = value
    return cls(**kwargs)
