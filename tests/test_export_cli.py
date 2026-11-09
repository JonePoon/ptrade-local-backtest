"""一键导出不读行情、不执行策略，参数与 run 冻结信息不可漂移。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from ptrade_sim import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def export_strategy(tmp_path):
    path = tmp_path / "策略" / "strategy.py"
    path.parent.mkdir()
    path.write_text(
        "def initialize(context):\n    g.params = get_strategy_params()\n\ndef handle_data(context, data):\n    g.seen = True\n",
        encoding="utf-8",
    )
    (path.parent / "strategy_config.json").write_text(
        json.dumps(
            {"params": {"active": True, "nullable": None, "nested": {"n": 3}, "label": "中文参数"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_export_check_is_json_only_and_writes_nothing(
    export_strategy, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["export", "--strategy", str(export_strategy), "--check", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["report"]["params"]["active"] is True
    assert payload["files"] == {}
    assert not (tmp_path / "production").exists()


def test_export_embeds_json_parameters_and_explicit_overrides(
    export_strategy, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "output" / "策略_ptrade.py"
    params_file = tmp_path / "params.json"
    params_file.write_text(json.dumps({"nested": {"n": 4}, "active": False}), encoding="utf-8")
    code = cli.main(
        [
            "export",
            "--strategy",
            str(export_strategy),
            "--output",
            str(output),
            "--params-file",
            str(params_file),
            "--params",
            '{"nested":{"n":5}}',
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["params"]["active"] is False
    assert payload["report"]["params"]["nested"] == {"n": 5}
    namespace = {"g": SimpleNamespace()}
    exec(compile(output.read_text(encoding="utf-8"), str(output), "exec"), namespace)
    namespace["initialize"](SimpleNamespace())
    assert namespace["g"].params["nested"] == {"n": 5}
    assert namespace["g"].params["nullable"] is None
    assert namespace["g"].params["label"] == "中文参数"
    manifest = output.with_suffix(".manifest.json")
    assert manifest.is_file()
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    assert hashlib.sha256(output.read_bytes()).hexdigest() == stored["export_sha256"]
    assert b"\r\n" not in output.read_bytes()


@pytest.mark.parametrize("params", ["[]", "null", "invalid"])
def test_export_bad_parameter_json_is_exit_two(export_strategy, params, capsys):
    assert (
        cli.main(["export", "--strategy", str(export_strategy), "--params", params, "--check"]) == 2
    )
    assert "转换通过" not in capsys.readouterr().out


def test_export_blocker_writes_report_not_strategy(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.py"
    source.write_text(
        "import duckdb\ndef initialize(context):\n    g.x = 1\ndef handle_data(context, data):\n    g.y = 2\n",
        encoding="utf-8",
    )
    output = tmp_path / "out.py"
    assert cli.main(["export", "--strategy", str(source), "--output", str(output), "--json"]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert not output.exists()
    manifest = output.with_suffix(".manifest.json")
    assert manifest.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["ready"] is False


def test_export_never_overwrites_input_even_with_overwrite(export_strategy, capsys):
    before = export_strategy.read_bytes()
    assert (
        cli.main(
            [
                "export",
                "--strategy",
                str(export_strategy),
                "--output",
                str(export_strategy),
                "--overwrite",
            ]
        )
        == 2
    )
    assert export_strategy.read_bytes() == before
    assert "转换通过" not in capsys.readouterr().out


@pytest.mark.integration
def test_run_freezes_exported_parameters_and_source(engine_factory):
    engine = engine_factory(
        "def initialize(context):\n    g.n = get_strategy_params('n', 1)\ndef handle_data(context, data):\n    g.t = 1\n",
        frequency="daily",
        params={"n": 7},
    )
    try:
        engine.run()
        source_file = engine.output_dir / "strategy_export.py"
        report_file = engine.output_dir / "strategy_export_report.json"
        frozen = source_file.read_text(encoding="utf-8")
        report = json.loads(report_file.read_text(encoding="utf-8"))
        assert report["ready"] is True
        assert report["params"] == {"n": 7}
        assert hashlib.sha256(source_file.read_bytes()).hexdigest() == report["export_sha256"]
        assert b"\r\n" not in source_file.read_bytes()
        original = engine.strategy_path
        from pathlib import Path

        Path(original).write_text(
            "raise RuntimeError('changed after backtest')\n", encoding="utf-8"
        )
        engine.config["params"]["n"] = 999
        assert source_file.read_text(encoding="utf-8") == frozen
        assert json.loads(report_file.read_text(encoding="utf-8"))["params"] == {"n": 7}
    finally:
        engine.feed.src.close()


@pytest.mark.integration
def test_run_export_preserves_explicit_empty_effective_parameters(engine_factory):
    engine = engine_factory(
        "def initialize(context):\n    g.params = get_strategy_params()\n"
        "def handle_data(context, data):\n    g.seen = True\n",
        frequency="daily",
        strategy_config={"params": {"stale_default": 42, "nested": {"default_only": 1}}},
    )
    engine.config["params"] = {}  # 最终生效值；不是让导出器再回读策略默认值。
    try:
        engine.run()
        assert engine.g.params == {}
        report = json.loads(
            (engine.output_dir / "strategy_export_report.json").read_text(encoding="utf-8")
        )
        assert report["ready"] is True
        assert report["params"] == {}
        namespace = {"g": SimpleNamespace()}
        source = (engine.output_dir / "strategy_export.py").read_text(encoding="utf-8")
        exec(compile(source, "frozen_strategy", "exec"), namespace)
        namespace["initialize"](SimpleNamespace())
        assert namespace["g"].params == {}
    finally:
        engine.feed.src.close()
