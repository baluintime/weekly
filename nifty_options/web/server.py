"""Local dashboard HTTP server (stdlib only).

Security posture: this page can arm real trading, so the server binds to
loopback, pins the Host header (blocks DNS-rebinding), and requires a
per-session key on every mutating request. The key is minted at startup and
embedded in the page the server itself serves, so other origins cannot read
it -- but it is not a substitute for keeping the port off a shared machine.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..config import Config, ConfigError
from ..upstox.auth import build_login_url, callback_endpoint
from .controller import DashboardController

LOG = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NiftyOptionsDashboard/1.0"
    controller: DashboardController
    session_key: str
    callback_path: str = "/callback"
    oauth_state: str = ""

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _authorised(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-Dashboard-Key", ""), type(self).session_key
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _static(self, name: str) -> None:
        path = (STATIC_DIR / name).resolve()
        if not path.is_file() or STATIC_DIR.resolve() not in path.parents:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        body = path.read_bytes()
        if path.name == "index.html":
            body = body.replace(b"__SESSION_KEY__", type(self).session_key.encode())
        self._send(200, body, CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("http: " + fmt, *args)

    # ------------------------------------------------------------------ #
    # routing
    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:                       # noqa: N802 (stdlib API)
        if not self._host_allowed():
            self._send(403, b"Forbidden host", "text/plain; charset=utf-8")
            return

        route = urlparse(self.path)
        path = route.path.rstrip("/") or "/"
        controller = type(self).controller

        if path == "/":
            return self._static("index.html")
        if path in ("/app.js", "/styles.css"):
            return self._static(path.lstrip("/"))

        # OAuth redirect target -- the whole point of the automatic flow.
        if path == type(self).callback_path.rstrip("/"):
            return self._handle_callback(route)

        if path == "/auth/login":
            state = secrets.token_urlsafe(16)
            type(self).oauth_state = state
            try:
                url = build_login_url(controller.config, state=state)
            except ConfigError as exc:
                return self._json({"ok": False, "message": str(exc)}, 400)
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
            return None

        if path == "/api/state":
            return self._json(controller.state())
        if path == "/api/journal":
            mode = parse_qs(route.query).get("mode", [None])[0]
            return self._json(controller.journal_view(mode))
        if path == "/api/report":
            return self._send(
                200, controller.report_markdown().encode(), "text/markdown; charset=utf-8"
            )
        if path == "/api/logs":
            after = int(parse_qs(route.query).get("after", ["0"])[0] or 0)
            return self._json({"logs": controller.logs.since(after)})

        self._send(404, b"Not found", "text/plain; charset=utf-8")
        return None

    def _handle_callback(self, route) -> None:
        try:
            self._exchange_callback(route)
        except Exception as exc:                    # a failed login is not fatal
            LOG.exception("Upstox callback failed")
            type(self).controller._login_state = {"status": "failed", "message": str(exc)}
            self.send_response(302)
            self.send_header("Location", "/?error=1")
            self.end_headers()

    def _exchange_callback(self, route) -> None:
        params = parse_qs(route.query)
        code = params.get("code", [""])[0]
        state = params.get("state", [""])[0]
        error = params.get("error_description", params.get("error", [""]))[0]
        controller = type(self).controller

        if error or not code:
            message = error or "Upstox did not return an authorization code."
        elif not type(self).oauth_state or state != type(self).oauth_state:
            # The login must have started at this dashboard's /auth/login, so an
            # unsolicited or replayed callback is refused.
            message = "Unexpected login callback -- start the connection from the dashboard."
        else:
            type(self).oauth_state = ""
            result = controller.complete_login(code, state)
            message = "" if result["ok"] else result["message"]

        # Bounce back to the dashboard either way; it polls for the outcome.
        self.send_response(302)
        self.send_header("Location", "/?connected=1" if not message else "/?error=1")
        self.end_headers()

    def do_POST(self) -> None:                      # noqa: N802 (stdlib API)
        if not self._host_allowed():
            self._send(403, b"Forbidden host", "text/plain; charset=utf-8")
            return
        if not self._authorised():
            self._json({"ok": False, "message": "Missing or invalid dashboard key."}, 403)
            return

        path = urlparse(self.path).path.rstrip("/") or "/"
        controller = type(self).controller
        body = self._body()

        actions: dict[str, Callable[[], dict[str, Any]]] = {
            "/api/mode": lambda: controller.switch_mode(
                body.get("mode", "paper"),
                body.get("confirmation", ""),
                body.get("shadow"),
            ),
            "/api/engine/start": lambda: controller.start(int(body.get("poll", 60))),
            "/api/engine/stop": controller.stop,
            "/api/engine/tick": controller.tick_once,
            "/api/panic": lambda: controller.panic(bool(body.get("flatten", True))),
            "/api/resume": controller.resume,
            "/api/credentials": lambda: controller.save_credentials(
                body.get("api_key", "").strip(),
                body.get("api_secret", "").strip(),
                body.get("redirect_uri", "").strip(),
            ),
        }

        action = actions.get(path)
        if action is None:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return

        try:
            self._json(action())
        except ConfigError as exc:
            self._json({"ok": False, "message": str(exc)}, 400)
        except Exception as exc:                    # never take the server down
            LOG.exception("Dashboard action %s failed", path)
            self._json({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, 500)


def build_server(
    config: Config,
    host: str | None = None,
    port: int | None = None,
    controller: DashboardController | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    """Bind the dashboard on the app's redirect URI so it owns /callback."""
    redirect_host, redirect_port, callback_path = callback_endpoint(config.upstox.redirect_uri)
    host = host or redirect_host
    port = redirect_port if port is None else port
    session_key = secrets.token_urlsafe(24)

    controller = controller or DashboardController(config)
    handler = type(
        "_BoundDashboardHandler",
        (DashboardHandler,),
        {
            "controller": controller,
            "session_key": session_key,
            "callback_path": callback_path,
            "oauth_state": "",
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server, session_key


def serve(
    config: Config,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    server, _ = build_server(config, host, port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}/"

    print(f"\n  Nifty 50 options dashboard  ->  {url}")
    print(f"  Mode: {config.mode.value.upper()}   (switch it from the page)")
    print("  Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.shutdown()
        server.server_close()
