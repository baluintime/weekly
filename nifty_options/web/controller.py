"""State owned by the dashboard: the engine, its thread, and the log tail.

The controller is the only object the HTTP layer talks to. It serialises
every mutating action behind one lock, so two browser tabs can never race the
mode switch or start two engine loops.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Any

from ..brokers import build_broker, build_client, describe_mode
from ..config import Config, ConfigError, TradingMode
from ..credentials import mask, save_credentials
from ..engine import Engine
from ..journal import Journal, comparison_report, evaluate
from ..upstox.auth import token_status
from ..upstox.client import UpstoxAPIError

LOG = logging.getLogger(__name__)


class LogTail(logging.Handler):
    """Ring buffer of recent log records, streamed to the dashboard."""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=capacity)
        self._sequence = 0

    def emit(self, record: logging.LogRecord) -> None:
        self._sequence += 1
        self.records.append(
            {
                "id": self._sequence,
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "source": record.name.replace("nifty_options.", ""),
                "message": record.getMessage(),
            }
        )

    def since(self, after: int = 0, limit: int = 200) -> list[dict[str, str]]:
        return [r for r in self.records if r["id"] > after][-limit:]


class DashboardController:
    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.logs = LogTail()
        self.logs.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(self.logs)
        # The activity panel is a headline feature, so make sure INFO records
        # actually reach it even when the host process left the root at WARNING.
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)

        self._engine: Engine | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_tick: dict[str, Any] = {}
        self._last_error: str = ""
        self._login_state: dict[str, str] = {"status": "idle", "message": ""}

    # ------------------------------------------------------------------ #
    # engine lifecycle
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def engine(self, create: bool = True) -> Engine | None:
        """Return the engine for the current mode, building it on demand."""
        with self.lock:
            if self._engine is not None and self._engine.config.mode is self.config.mode:
                return self._engine
            if not create:
                return None
            client = build_client(self.config, required=self.config.mode.is_live)
            broker = build_broker(self.config, client)
            self._engine = Engine(self.config, broker, client)
            return self._engine

    def start(self, poll_seconds: int = 60) -> dict[str, Any]:
        with self.lock:
            if self.running:
                return {"ok": False, "message": "The engine is already running."}
            self.config.assert_live_allowed()
            engine = self.engine()
            if engine is None:
                return {"ok": False, "message": "Could not build the engine."}

            self._stop.clear()
            self._last_error = ""

            def loop() -> None:
                try:
                    engine.run(
                        poll_seconds=poll_seconds,
                        stop_event=self._stop,
                        on_tick=self._record_tick,
                    )
                except Exception as exc:                 # surfaced in the UI
                    self._last_error = str(exc)
                    LOG.exception("Engine loop stopped with an error.")

            self._thread = threading.Thread(target=loop, name="engine", daemon=True)
            self._thread.start()
            LOG.warning(
                "Engine started in %s mode (polling every %ss).",
                self.config.mode.value.upper(), poll_seconds,
            )
            return {"ok": True, "message": f"Engine running in {self.config.mode.value} mode."}

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if not self.running:
                return {"ok": False, "message": "The engine is not running."}
            self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        LOG.warning("Engine stopped. Open positions were left untouched.")
        return {"ok": True, "message": "Engine stopped; open positions were left untouched."}

    def _record_tick(self, report: dict[str, Any]) -> None:
        self._last_tick = report

    def tick_once(self) -> dict[str, Any]:
        with self.lock:
            self.config.assert_live_allowed()
            engine = self.engine()
            if engine is None:
                return {"ok": False, "message": "Could not build the engine."}
            try:
                report = engine.tick()
            except UpstoxAPIError as exc:
                return {"ok": False, "message": f"Upstox error: {exc}"}
            self._last_tick = report
            return {"ok": True, "report": report}

    # ------------------------------------------------------------------ #
    # mode switch
    # ------------------------------------------------------------------ #
    def switch_mode(self, mode: str, confirmation: str = "") -> dict[str, Any]:
        """Flip between paper and actual trading from the UI.

        Live still has to clear every guard in `Config.assert_live_allowed`;
        the typed phrase replaces the terminal's interactive prompt, and the
        engine must be stopped first so a running loop never changes brokers
        underneath itself.
        """
        with self.lock:
            if self.running:
                return {"ok": False, "message": "Stop the engine before switching mode."}

            target = Config.resolve_mode(mode)
            if target.is_live:
                if confirmation.strip() != self.config.live.confirmation_phrase:
                    return {
                        "ok": False,
                        "message": "The confirmation phrase does not match. "
                        f'Type exactly: "{self.config.live.confirmation_phrase}"',
                    }
                probe = self.config.with_mode(target)
                try:
                    probe.assert_live_allowed()
                except ConfigError as exc:
                    return {"ok": False, "message": str(exc)}

            self.config.mode = target
            self._engine = None                       # rebuilt against the new broker
            self._last_tick = {}
            LOG.warning("Trading mode switched to %s.", target.value.upper())
            return {
                "ok": True,
                "mode": target.value,
                "message": describe_mode(self.config),
            }

    # ------------------------------------------------------------------ #
    # credentials and login
    # ------------------------------------------------------------------ #
    def save_credentials(self, api_key: str, api_secret: str, redirect_uri: str) -> dict[str, Any]:
        if not api_key or not api_secret:
            return {"ok": False, "message": "Both the API key and secret are required."}
        with self.lock:
            path = save_credentials(api_key, api_secret, redirect_uri)
            self.config = Config.load(mode=self.config.mode)
            self._engine = None
        return {"ok": True, "message": f"Credentials saved to {path}."}

    def complete_login(self, code: str, state: str = "") -> dict[str, Any]:
        """Exchange a code the dashboard's own /callback route received.

        The dashboard owns the redirect port, so it catches Upstox's redirect
        directly and stores the token -- there is nothing to copy by hand.
        """
        from ..upstox.auth import exchange_code

        try:
            token = exchange_code(self.config, code)
        except ConfigError as exc:
            self._login_state = {"status": "failed", "message": str(exc)}
            return {"ok": False, "message": str(exc)}

        with self.lock:
            self._engine = None
        self._login_state = {
            "status": "connected",
            "message": f"Connected. Token valid until {token.expires_at}.",
        }
        return {"ok": True, "message": self._login_state["message"]}

    # ------------------------------------------------------------------ #
    # risk controls
    # ------------------------------------------------------------------ #
    def panic(self, flatten: bool = True) -> dict[str, Any]:
        with self.lock:
            if self.running:
                self._stop.set()
            engine = self.engine()
            if engine is None:
                return {"ok": False, "message": "Could not build the engine."}
            engine.risk.engage_kill_switch("panic from dashboard")
            closed = engine.square_off_all("panic square-off") if flatten else []
        return {
            "ok": True,
            "message": f"Kill switch engaged; squared off {len(closed)} trade(s)."
            if flatten else "Kill switch engaged; positions left open.",
        }

    def resume(self) -> dict[str, Any]:
        self.config.risk.kill_switch_file.unlink(missing_ok=True)
        engine = self.engine(create=False)
        if engine is not None:
            engine.risk.state.halted = False
            engine.risk.state.halt_reason = ""
        LOG.warning("Kill switch released from the dashboard.")
        return {"ok": True, "message": "Kill switch released."}

    # ------------------------------------------------------------------ #
    # read models
    # ------------------------------------------------------------------ #
    def state(self) -> dict[str, Any]:
        config = self.config
        engine = self.engine(create=False)
        blocked = ""
        if config.mode.is_live:
            try:
                config.assert_live_allowed()
            except ConfigError as exc:
                blocked = str(exc)

        summary: dict[str, Any] = {}
        risk: dict[str, Any] = {}
        positions: list[dict[str, Any]] = []
        if engine is not None:
            try:
                summary = getattr(engine.broker, "summary", dict)()
            except UpstoxAPIError as exc:
                self._last_error = str(exc)
            risk = engine.risk.status()
            positions = [
                {
                    "track": t.track,
                    "description": t.description,
                    "lots": t.lots,
                    "quantity": t.quantity,
                    "entry": t.entry_price,
                    "stop": t.stop_loss,
                    "target": t.target,
                    "expiry": t.expiry,
                    "opened_at": t.opened_at.strftime("%d %b %H:%M"),
                    "legs": [
                        {
                            "symbol": leg.symbol,
                            "side": leg.side.value,
                            "role": leg.role,
                            "strike": leg.strike,
                            "delta": leg.delta,
                            "entry": leg.entry_price,
                        }
                        for leg in t.legs
                    ],
                }
                for t in engine.open_trades
                if t.is_open
            ]

        return {
            "mode": config.mode.value,
            "mode_description": describe_mode(config),
            "live_blocked": blocked,
            "live_armed": config.live.enabled,
            "dry_run": config.live.dry_run,
            "confirmation_phrase": config.live.confirmation_phrase,
            "running": self.running,
            "engine_ready": engine is not None,
            "last_tick": self._last_tick,
            "last_error": self._last_error,
            "upstox": {**token_status(config), "api_key": mask(config.upstox.api_key)},
            "login": self._login_state,
            "guards": self.guards(),
            "capital": {
                "track_a": config.track_a.capital,
                "track_b": config.track_b.capital,
                "total": config.track_a.capital + config.track_b.capital,
            },
            "limits": {
                "risk_per_trade": config.track_a.risk_per_trade,
                "max_outlay": config.track_a.max_outlay_per_trade,
                "daily_loss_limit_pct": config.risk.daily_loss_limit_pct,
                "live_max_order_value": config.risk.live_max_order_value,
                "lot_size": config.lot_size,
            },
            "tracks": {
                "a": {"enabled": config.track_a.enabled, "name": "Intraday Debit Momentum"},
                "b": {"enabled": config.track_b.enabled, "name": "Weekly Credit Decay"},
            },
            "broker": summary,
            "risk": risk,
            "positions": positions,
        }

    def guards(self) -> list[dict[str, Any]]:
        """The live-trading checklist, exactly as the UI renders it."""
        import os

        config = self.config
        token = token_status(config)
        confirm = os.getenv("UPSTOX_LIVE_CONFIRM", "").strip()
        deployed = config.track_a.capital + config.track_b.capital
        return [
            {
                "label": "Upstox credentials saved",
                "ok": token["has_credentials"],
                "detail": mask(config.upstox.api_key) or "not set",
            },
            {
                "label": "Access token valid",
                "ok": token["connected"],
                "detail": f"expires {token['expires_at'][:16]}" if token["connected"] else "not connected",
            },
            {
                "label": "live.enabled in config.yaml",
                "ok": config.live.enabled,
                "detail": "armed" if config.live.enabled else "set live.enabled: true",
            },
            {
                "label": "UPSTOX_LIVE_CONFIRM matches",
                "ok": confirm == config.live.confirmation_phrase,
                "detail": "matched" if confirm == config.live.confirmation_phrase else "not exported",
            },
            {
                "label": "Capital within live ceiling",
                "ok": deployed <= config.live.max_capital,
                "detail": f"Rs {deployed:,.0f} of Rs {config.live.max_capital:,.0f}",
            },
            {
                "label": "Kill switch clear",
                "ok": not config.risk.kill_switch_file.exists(),
                "detail": "clear" if not config.risk.kill_switch_file.exists() else "ENGAGED",
            },
        ]

    def journal_view(self, mode: str | None = None) -> dict[str, Any]:
        journal = Journal(self.config.journal_dir, mode or self.config.mode.value)
        rows = journal.rows()
        equity: list[dict[str, Any]] = []
        running = 0.0
        for index, row in enumerate(rows, start=1):
            try:
                running += float(row.get("Realized PnL (Rs)", 0) or 0)
            except ValueError:
                continue
            equity.append({"n": index, "date": row.get("Date", ""), "equity": round(running, 2)})

        return {
            "rows": rows[-100:],
            "equity": equity,
            "overall": evaluate(rows).as_dict(),
            "track_a": evaluate(rows, "Track A").as_dict(),
            "track_b": evaluate(rows, "Track B").as_dict(),
            "path": str(journal.path),
        }

    def report_markdown(self, mode: str | None = None) -> str:
        return comparison_report(Journal(self.config.journal_dir, mode or self.config.mode.value))
