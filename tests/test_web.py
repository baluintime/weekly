"""Dashboard: HTTP surface, security posture, and the browser OAuth capture."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from nifty_options.config import Config, TradingMode
from nifty_options.upstox.auth import Token, callback_endpoint
from nifty_options.web.controller import DashboardController, LogTail
from nifty_options.web.server import build_server

from .conftest import FakeUpstoxClient
from .test_mode_switch import arm_live


@pytest.fixture
def dashboard(config, fake_client):
    """A live server on an ephemeral port, with a seeded paper engine."""
    controller = DashboardController(config)
    from nifty_options.brokers import build_broker
    from nifty_options.engine import Engine

    controller._engine = Engine(config, build_broker(config, fake_client), fake_client)
    server, key = build_server(config, host="127.0.0.1", port=0, controller=controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, key, controller
    server.shutdown()
    server.server_close()


def get(base, path, host=None):
    request = urllib.request.Request(base + path)
    if host:
        request.add_header("Host", host)
    return urllib.request.urlopen(request, timeout=5)


def post(base, path, body=None, key=None):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json", **({"X-Dashboard-Key": key} if key else {})},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=5)


# ---------------------------------------------------------------------- #
# serving
# ---------------------------------------------------------------------- #
def test_index_is_served_with_the_session_key_injected(dashboard):
    base, key, _ = dashboard
    body = get(base, "/").read().decode()
    assert key in body
    assert "__SESSION_KEY__" not in body


def test_static_assets_are_served(dashboard):
    base, _, _ = dashboard
    assert get(base, "/app.js").status == 200
    assert get(base, "/styles.css").status == 200


def test_state_endpoint_describes_the_mode(dashboard):
    base, _, _ = dashboard
    state = json.loads(get(base, "/api/state").read())
    assert state["mode"] == "paper"
    assert "nothing is sent to the exchange" in state["mode_description"]
    assert len(state["guards"]) == 6


def test_state_never_leaks_the_access_token(dashboard, monkeypatch):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "super-secret-token")
    base, _, _ = dashboard
    body = get(base, "/api/state").read().decode()
    assert "super-secret-token" not in body


def test_api_key_is_masked_in_state(dashboard, monkeypatch):
    base, _, controller = dashboard
    controller.config.upstox.api_key = "abcd1234efgh5678"
    body = json.loads(get(base, "/api/state").read())
    assert body["upstox"]["api_key"] == "abcd...5678"
    assert "abcd1234efgh5678" not in json.dumps(body)


def test_journal_endpoint_returns_metrics(dashboard):
    base, _, _ = dashboard
    payload = json.loads(get(base, "/api/journal").read())
    assert {"rows", "equity", "overall", "track_a", "track_b"} <= set(payload)


# ---------------------------------------------------------------------- #
# security
# ---------------------------------------------------------------------- #
def test_post_without_the_session_key_is_refused(dashboard):
    base, _, controller = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(base, "/api/mode", {"mode": "live"})
    assert excinfo.value.code == 403
    assert controller.config.mode is TradingMode.PAPER


def test_post_with_a_wrong_key_is_refused(dashboard):
    base, _, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(base, "/api/engine/start", {}, key="not-the-key")
    assert excinfo.value.code == 403


def test_foreign_host_header_is_rejected(dashboard):
    """Blocks DNS rebinding: a page on another origin cannot drive the engine."""
    base, key, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(base, "/api/state", host="attacker.example.com")
    assert excinfo.value.code == 403


def test_static_route_cannot_escape_the_asset_directory(dashboard):
    base, _, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(base, "/../config.yaml")
    assert excinfo.value.code in (400, 404)


# ---------------------------------------------------------------------- #
# mode switching over HTTP
# ---------------------------------------------------------------------- #
def test_switch_to_live_needs_the_exact_phrase(dashboard):
    base, key, controller = dashboard
    result = json.loads(post(base, "/api/mode", {"mode": "live", "confirmation": "yes"}, key).read())
    assert result["ok"] is False
    assert "confirmation phrase" in result["message"]
    assert controller.config.mode is TradingMode.PAPER


def test_switch_to_live_still_needs_the_config_guards(dashboard):
    base, key, controller = dashboard
    phrase = controller.config.live.confirmation_phrase
    result = json.loads(
        post(base, "/api/mode", {"mode": "live", "confirmation": phrase}, key).read()
    )
    assert result["ok"] is False
    assert "live.enabled" in result["message"]
    assert controller.config.mode is TradingMode.PAPER


def test_switch_to_live_succeeds_once_fully_armed(dashboard, monkeypatch):
    base, key, controller = dashboard
    arm_live(controller.config, monkeypatch)
    phrase = controller.config.live.confirmation_phrase
    result = json.loads(
        post(base, "/api/mode", {"mode": "live", "confirmation": phrase}, key).read()
    )
    assert result["ok"] is True
    assert controller.config.mode is TradingMode.LIVE


def test_switching_back_to_paper_needs_no_phrase(dashboard, monkeypatch):
    base, key, controller = dashboard
    arm_live(controller.config, monkeypatch)
    controller.config.mode = TradingMode.LIVE
    result = json.loads(post(base, "/api/mode", {"mode": "paper"}, key).read())
    assert result["ok"] is True
    assert controller.config.mode is TradingMode.PAPER


def test_mode_cannot_change_while_the_engine_runs(dashboard, monkeypatch):
    base, key, controller = dashboard
    monkeypatch.setattr(type(controller), "running", property(lambda self: True))
    result = json.loads(post(base, "/api/mode", {"mode": "paper"}, key).read())
    assert result["ok"] is False
    assert "Stop the engine" in result["message"]


def test_switching_mode_rebuilds_the_broker(dashboard, monkeypatch):
    base, key, controller = dashboard
    arm_live(controller.config, monkeypatch)
    before = controller.engine()
    post(base, "/api/mode", {"mode": "live", "confirmation": controller.config.live.confirmation_phrase}, key)
    assert controller._engine is not before or controller._engine is None


# ---------------------------------------------------------------------- #
# OAuth capture -- the "no copy-paste" path
# ---------------------------------------------------------------------- #
def test_callback_endpoint_parses_a_redirect_uri():
    assert callback_endpoint("http://127.0.0.1:5000/callback") == ("127.0.0.1", 5000, "/callback")


def test_login_route_redirects_to_upstox(dashboard, monkeypatch):
    base, _, controller = dashboard
    controller.config.upstox.api_key = "test-key"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        no_redirect_opener().open(base + "/auth/login", timeout=5)
    assert excinfo.value.code == 302
    location = excinfo.value.headers["Location"]
    assert location.startswith("https://api.upstox.com/v2/login/authorization/dialog")
    assert "client_id=test-key" in location


def no_redirect_opener():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    return urllib.request.build_opener(NoRedirect)


def start_login(base, controller) -> str:
    """Walk the real /auth/login step and return the armed state value."""
    controller.config.upstox.api_key = "test-key"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        no_redirect_opener().open(base + "/auth/login", timeout=5)
    return parse_qs(urlparse(excinfo.value.headers["Location"]).query)["state"][0]


def test_callback_exchanges_the_code_without_any_manual_step(dashboard, monkeypatch):
    """The redirect lands here and the token is stored -- nothing is pasted."""
    base, _, controller = dashboard
    captured: dict[str, str] = {}

    def fake_exchange(config, code, timeout=20):
        captured["code"] = code
        return Token(access_token="tok", user_id="U1", expires_at="2026-08-29T03:30:00+05:30")

    monkeypatch.setattr("nifty_options.upstox.auth.exchange_code", fake_exchange)
    state = start_login(base, controller)

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        no_redirect_opener().open(base + f"/callback?code=AUTH-CODE-123&state={state}", timeout=5)
    assert excinfo.value.code == 302
    assert excinfo.value.headers["Location"] == "/?connected=1"
    assert captured["code"] == "AUTH-CODE-123"
    assert controller._login_state["status"] == "connected"


def test_callback_rejects_a_mismatched_state(dashboard, monkeypatch):
    base, _, controller = dashboard
    called = []
    monkeypatch.setattr(
        "nifty_options.upstox.auth.exchange_code", lambda *a, **k: called.append(1)
    )
    start_login(base, controller)

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        no_redirect_opener().open(base + "/callback?code=X&state=wrong", timeout=5)
    assert excinfo.value.headers["Location"] == "/?error=1"
    assert called == []


def test_unsolicited_callback_is_refused(dashboard, monkeypatch):
    """A replayed or forged callback with no login in flight exchanges nothing."""
    base, _, _ = dashboard
    called = []
    monkeypatch.setattr(
        "nifty_options.upstox.auth.exchange_code", lambda *a, **k: called.append(1)
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        no_redirect_opener().open(base + "/callback?code=STOLEN", timeout=5)
    assert excinfo.value.headers["Location"] == "/?error=1"
    assert called == []


def test_a_state_cannot_be_replayed(dashboard, monkeypatch):
    """Each armed state is consumed by the first callback that uses it."""
    base, _, controller = dashboard
    monkeypatch.setattr(
        "nifty_options.upstox.auth.exchange_code",
        lambda config, code, timeout=20: Token(access_token="tok", expires_at="2026-08-29T03:30:00+05:30"),
    )
    state = start_login(base, controller)
    opener = no_redirect_opener()
    with pytest.raises(urllib.error.HTTPError) as first:
        opener.open(base + f"/callback?code=A&state={state}", timeout=5)
    assert first.value.headers["Location"] == "/?connected=1"
    with pytest.raises(urllib.error.HTTPError) as second:
        opener.open(base + f"/callback?code=A&state={state}", timeout=5)
    assert second.value.headers["Location"] == "/?error=1"


def test_callback_without_a_code_reports_an_error(dashboard):
    base, _, _ = dashboard
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        no_redirect_opener().open(base + "/callback?error=access_denied", timeout=5)
    assert excinfo.value.headers["Location"] == "/?error=1"


# ---------------------------------------------------------------------- #
# controller behaviour
# ---------------------------------------------------------------------- #
def test_credentials_are_saved_and_reloaded(dashboard, tmp_path, monkeypatch):
    _, _, controller = dashboard
    monkeypatch.setattr("nifty_options.web.controller.save_credentials",
                        lambda k, s, r: tmp_path / ".env")
    result = controller.save_credentials("key", "secret", "http://127.0.0.1:5000/callback")
    assert result["ok"] is True


def test_credentials_require_both_fields(dashboard):
    _, _, controller = dashboard
    assert controller.save_credentials("key", "", "")["ok"] is False


def test_login_route_needs_credentials_first(dashboard):
    base, _, controller = dashboard
    controller.config.upstox.api_key = ""
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        no_redirect_opener().open(base + "/auth/login", timeout=5)
    assert excinfo.value.code == 400


def test_panic_engages_the_kill_switch(dashboard):
    base, key, controller = dashboard
    result = json.loads(post(base, "/api/panic", {"flatten": False}, key).read())
    assert result["ok"] is True
    assert controller.config.risk.kill_switch_file.exists()

    resumed = json.loads(post(base, "/api/resume", {}, key).read())
    assert resumed["ok"] is True
    assert not controller.config.risk.kill_switch_file.exists()


def test_tick_endpoint_runs_one_evaluation(dashboard):
    base, key, _ = dashboard
    result = json.loads(post(base, "/api/engine/tick", {}, key).read())
    assert result["ok"] is True
    assert result["report"]["status"] == "ok"


def test_engine_start_and_stop(dashboard):
    base, key, controller = dashboard
    started = json.loads(post(base, "/api/engine/start", {"poll": 1}, key).read())
    assert started["ok"] is True
    assert controller.running

    again = json.loads(post(base, "/api/engine/start", {"poll": 1}, key).read())
    assert again["ok"] is False                     # no double-start

    stopped = json.loads(post(base, "/api/engine/stop", {}, key).read())
    assert stopped["ok"] is True
    assert not controller.running


def test_log_tail_streams_incrementally(dashboard):
    """The controller raises the root level so INFO reaches the activity panel."""
    base, _, controller = dashboard
    import logging

    assert logging.getLogger().level <= logging.INFO
    logging.getLogger("nifty_options.test").info("first")
    logging.getLogger("nifty_options.test").warning("second")
    payload = json.loads(get(base, "/api/logs?after=0").read())
    messages = [entry["message"] for entry in payload["logs"]]
    assert "first" in messages and "second" in messages

    last = payload["logs"][-1]["id"]
    assert json.loads(get(base, f"/api/logs?after={last}").read())["logs"] == []


def test_log_tail_is_bounded():
    tail = LogTail(capacity=5)
    import logging

    for i in range(20):
        tail.emit(logging.LogRecord("n", logging.INFO, "p", 1, f"m{i}", None, None))
    assert len(tail.records) == 5
    assert tail.records[-1]["message"] == "m19"
