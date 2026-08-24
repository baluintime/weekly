"""Upstox OAuth2 login and access-token storage.

Upstox access tokens are valid for a single trading day and expire daily at
03:30 IST, so the token file records an explicit expiry and the loader
refuses to hand back a stale token.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from ..config import Config, ConfigError

LOG = logging.getLogger(__name__)

AUTH_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_EXPIRY_IST = time(3, 30)


@dataclass
class Token:
    access_token: str
    user_id: str = ""
    issued_at: str = ""
    expires_at: str = ""

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return True
        return datetime.now(IST) >= datetime.fromisoformat(self.expires_at)


def next_expiry(now: datetime | None = None) -> datetime:
    """The upcoming 03:30 IST boundary at which an Upstox token dies."""
    now = now or datetime.now(IST)
    today_expiry = now.replace(
        hour=TOKEN_EXPIRY_IST.hour, minute=TOKEN_EXPIRY_IST.minute,
        second=0, microsecond=0,
    )
    return today_expiry if now < today_expiry else today_expiry + timedelta(days=1)


def build_login_url(config: Config, state: str = "nifty-options") -> str:
    if not config.upstox.api_key:
        raise ConfigError("UPSTOX_API_KEY is not set; cannot build a login URL.")
    query = urlencode(
        {
            "client_id": config.upstox.api_key,
            "redirect_uri": config.upstox.redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{AUTH_DIALOG_URL}?{query}"


def exchange_code(config: Config, code: str, timeout: int = 20) -> Token:
    """Trade the one-time authorization code for a daily access token."""
    response = requests.post(
        TOKEN_URL,
        headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "code": code,
            "client_id": config.upstox.api_key,
            "client_secret": config.upstox.api_secret,
            "redirect_uri": config.upstox.redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise ConfigError(f"Token exchange failed [{response.status_code}]: {response.text}")

    payload = response.json()
    token = Token(
        access_token=payload["access_token"],
        user_id=payload.get("user_id", ""),
        issued_at=datetime.now(IST).isoformat(),
        expires_at=next_expiry().isoformat(),
    )
    save_token(config.upstox.token_path, token)
    return token


def save_token(path: Path, token: Token) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(token), indent=2))
    path.chmod(0o600)
    LOG.info("Saved Upstox access token to %s (expires %s)", path, token.expires_at)


def load_token(config: Config) -> Token | None:
    """Return a usable token from the environment or the token file."""
    import os

    env_token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if env_token:
        return Token(
            access_token=env_token,
            issued_at=datetime.now(IST).isoformat(),
            expires_at=next_expiry().isoformat(),
        )

    path = config.upstox.token_path
    if not path.exists():
        return None
    token = Token(**json.loads(path.read_text()))
    if token.is_expired:
        LOG.warning("Stored Upstox token expired at %s; re-login required.", token.expires_at)
        return None
    return token


def require_token(config: Config) -> Token:
    token = load_token(config)
    if token is None:
        raise ConfigError(
            "No valid Upstox access token. Run:  python -m nifty_options login"
        )
    return token
