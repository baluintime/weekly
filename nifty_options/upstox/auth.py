"""Upstox OAuth2 login and access-token storage.

Upstox access tokens are valid for a single trading day and expire daily at
03:30 IST, so the token file records an explicit expiry and the loader
refuses to hand back a stale token.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time as time_module
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

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


# ---------------------------------------------------------------------- #
# Automatic browser login -- no copy-pasting of codes or tokens
# ---------------------------------------------------------------------- #
SUCCESS_PAGE = """<!doctype html><meta charset="utf-8">
<title>Upstox connected</title>
<style>
  :root{color-scheme:light dark}
  body{margin:0;min-height:100vh;display:grid;place-items:center;
       font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       background:#0b0f14;color:#e6edf3}
  .card{max-width:30rem;padding:2.5rem;border-radius:1rem;text-align:center;
        background:#131a22;border:1px solid #243040}
  .tick{width:3.5rem;height:3.5rem;margin:0 auto 1.25rem;border-radius:50%;
        display:grid;place-items:center;background:#10352a;color:#3fb950;font-size:1.75rem}
  h1{margin:0 0 .5rem;font-size:1.25rem}
  p{margin:0;color:#8b98a5}
  code{background:#0b0f14;padding:.15rem .4rem;border-radius:.3rem;color:#79c0ff}
</style>
<div class="card">
  <div class="tick">&#10003;</div>
  <h1>{heading}</h1>
  <p>{message}</p>
</div>
<script>setTimeout(function(){window.close()},2500)</script>
"""


@dataclass
class CallbackResult:
    code: str = ""
    error: str = ""
    state: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.code) and not self.error


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures Upstox's ?code= redirect."""

    callback_path = "/callback"
    expected_state = ""
    result: CallbackResult | None = None

    def do_GET(self) -> None:                       # noqa: N802 (stdlib API)
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != self.callback_path.rstrip("/"):
            self.send_error(404, "Not the Upstox callback path")
            return

        params = parse_qs(parsed.query)
        result = CallbackResult(
            code=params.get("code", [""])[0],
            error=params.get("error_description", params.get("error", [""]))[0],
            state=params.get("state", [""])[0],
        )
        if result.ok and self.expected_state and result.state != self.expected_state:
            result = CallbackResult(error="State mismatch -- possible CSRF; login refused.")

        type(self).result = result
        if result.ok:
            body = SUCCESS_PAGE.replace("{heading}", "Upstox connected").replace(
                "{message}", "The access token was saved automatically. "
                "You can close this tab and return to the dashboard."
            )
        else:
            body = (
                SUCCESS_PAGE.replace("{heading}", "Login failed")
                .replace("{message}", result.error or "No authorization code was returned.")
                .replace("#3fb950", "#f85149")
                .replace("#10352a", "#3d1418")
                .replace("&#10003;", "!")
            )
        encoded = body.encode()
        self.send_response(200 if result.ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("callback: " + fmt, *args)


def callback_endpoint(redirect_uri: str) -> tuple[str, int, str]:
    """Split a redirect URI into (host, port, path) for the local listener."""
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port, parsed.path or "/callback"


def run_login_flow(
    config: Config,
    timeout: int = 300,
    open_browser: bool = True,
) -> Token:
    """Full browser OAuth with no manual steps.

    Starts a one-shot listener on the app's registered redirect URI, opens the
    Upstox consent screen, catches the ?code= redirect and exchanges it for a
    token. Nothing is ever copied by hand.
    """
    host, port, path = callback_endpoint(config.upstox.redirect_uri)
    if host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        raise ConfigError(
            f"Automatic login needs a loopback redirect URI; got {config.upstox.redirect_uri!r}. "
            "Register http://127.0.0.1:5000/callback on your Upstox app, or use --manual."
        )

    state = secrets.token_urlsafe(16)
    handler = type(
        "_BoundCallbackHandler",
        (_CallbackHandler,),
        {"callback_path": path, "expected_state": state, "result": None},
    )

    try:
        server = HTTPServer((host, port), handler)
    except OSError as exc:
        raise ConfigError(
            f"Could not listen on {host}:{port} for the Upstox callback ({exc}). "
            "The dashboard may already be running -- use its Connect button instead."
        ) from exc

    server.timeout = timeout
    url = build_login_url(config, state=state)
    LOG.info("Waiting for the Upstox callback on %s:%s%s", host, port, path)

    with server:
        if open_browser:
            opened = webbrowser.open(url)
            if not opened:
                print("Could not open a browser automatically. Visit:\n\n  " + url + "\n")
        else:
            print("Open this URL to authorise:\n\n  " + url + "\n")

        deadline = time_module.monotonic() + timeout
        while handler.result is None:
            if time_module.monotonic() > deadline:
                raise ConfigError(
                    f"Timed out after {timeout}s waiting for the Upstox callback."
                )
            server.handle_request()

    result = handler.result
    if not result.ok:
        raise ConfigError(f"Upstox login failed: {result.error or 'no code returned'}")

    return exchange_code(config, result.code)


def token_status(config: Config) -> dict[str, object]:
    """Connection state for the dashboard -- never exposes the token itself."""
    token = load_token(config)
    return {
        "connected": token is not None,
        "user_id": token.user_id if token else "",
        "expires_at": token.expires_at if token else "",
        "source": "environment" if os.getenv("UPSTOX_ACCESS_TOKEN") else "token file",
        "has_credentials": bool(config.upstox.api_key and config.upstox.api_secret),
        "redirect_uri": config.upstox.redirect_uri,
    }
