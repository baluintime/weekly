"""Nifty 50 dual-track options framework on Upstox.

Track A -- intraday debit momentum; Track B -- weekly credit decay (iron
condor). Runs in paper mode by default and switches to actual trading through
:func:`nifty_options.brokers.factory.build_broker`.
"""

from .config import Config, TradingMode

__version__ = "1.0.0"
__all__ = ["Config", "TradingMode"]
