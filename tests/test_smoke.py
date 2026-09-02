"""CLI smoke tests: doctor is non-mutating and invalid flags return 1.

Live harness checks belong in smoke/ and use exit 77 when skipped.
"""
import os
from pathlib import Path

import pytest
from conftest import _env_sample_vars

from tool_bridge.cli import DATA_ENV, EXIT_FAIL, EXIT_OK, main


def test_tests_scrub_external_tools_data():
    """An external TOOLS_DATA cannot redirect tests into user storage."""
    assert "TOOLS_DATA" not in os.environ


def test_doctor_reports_missing_install_without_mutation(tmp_path, capsys):
    home = tmp_path / "data"
    home.mkdir()
    assert main(["--home", str(home), "doctor"]) == EXIT_FAIL
    out = capsys.readouterr().out
    assert "doctor" in out
    assert "NOT OK" in out
    assert "bridge launcher is missing" in out
    assert "shared config:" in out
    assert "exists=false valid=- error=false" in out


def test_doctor_uses_tools_data_default_and_env_override(tmp_path, monkeypatch, capsys):
    """Resolve home through CLI, tool env, TOOLS_DATA, then the documented default."""
    assert DATA_ENV == "BRIDGE_DATA"
    monkeypatch.delenv(DATA_ENV, raising=False)
    monkeypatch.delenv("TOOLS_DATA", raising=False)
    default_home = Path.home() / "tools-data" / "bridge-data"
    default_home.mkdir(parents=True)
    assert main(["doctor"]) == EXIT_FAIL
    assert "home: " + str(default_home) in capsys.readouterr().out

    env_home = tmp_path / "from-env"
    env_home.mkdir()
    monkeypatch.setenv(DATA_ENV, str(env_home))
    assert main(["doctor"]) == EXIT_FAIL
    assert "home: " + str(env_home) in capsys.readouterr().out

    cli_home = tmp_path / "from-cli"
    cli_home.mkdir()
    assert main(["--home", str(cli_home), "doctor"]) == EXIT_FAIL
    assert "home: " + str(cli_home) in capsys.readouterr().out

    tools_data = tmp_path / "shared-root"
    (tools_data / "bridge-data").mkdir(parents=True)
    monkeypatch.delenv(DATA_ENV, raising=False)
    monkeypatch.setenv("TOOLS_DATA", str(tools_data))
    assert main(["doctor"]) == EXIT_FAIL
    assert "home: " + str(tools_data / "bridge-data") in capsys.readouterr().out


def test_home_is_accepted_before_and_after_subcommand(tmp_path, capsys):
    home = tmp_path / "same-home"
    home.mkdir()
    assert main(["--home", str(home), "doctor"]) == EXIT_FAIL
    before = capsys.readouterr().out
    assert main(["doctor", "--home", str(home)]) == EXIT_FAIL
    after = capsys.readouterr().out
    expected = "  home: " + str(home.resolve())
    assert expected in before
    assert expected in after


def test_doctor_missing_home_fails_and_does_not_create(tmp_path, capsys):
    home = tmp_path / "no-such-home"
    assert main(["--home", str(home), "doctor"]) == EXIT_FAIL
    assert not home.exists()  # doctor diagnoses without mutation


def test_usage_error_exits_1(capsys):
    """The exit-code contract maps an invalid flag to 1 rather than argparse's 2."""
    with pytest.raises(SystemExit) as e:
        main(["doctor", "--no-such-flag"])
    assert e.value.code == EXIT_FAIL
    assert "error" in capsys.readouterr().err


def test_env_sample_creds_scrubbed(tmp_path):
    """The conftest isolator hides variables declared in .env.sample."""
    # Prose containing an assignment-like fragment must not match.
    sample = tmp_path / ".env.sample"
    sample.write_text("# prose heading: NAME=\n# FOO_TOKEN=   # secret\nBAR_KEY=x\n", "utf-8")
    assert _env_sample_vars(sample) == ["FOO_TOKEN", "BAR_KEY"]
    # No variable declared by the repository sample is visible to the test.
    leaked = [n for n in _env_sample_vars() if n in os.environ]
    assert not leaked, f"credentials from .env.sample are visible to tests: {leaked}"
