"""CLI surface: mode reporting, exit codes and the live confirmation gate."""

from __future__ import annotations

import pytest

from nifty_options import cli
from nifty_options.config import Config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point the CLI at a throwaway config so tests never touch real state."""
    source = Config.load()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "mode: paper\n"
        f"journal_dir: {tmp_path / 'journal'}\n"
        f"state_dir: {tmp_path / 'state'}\n"
        "risk:\n"
        f"  kill_switch_file: {tmp_path / 'KILL_SWITCH'}\n"
        "upstox:\n"
        f"  token_path: {tmp_path / 'token.json'}\n"
    )
    monkeypatch.setenv("NIFTY_CONFIG", str(config_file))
    monkeypatch.delenv("UPSTOX_TRADING_MODE", raising=False)
    monkeypatch.delenv("UPSTOX_LIVE_CONFIRM", raising=False)
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    return config_file


def test_mode_command_reports_paper(capsys):
    assert cli.main(["mode"]) == 0
    out = capsys.readouterr().out
    assert "PAPER" in out
    assert "simulated fills" in out


def test_mode_command_explains_how_to_go_live(capsys):
    cli.main(["mode"])
    out = capsys.readouterr().out
    assert "live.enabled: true" in out
    assert "UPSTOX_LIVE_CONFIRM" in out


def test_live_flag_reports_the_block(capsys):
    assert cli.main(["--live", "mode"]) == 0
    out = capsys.readouterr().out
    assert "LIVE" in out and "BLOCKED" in out


def test_run_live_without_arming_exits_with_code_three(capsys):
    assert cli.main(["--live", "--yes", "run", "--once"]) == 3
    assert "LIVE TRADING BLOCKED" in capsys.readouterr().err


def test_run_live_needs_interactive_confirmation(monkeypatch, capsys, isolated_config):
    """Armed but non-interactive and without --yes: refuse to trade."""
    isolated_config.write_text(
        isolated_config.read_text() + "live:\n  enabled: true\n  require_market_hours: false\n"
    )
    monkeypatch.setenv("UPSTOX_LIVE_CONFIRM", "I UNDERSTAND THIS TRADES REAL MONEY")
    monkeypatch.setenv("UPSTOX_API_KEY", "key")
    monkeypatch.setenv("UPSTOX_API_SECRET", "secret")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["--live", "run", "--once"]) == 1
    assert "still in paper mode" in capsys.readouterr().out


def test_panic_then_resume_toggles_the_kill_switch(tmp_path, capsys):
    kill_switch = tmp_path / "KILL_SWITCH"
    assert cli.main(["panic", "--no-flatten"]) == 0
    assert kill_switch.exists()
    assert cli.main(["resume"]) == 0
    assert not kill_switch.exists()


def test_report_runs_on_an_empty_journal(capsys):
    assert cli.main(["report"]) == 0
    assert "Nifty 50" in capsys.readouterr().out


def test_unknown_mode_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        cli.main(["--mode", "semi-live", "mode"])
