"""Credential storage: entered once, never re-pasted."""

from __future__ import annotations

import os

import pytest

from nifty_options.config import Config
from nifty_options.credentials import load_env, mask, parse_env, save_credentials


def test_parse_handles_quotes_exports_and_comments():
    values = parse_env(
        '# comment\n'
        'export UPSTOX_API_KEY="abc123"\n'
        "UPSTOX_API_SECRET='sh h'\n"
        "\n"
        "UPSTOX_LIVE_CONFIRM=I UNDERSTAND THIS TRADES REAL MONEY\n"
        "junk line without equals\n"
    )
    assert values["UPSTOX_API_KEY"] == "abc123"
    assert values["UPSTOX_API_SECRET"] == "sh h"
    assert values["UPSTOX_LIVE_CONFIRM"] == "I UNDERSTAND THIS TRADES REAL MONEY"
    assert "junk line without equals" not in values


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_API_KEY", raising=False)
    path = tmp_path / ".env"
    save_credentials("key-123", "secret-456", "http://127.0.0.1:5000/callback", path)
    assert parse_env(path.read_text())["UPSTOX_API_KEY"] == "key-123"

    monkeypatch.delenv("UPSTOX_API_KEY", raising=False)
    load_env(path)
    assert os.environ["UPSTOX_API_KEY"] == "key-123"


def test_file_is_owner_only(tmp_path):
    path = save_credentials("k", "s", path=tmp_path / ".env")
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_saving_preserves_unrelated_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text("UPSTOX_TRADING_MODE=paper\nLOG_LEVEL=DEBUG\n")
    save_credentials("k", "s", path=path)
    values = parse_env(path.read_text())
    assert values["UPSTOX_TRADING_MODE"] == "paper"
    assert values["LOG_LEVEL"] == "DEBUG"
    assert values["UPSTOX_API_KEY"] == "k"


def test_real_environment_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("UPSTOX_API_KEY=from-file\n")
    monkeypatch.setenv("UPSTOX_API_KEY", "from-shell")
    load_env(path)
    assert os.environ["UPSTOX_API_KEY"] == "from-shell"


def test_override_forces_the_file_value(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("UPSTOX_API_KEY=from-file\n")
    monkeypatch.setenv("UPSTOX_API_KEY", "from-shell")
    load_env(path, override=True)
    assert os.environ["UPSTOX_API_KEY"] == "from-file"


def test_config_picks_up_stored_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_API_KEY", raising=False)
    monkeypatch.delenv("UPSTOX_API_SECRET", raising=False)
    monkeypatch.setattr("nifty_options.config.load_env", lambda: None)
    monkeypatch.setenv("UPSTOX_API_KEY", "stored-key")
    monkeypatch.setenv("UPSTOX_API_SECRET", "stored-secret")
    config = Config.load()
    assert config.upstox.api_key == "stored-key"


@pytest.mark.parametrize(
    "value,expected",
    [("", ""), ("short", "*****"), ("abcd1234efgh5678", "abcd...5678")],
)
def test_mask_never_reveals_the_middle(value, expected):
    assert mask(value) == expected
