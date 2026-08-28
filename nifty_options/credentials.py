"""Read and write the local `.env` credential file.

Upstox API keys are entered once -- from the dashboard or the CLI -- and kept
in a 0600 file next to the config. Nothing is ever pasted twice, and the
secret is never sent back to the browser.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

LOG = logging.getLogger(__name__)

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Load `.env` into the process environment.

    Real environment variables win by default, so an operator can always
    override the stored file for one run without editing it.
    """
    path = path or ENV_PATH
    if not path.exists():
        return {}
    values = parse_env(path.read_text())
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def save_credentials(
    api_key: str,
    api_secret: str,
    redirect_uri: str = "",
    path: Path | None = None,
) -> Path:
    """Persist credentials to `.env`, preserving anything already in it."""
    path = path or ENV_PATH
    existing = parse_env(path.read_text()) if path.exists() else {}

    if api_key:
        existing["UPSTOX_API_KEY"] = api_key.strip()
    if api_secret:
        existing["UPSTOX_API_SECRET"] = api_secret.strip()
    if redirect_uri:
        existing["UPSTOX_REDIRECT_URI"] = redirect_uri.strip()

    body = "\n".join(f"{key}={value}" for key, value in sorted(existing.items()))
    path.write_text("# Written by nifty_options -- keep this file private.\n" + body + "\n")
    path.chmod(0o600)

    for key in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"):
        if key in existing:
            os.environ[key] = existing[key]

    LOG.info("Saved Upstox credentials to %s", path)
    return path


def mask(value: str, keep: int = 4) -> str:
    """`abcd1234wxyz` -> `abcd...wxyz`; used for display only."""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"
