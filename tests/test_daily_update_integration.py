"""The shipped daily template preserves configuration priorities and exit codes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def daily(monkeypatch, tmp_path):
    file = Path(__file__).resolve().parents[1] / "integrations" / "cne_lake" / "daily_update.py"
    spec = importlib.util.spec_from_file_location("daily_template", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HERE", tmp_path / "cne_lake")
    monkeypatch.setattr(module, "PT_ROOT", tmp_path / "ptrade-sim")
    monkeypatch.setattr(module, "LAKE", tmp_path / "existing-lake")
    monkeypatch.setattr(module, "ENGINE_DB", tmp_path / "existing.duckdb")
    return module


@pytest.mark.parametrize("exit_code", [0, 3, 6])
def test_native_plan_respects_config_and_propagates_exit(daily, tmp_path, monkeypatch, exit_code):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=exit_code)

    monkeypatch.setattr(daily.subprocess, "run", run)
    config = tmp_path / "data-config.json"
    output = tmp_path / "jobs"
    assert (
        daily.main(
            [
                "--plan",
                "--config",
                str(config),
                "--output-dir",
                str(output),
                "--symbols",
                "000001.SZ",
                "--no-reference-refresh",
            ]
        )
        == exit_code
    )
    command, kwargs = calls[0]
    assert command[1:6] == ["-m", "ptrade_sim.cli", "data", "plan", "--json"]
    assert "--lake" not in command and "--db" not in command
    assert command[command.index("--config") + 1] == str(config.resolve())
    assert "--no-refresh-reference" in command and "--refresh-reference" not in command
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert not output.exists() and not daily.HERE.exists(), (
        "Read-only plan must not create logs/jobs"
    )


def test_explicit_lake_and_db_override_template_and_config(daily, tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(
        daily.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or SimpleNamespace(returncode=0),
    )
    lake, db = tmp_path / "chosen-lake", tmp_path / "chosen.duckdb"
    assert (
        daily.main(
            [
                "--plan",
                "--config",
                str(tmp_path / "config.json"),
                "--lake",
                str(lake),
                "--db",
                str(db),
            ]
        )
        == 0
    )
    command = commands[0]
    assert command[command.index("--lake") + 1] == str(lake.resolve())
    assert command[command.index("--db") + 1] == str(db.resolve())
    assert "--refresh-reference" in command


def test_legacy_plan_never_launches_an_etl_process(daily, monkeypatch, capsys):
    monkeypatch.setattr(
        daily.subprocess, "run", lambda *a, **k: pytest.fail("legacy plan is read-only")
    )
    assert daily.main(["--backend", "python", "--plan"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["backend"] == "python" and report["write"] is False
    assert not daily.HERE.exists()
