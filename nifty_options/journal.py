"""Trade journal in the tracking-sheet format from the framework (section 5).

Columns match the document exactly:

    Date | Track | Strategy / Legs | Entry | Exit | Net Points | Realized PnL

A `mode` column is appended so paper rows and live rows stay distinguishable
in the same evaluation, and :func:`evaluate` produces the section 4 metrics
(win rate, Sharpe, max drawdown) used to compare the two tracks.
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

LOG = logging.getLogger(__name__)

COLUMNS = [
    "Date", "Track", "Strategy / Legs", "Entry", "Exit",
    "Net Points", "Realized PnL (Rs)", "Mode", "Lots", "Charges", "Exit Reason",
    "Entry Time", "Exit Time",
]


@dataclass
class JournalEntry:
    date: str
    track: str
    strategy: str
    entry: float
    exit: float
    net_points: float
    realized_pnl: float
    mode: str = "paper"
    lots: int = 1
    charges: float = 0.0
    exit_reason: str = ""
    entry_time: str = ""
    exit_time: str = ""

    def as_row(self) -> list[Any]:
        return [
            self.date,
            self.track,
            self.strategy,
            f"{self.entry:.2f}",
            f"{self.exit:.2f}",
            f"{self.net_points:+.2f}",
            f"{self.realized_pnl:+.2f}",
            self.mode,
            self.lots,
            f"{self.charges:.2f}",
            self.exit_reason,
            self.entry_time,
            self.exit_time,
        ]


class Journal:
    """Append-only CSV, one file per trading mode."""

    def __init__(self, directory: Path, mode: str = "paper"):
        self.directory = Path(directory)
        self.mode = mode
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"tracking_sheet_{mode}.csv"
        if not self.path.exists():
            with self.path.open("w", newline="") as handle:
                csv.writer(handle).writerow(COLUMNS)

    def record(self, entry: JournalEntry) -> None:
        entry.mode = entry.mode or self.mode
        with self.path.open("a", newline="") as handle:
            csv.writer(handle).writerow(entry.as_row())
        LOG.info(
            "Journalled %s %s  %.2f -> %.2f  (%+.2f pts, Rs %+.2f)",
            entry.track, entry.strategy, entry.entry, entry.exit,
            entry.net_points, entry.realized_pnl,
        )

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="") as handle:
            return list(csv.DictReader(handle))

    def to_markdown(self, limit: int | None = None) -> str:
        rows = self.rows()
        if limit:
            rows = rows[-limit:]
        if not rows:
            return "_No trades recorded yet._"
        head = COLUMNS[:7]
        lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for row in rows:
            lines.append("| " + " | ".join(str(row.get(col, "")) for col in head) + " |")
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
# evaluation (section 4 comparison matrix)
# ---------------------------------------------------------------------- #
@dataclass
class TrackMetrics:
    track: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    charges: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def evaluate(rows: Iterable[dict[str, str]], track: str | None = None) -> TrackMetrics:
    pnls: list[float] = []
    charges = 0.0
    for row in rows:
        if track and row.get("Track", "") != track:
            continue
        try:
            pnls.append(float(row.get("Realized PnL (Rs)", 0) or 0))
            charges += float(row.get("Charges", 0) or 0)
        except ValueError:
            continue

    metrics = TrackMetrics(track=track or "ALL", trades=len(pnls), charges=charges)
    if not pnls:
        return metrics

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    metrics.wins = len(wins)
    metrics.losses = len(losses)
    metrics.gross_pnl = sum(pnls)
    metrics.best = max(pnls)
    metrics.worst = min(pnls)
    metrics.win_rate = len(wins) / len(pnls) * 100.0
    metrics.avg_win = sum(wins) / len(wins) if wins else 0.0
    metrics.avg_loss = sum(losses) / len(losses) if losses else 0.0
    metrics.expectancy = sum(pnls) / len(pnls)
    loss_sum = abs(sum(losses))
    metrics.profit_factor = (sum(wins) / loss_sum) if loss_sum else float("inf") if wins else 0.0
    metrics.sharpe = _sharpe(pnls)
    metrics.max_drawdown = _max_drawdown(pnls)
    return metrics


def _sharpe(pnls: Sequence[float], periods_per_year: int = 252) -> float:
    """Per-trade Sharpe, annualised. Risk-free rate treated as 0."""
    if len(pnls) < 2:
        return 0.0
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return 0.0
    return (mean / stdev) * math.sqrt(periods_per_year)


def _max_drawdown(pnls: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def comparison_report(journal: Journal) -> str:
    """Track A vs Track B, in the shape of the document's comparison matrix."""
    rows = journal.rows()
    tracks = ["Track A", "Track B"]
    metrics = [evaluate(rows, track) for track in tracks]
    overall = evaluate(rows)

    lines = [
        f"# Nifty 50 Options -- {journal.mode.upper()} results",
        "",
        f"Trades recorded: {overall.trades}   Net PnL: Rs {overall.gross_pnl:,.2f}   "
        f"Charges: Rs {overall.charges:,.2f}",
        "",
        "| Metric | Track A: Intraday Debit | Track B: Weekly Condor |",
        "|---|---|---|",
    ]
    fields = [
        ("Trades", "trades", "{:d}"),
        ("Win rate", "win_rate", "{:.1f}%"),
        ("Net PnL (Rs)", "gross_pnl", "{:,.2f}"),
        ("Avg win (Rs)", "avg_win", "{:,.2f}"),
        ("Avg loss (Rs)", "avg_loss", "{:,.2f}"),
        ("Expectancy (Rs)", "expectancy", "{:,.2f}"),
        ("Profit factor", "profit_factor", "{:.2f}"),
        ("Sharpe (annualised)", "sharpe", "{:.2f}"),
        ("Max drawdown (Rs)", "max_drawdown", "{:,.2f}"),
        ("Charges (Rs)", "charges", "{:,.2f}"),
    ]
    for label, attr, fmt in fields:
        cells = []
        for metric in metrics:
            value = getattr(metric, attr)
            cells.append(fmt.format(value) if value not in (float("inf"),) else "inf")
        lines.append(f"| {label} | {cells[0]} | {cells[1]} |")

    lines += ["", "## Recent trades", "", journal.to_markdown(limit=20)]
    return "\n".join(lines)
