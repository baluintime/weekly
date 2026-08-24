"""Console + rotating file logging, with live orders visually flagged."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "nifty_options.log", maxBytes=5_000_000, backupCount=5
        )
        file_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
