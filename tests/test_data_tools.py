"""控制层测试：所有湖/库为tmp，原生actor只用明确的协议mock。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ptrade_sim import data_tools as tools
from ptrade_sim.exceptions import ConfigError

pytestmark = pytest.mark.integration


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in tools.ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PT_DATA_CONFIG", raising=False)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "lake": "lake",
                "db": "out/db.duckdb",
                "start": "2025-01-02",
                "end": "2025-01-03",
                "symbols": ["000001.SZ", "600000.SS"],
                "fetch_exe": "missing-fetch.exe",
                "store_exe": "missing-store.exe",
                "reference_refresh": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return tools.load_data_config(path, use_env=False)


def test_minimal_config_paths_dates_and_units(config, tmp_path):
    assert config["lake"] == str((tmp_path / "lake").resolve())
    assert config["db"] == str((tmp_path / "out/db.duckdb").resolve())
    assert config["symbols"] == ["000001.SZ", "600000.SH"]
    assert config["start"] == "2025-01-02"
    assert config["require_volume_v2"] is True
    assert config["max_pages"] == 64
    assert len(config["datasets"]) == 10
    assert not (tmp_path / "lake").exists() and not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_pages", 1000),
        ("page_size", 801),
        ("retries", -1),
        ("retries", 33),
        ("retries", 1.5),
        ("timeout_secs", 0),
        ("requests_per_second", 0),
        ("requests_per_second", float("inf")),
        ("require_volume_v2", "true"),
        ("start", "2025-02-30"),
        ("datasets", ["unknown"]),
        ("symbols", ["../path"]),
    ],
)
def test_invalid_native_limits_are_rejected(config, key, value, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({**config, key: value}), encoding="utf-8")
    with pytest.raises(ConfigError):
        tools.load_data_config(path, use_env=False)


def test_utf8_lf_atomic_json_and_failed_write_preserve_old(tmp_path):
    path = tmp_path / "nested/report.json"
    tools.atomic_json(path, {"message": "中文报告"})
    before = path.read_bytes()
    assert "中文报告".encode() in before and before.endswith(b"\n") and b"\r\n" not in before
    assert tools.read_json(path) == {"message": "中文报告"}
    with pytest.raises(ValueError):
        tools.atomic_json(path, {"invalid": float("nan")})
    assert path.read_bytes() == before, "失败写入不能损坏已冻结报告"
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "report,code,expected",
    [
        ({"status": "ok"}, 0, "succeeded"),
        ({"status": "partial"}, 0, "partial"),
        ({"status": "partial"}, 4, "partial"),
        ({}, 130, "cancelled"),
        ({}, 0, "failed"),
        ({"status": "ok"}, 3, "failed"),
        ({"status": "failed"}, 0, "failed"),
        ({"status": "canceled"}, 0, "cancelled"),
    ],
)
def test_native_status_normalization_does_not_fake_success(report, code, expected):
    assert tools._normalized_status(report, code) == expected


def test_missing_doctor_and_plan_are_readonly(config, tmp_path):
    assert tools.binary_status("fetch", config)["available"] is False
    plan = tools.build_plan(config, "sync")
    assert plan["ready"] is False and set(plan["missing"]) == {"fetch", "store"}
    assert plan["steps"] == ["fetch", "reference_resolve", "ingest", "verify"]
    assert not (tmp_path / "lake").exists() and not (tmp_path / "out").exists()


def test_three_native_stages_use_filtered_configs_and_no_empty_root_fallback(
    config, tmp_path, monkeypatch
):
    fake = tmp_path / "inert-native.exe"
    fake.write_text("not executed", encoding="utf-8")
    config["fetch_exe"] = config["store_exe"] = str(fake)
    calls = []
    references = []

    def reference(cfg, stage, directory, event):
        references.append(stage)
        return {"status": "succeeded", "dataset_roots": {}, "native_files": {}, "errors": []}

    def process(command, *, log_path, cancel_event):
        stage = command[1]
        value = tools.read_json(Path(command[command.index("--config") + 1]))
        calls.append((stage, value))
        report_path = Path(command[command.index("--report") + 1])
        tools.atomic_json(report_path, {"status": "ok", "counts": {"rows": 2}, "coverage": []})
        return 0

    monkeypatch.setattr(tools, "_reference_stage", reference)
    monkeypatch.setattr(tools, "run_process", process)
    outcome = tools.run_data_pipeline(
        config,
        "sync",
        directory=tmp_path / "job",
        report_path=tmp_path / "report.json",
        progress_path=tmp_path / "progress.json",
    )
    assert outcome.exit_code == 0 and outcome.report["status"] == "succeeded"
    assert [stage for stage, _ in calls] == ["fetch", "ingest", "verify"]
    assert references == ["reference_resolve"]
    assert set(calls[0][1]["datasets"]) == {
        "daily_bars",
        "index_bars",
        "minute_bars",
        "minute_bars_5m",
    }
    assert "tdx_hosts" not in calls[0][1]
    assert set(calls[1][1]["datasets"]) == {
        "daily_bars",
        "index_bars",
        "minute_bars",
        "minute_bars_5m",
        "instruments",
        "trading_calendar",
        "adj_factors",
        "index_constituents",
        "trading_status",
    }
    assert "corporate_actions" not in calls[1][1]["datasets"]
    assert calls[1][1]["dataset_roots"] == calls[1][1]["native_files"] == {}
    assert tools.read_json(tmp_path / "progress.json")["status"] == "succeeded"


@pytest.mark.parametrize(
    "body,code,available",
    [
        ({"status": "ok", "version": "fixture-1"}, 0, True),
        ({}, 0, False),
        ({"status": "partial"}, 0, False),
        ({"status": "unknown"}, 0, False),
        ({"status": "ok"}, 3, False),
        ([], 0, False),
    ],
)
def test_doctor_requires_real_success_shape(config, tmp_path, monkeypatch, body, code, available):
    import subprocess

    path = tmp_path / "inert.exe"
    path.write_text("never executed", encoding="utf-8")
    config["fetch_exe"] = str(path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, code, stdout=json.dumps(body), stderr="fixture-error"
        )

    monkeypatch.setattr(tools.subprocess, "run", run)
    result = tools.binary_status("fetch", config)
    assert result["available"] is available
    assert calls == [[str(path), "doctor"]]
    if available:
        assert result["version"] == "fixture-1"
    else:
        assert result["error"]
    calls.clear()
    skipped = tools.binary_status("fetch", config, probe=False)
    assert skipped["exists"] is True and skipped["available"] is False
    assert skipped["error"] == "startup_not_probed" and calls == []


@pytest.mark.parametrize("error", [OSError("no launch"), ValueError("bad JSON")])
def test_doctor_failures_are_unavailable(config, tmp_path, monkeypatch, error):
    path = tmp_path / "inert.exe"
    path.write_text("inert", encoding="utf-8")
    config["fetch_exe"] = str(path)

    def fail(*a, **kw):
        raise error

    monkeypatch.setattr(tools.subprocess, "run", fail)
    assert tools.binary_status("fetch", config)["available"] is False


def test_public_config_redacts_secrets_and_url_values():
    result = tools.public_config(
        {
            "token": "SECRET",
            "headers": {"Authorization": "SECRET"},
            "_base_dir": "SECRET",
            "fetch_exe": "SECRET",
            "nested": [{"password": "SECRET"}],
            "endpoint": "https://user:pw@example.invalid:8443/p?token=SECRET&format=json#frag",
            "count": 2,
        }
    )
    text = json.dumps(result)
    assert "SECRET" not in text and "user:pw" not in text
    assert result["endpoint"] == "https://example.invalid:8443/p?token&format"
    assert result["count"] == 2


@pytest.mark.parametrize(
    "value", [[], {"lake": "elsewhere"}, {"fetch_exe": "cmd.exe"}, {"url": "http://bad"}]
)
def test_public_overrides_disallow_paths_or_commands(value):
    with pytest.raises(ConfigError):
        tools.validate_overrides(value)


def test_config_environment_and_explicit_overlay(config, tmp_path, monkeypatch):
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("PT_DATA_CONFIG", str(path))
    monkeypatch.setenv("PT_DATA_START", "2025-01-01")
    value = tools.load_data_config(overrides={"end": "2025-01-04", "symbols": ["600000.SS"]})
    assert value["start"] == "2025-01-01" and value["end"] == "2025-01-04"
    assert value["symbols"] == ["600000.SH"]
    assert config["symbols"] == ["000001.SZ", "600000.SH"]


@pytest.mark.parametrize("text", ["{", "[]"])
def test_bad_json_reports_fail_loudly(tmp_path, text):
    from ptrade_sim.exceptions import DataError

    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(DataError):
        tools.read_json(path)
    with pytest.raises(DataError):
        tools.read_json(path, max_bytes=0)


@pytest.mark.parametrize(
    "bad", [{"errors": {}}, {"coverage": {}}, {"warnings": 0}, {"files": "x"}, {"counts": []}]
)
def test_native_report_fields_are_validated(tmp_path, bad):
    from ptrade_sim.exceptions import DataError

    path = tmp_path / "report.json"
    tools.atomic_json(path, {"status": "ok", **bad})
    with pytest.raises(DataError):
        tools._stage_report(path, "fetch", 0, tmp_path / "log")
    missing = tools._stage_report(tmp_path / "missing", "fetch", 130, tmp_path / "log")
    assert missing["status"] == "cancelled"


@pytest.mark.parametrize(
    "partial,allow,code,expected",
    [
        (True, False, 0, "partial"),
        (True, True, 4, "partial"),
        (False, False, 3, "failed"),
        (False, False, 130, "cancelled"),
    ],
)
def test_pipeline_partial_failure_cancel_do_not_start_wrong_stages(
    config, tmp_path, monkeypatch, partial, allow, code, expected
):
    path = tmp_path / "inert.exe"
    path.write_text("inert", encoding="utf-8")
    config.update(fetch_exe=str(path), store_exe=str(path), allow_partial=allow)
    calls = []

    def reference(*a, **kw):
        return {"status": "succeeded", "dataset_roots": {}, "native_files": {}}

    def run(command, *, log_path, cancel_event):
        calls.append(command[1])
        report = Path(command[command.index("--report") + 1])
        if len(calls) == 1 and code == 130:
            return code
        tools.atomic_json(report, {"status": "partial" if len(calls) == 1 and partial else "ok"})
        return code if len(calls) == 1 else 0

    monkeypatch.setattr(tools, "_reference_stage", reference)
    monkeypatch.setattr(tools, "run_process", run)
    result = tools.run_data_pipeline(
        config, "sync", directory=tmp_path / "job", progress_path=tmp_path / "progress.json"
    )
    assert result.report["status"] == expected
    assert calls == (["fetch", "ingest", "verify"] if partial and allow else ["fetch"])
    assert result.exit_code == (0 if partial and allow else 130 if code == 130 else 3)


def test_pipeline_missing_binary_records_dependency_failure(config, tmp_path):
    result = tools.run_data_pipeline(config, "fetch", directory=tmp_path / "job")
    assert result.report["status"] == "failed" and result.exit_code == 6
    assert result.report["errors"][0]["kind"] == "DependencyError"


@pytest.mark.parametrize(
    "stage,expected", [("reference_resolve", "resolve"), ("reference_after", "after")]
)
def test_revision_connector_uses_absolute_script_and_explicit_empty_roots(
    config, tmp_path, monkeypatch, stage, expected
):
    import sys

    config["datasets"] = ["daily_bars"]
    config["reference_refresh"]["python"] = sys.executable
    config["reference_refresh"]["after"] = ["derive_adj_factors"]
    directory = tmp_path / "job"
    directory.mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        report = Path(command[command.index("--report") + 1])
        tools.atomic_json(
            report,
            {
                "status": "ok",
                "dataset_roots": {"daily_bars": []},
                "native_files": {"daily_bars": []},
            },
        )
        return 0

    monkeypatch.setattr(tools, "run_process", run)
    report = tools._reference_stage(config, stage, directory, None)
    assert report["status"] == "succeeded"
    assert Path(calls[0][1]).is_absolute() and Path(calls[0][1]).name == "data_reference.py"
    assert calls[0][calls[0].index("--stage") + 1] == expected
    assert report["dataset_roots"] == {"daily_bars": []}


def test_reference_before_temp_toml_preserves_original_and_sdk_db_isolated(
    config, tmp_path, monkeypatch
):
    import sys

    import tomllib

    original = tmp_path / "lake.toml"
    text = '[data]\nroot="ORIGINAL"\nmode="keep"\n[duckdb]\npath="ORIGINAL_DB"\nthreads=3\n'
    original.write_text(text, encoding="utf-8")
    config["reference_refresh"].update(
        python=sys.executable, config=str(original), before=["instruments"]
    )
    directory = tmp_path / "job"
    directory.mkdir()
    captures = []

    def run(command, **kwargs):
        path = Path(command[command.index("--config") + 1])
        captures.append((path, tomllib.loads(path.read_text(encoding="utf-8"))))
        assert command[command.index("--group") + 1].startswith("ptrade_refs_")
        return 0

    monkeypatch.setattr(tools, "run_process", run)
    report = tools._reference_stage(config, "reference_before", directory, None)
    assert report["status"] == "succeeded" and report["counts"]["bar_downloads"] == 0
    assert captures[0][1]["data"]["root"] == str(Path(config["lake"]).resolve())
    assert captures[0][1]["data"]["mode"] == "keep"
    assert captures[0][1]["duckdb"]["path"] == str((directory / "reference-sdk.duckdb").resolve())
    assert captures[0][1]["duckdb"]["threads"] == 3
    assert original.read_text(encoding="utf-8") == text and not captures[0][0].exists()
    config["reference_refresh"]["before"] = ["daily_bars"]
    with pytest.raises(ConfigError):
        tools._reference_stage(config, "reference_before", directory, None)


def test_short_real_owned_process_success_timeout_and_cancel(tmp_path):
    import sys
    from threading import Event, Timer

    log = tmp_path / "owned.log"
    assert (
        tools.run_process([sys.executable, "-c", "print('中文fixture')"], log_path=log, timeout=5)
        == 0
    )
    assert "中文fixture" in log.read_text(encoding="utf-8")
    assert (
        tools.run_process(
            [sys.executable, "-c", "import time;time.sleep(10)"], log_path=log, timeout=0.2
        )
        == 124
    )
    event = Event()
    timer = Timer(0.3, event.set)
    timer.start()
    try:
        assert (
            tools.run_process(
                [sys.executable, "-c", "import time;time.sleep(10)"],
                log_path=log,
                cancel_event=event,
                timeout=3,
            )
            == 130
        )
    finally:
        timer.cancel()
    event.set()
    assert (
        tools.run_process(
            [sys.executable, "-c", "raise RuntimeError('must not launch')"],
            log_path=tmp_path / "not-created/log",
            cancel_event=event,
        )
        == 130
    )
    assert not (tmp_path / "not-created").exists()


def test_cli_parse_status_plan_and_mocked_pipeline(config, tmp_path, monkeypatch, capsys):
    from ptrade_sim import cli, data_jobs

    path = tmp_path / "cli.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    root = tmp_path / "results"
    status_args = cli.parse_args(
        ["data", "status", "--config", str(path), "--output-dir", str(root), "--json"]
    )
    assert status_args.data_command == "status" and cli.run_data(status_args) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == [] and not root.exists()
    plan_args = cli.parse_args(
        [
            "data",
            "plan",
            "--config",
            str(path),
            "--start",
            "2025-01-01",
            "--symbols",
            "600000.SS,000001.SZ",
            "--output-dir",
            str(root),
            "--json",
        ]
    )
    assert cli.run_data(plan_args) == 6
    assert json.loads(capsys.readouterr().out)["symbols"] == ["600000.SH", "000001.SZ"]
    calls = []

    def pipeline(cfg, operation, **kwargs):
        calls.append((operation, cfg["symbols"]))
        return tools.DataOutcome(
            {"status": "partial", "counts": {}, "errors": [], "coverage": []}, 3
        )

    monkeypatch.setattr(data_jobs, "run_data_pipeline", pipeline)
    args = cli.parse_args(
        [
            "data",
            "fetch",
            "--config",
            str(path),
            "--symbols",
            "000001.SZ",
            "--datasets",
            "daily_bars",
            "--output-dir",
            str(root),
            "--json",
        ]
    )
    assert cli.run_data(args) == 3
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "partial" and calls == [("fetch", ["000001.SZ"])]


def test_lake_inventory_reads_only_known_dataset_files(tmp_path):
    root = tmp_path / "lake"
    for relative in (
        "curated/daily_bars/a.parquet",
        "derived/adj_factors/b.parquet",
        "curated/unrelated/c.parquet",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    value = tools.lake_inventory(str(root))
    assert value["exists"] is True
    assert set(value["datasets"]) == {"daily_bars", "adj_factors"}
    assert value["datasets"]["daily_bars"] == {"files": 1, "bytes": 7}


def test_database_inventory_is_json_ready_and_ignores_foreign_tables(tmp_path):
    import duckdb

    path = tmp_path / "fixture.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE ashare_test(date DATE, value DOUBLE)")
    connection.execute("INSERT INTO ashare_test VALUES ('2025-01-02',1),('2025-01-03',2)")
    connection.execute("CREATE TABLE ashare_metadata(code VARCHAR)")
    connection.execute("INSERT INTO ashare_metadata VALUES ('000001.SZ')")
    connection.execute("CREATE TABLE foreign_table(value INTEGER)")
    connection.close()
    before = path.read_bytes()
    result = tools.database_inventory(str(path))
    assert result["exists"] is True and result["busy"] is False and result["error"] is None
    assert {row["name"] for row in result["tables"]} == {"ashare_test", "ashare_metadata"}
    row = next(row for row in result["tables"] if row["name"] == "ashare_test")
    assert row["rows"] == 2
    # status给HTTP/CLI使用，真实DATE字段必须是JSON可编码的ISO字符串。
    assert row["start"] == "2025-01-02" and row["end"] == "2025-01-03"
    json.dumps(result)
    assert path.read_bytes() == before


def test_busy_database_does_not_claim_zero_coverage(tmp_path, monkeypatch):
    import duckdb

    path = tmp_path / "fixture.duckdb"
    path.write_bytes(b"fixture")

    def busy(*a, **kw):
        raise duckdb.IOException("database lock")

    monkeypatch.setattr(duckdb, "connect", busy)
    result = tools.database_inventory(str(path))
    assert result["busy"] is True and result["error"] and result["tables"] == []


def test_date_window_weekday_fallback_and_calendar_source(config, tmp_path, monkeypatch):
    import datetime

    import duckdb

    class FixedDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2025, 1, 4)

    monkeypatch.setattr(tools.dt, "date", FixedDate)
    value = {**config, "start": None, "end": None}
    fallback = tools.resolve_window(value)
    assert fallback["end"] == "2025-01-03" and fallback["start"] == "2024-12-22"
    assert fallback["_calendar_basis"] == "weekday_fallback"
    directory = Path(config["lake"]) / "curated/trading_calendar"
    directory.mkdir(parents=True)
    path = directory / "part.parquet"
    connection = duckdb.connect()
    connection.execute("CREATE TABLE cal(trade_date DATE,is_trading BOOLEAN)")
    connection.execute("INSERT INTO cal VALUES ('2025-01-02',true),('2025-01-03',false)")
    connection.execute("COPY cal TO ? (FORMAT PARQUET)", [str(path)])
    connection.close()
    result = tools.resolve_window(value)
    assert result["end"] == "2025-01-02" and result["_calendar_basis"] == "source_trading_calendar"


@pytest.mark.parametrize(
    "key,value",
    [
        ("lake", None),
        ("db", ""),
        ("symbols", [3]),
        ("datasets", None),
        ("trailing_days", 0),
        ("concurrency", True),
        ("refresh", 0),
        ("reference_refresh", None),
        ("reference_refresh", {"enabled": True, "python": "", "config": "x"}),
        ("reference_refresh", {"enabled": True, "timeout_secs": 0}),
        ("reference_refresh", {"enabled": True, "before": [3]}),
        ("reference_refresh", {"enabled": True, "after": ["bad name"]}),
    ],
)
def test_remaining_configuration_types_fail(config, tmp_path, key, value):
    path = tmp_path / "bad-config.json"
    path.write_text(json.dumps({**config, key: value}), encoding="utf-8")
    with pytest.raises(ConfigError):
        tools.load_data_config(path, use_env=False)


def test_auto_config_discovery_and_date_empty_normalization(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "repository_root", lambda: tmp_path)
    path = tmp_path / "ptrade_data_config.json"
    path.write_text(json.dumps({**config, "start": ""}), encoding="utf-8")
    value = tools.load_data_config(use_env=False)
    assert value["start"] is None and value["_config_path"] == str(path)
    with pytest.raises(ConfigError):
        tools._date(42, "start")
    with pytest.raises(ConfigError):
        tools.load_data_config(tmp_path / "missing.json", use_env=False)


def test_native_binary_search_fallback_uses_no_arbitrary_executable(config, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "__file__", str(tmp_path / "src/pkg/data_tools.py"))
    monkeypatch.setattr(tools, "repository_root", lambda: tmp_path)
    path = tmp_path / "on-path.exe"
    path.write_bytes(b"not executed")
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(path))
    assert tools.resolve_binary("fetch", {**config, "fetch_exe": None}) == path
    with pytest.raises(ConfigError):
        tools.resolve_binary("shell", config)


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_roots": None, "native_files": {}},
        {"dataset_roots": {"daily_bars": "bad"}, "native_files": {}},
        {"dataset_roots": {"daily_bars": []}, "native_files": {"daily_bars": ["bad"]}},
        {"dataset_roots": {}, "native_files": {}},
    ],
)
def test_revision_connector_incomplete_manifests_do_not_succeed(
    config, tmp_path, monkeypatch, changes
):
    import sys

    from ptrade_sim.exceptions import DataError

    config["datasets"] = ["daily_bars"]
    config["reference_refresh"]["python"] = sys.executable
    directory = tmp_path / "job"
    directory.mkdir()

    def run(command, **kwargs):
        tools.atomic_json(Path(command[command.index("--report") + 1]), {"status": "ok", **changes})
        return 0

    monkeypatch.setattr(tools, "run_process", run)
    with pytest.raises(DataError):
        tools._reference_connector(config, "reference_resolve", directory, None)


def test_reference_missing_and_disallowed_steps(config, tmp_path):
    from ptrade_sim.exceptions import DependencyError

    directory = tmp_path / "job"
    directory.mkdir()
    with pytest.raises(DependencyError):
        tools._reference_stage(config, "reference_after", directory, None)
    with pytest.raises(DependencyError):
        tools._reference_stage(config, "reference_before", directory, None)
    config["reference_refresh"]["before"] = []
    assert (
        tools._reference_stage(config, "reference_before", directory, None)["status"] == "succeeded"
    )
    assert tools._toml_override("", "data", "root", "x").endswith('root = "x"\n')
    with pytest.raises(ConfigError):
        tools._toml_override("[data]\n[data]\n", "data", "root", "x")


def test_precancelled_pipeline_and_empty_core_dataset(config, tmp_path, monkeypatch):
    from threading import Event

    event = Event()
    event.set()

    def forbidden(*a, **kw):
        pytest.fail("预取消不能启动actor")

    monkeypatch.setattr(tools, "run_process", forbidden)
    result = tools.run_data_pipeline(
        config, "fetch", directory=tmp_path / "pre", cancel_event=event
    )
    assert result.report["status"] == "cancelled" and result.exit_code == 130
    with pytest.raises(ConfigError):
        tools.run_data_pipeline(config, "shell", directory=tmp_path / "invalid")
    with pytest.raises(ConfigError):
        tools._pipeline_stages({**config, "datasets": ["corporate_actions"]}, "fetch")
    fake = tmp_path / "inert.exe"
    fake.write_bytes(b"inert")
    config["store_exe"] = str(fake)
    monkeypatch.setattr(
        tools,
        "_reference_stage",
        lambda *a, **kw: {"status": "succeeded", "dataset_roots": {}, "native_files": {}},
    )
    config["datasets"] = ["corporate_actions"]
    result = tools.run_data_pipeline(config, "ingest", directory=tmp_path / "no-core")
    assert result.report["status"] == "failed" and result.exit_code == 2
    assert result.report["coverage"][0]["native_core_table"] is False


@pytest.mark.parametrize("failure", [None, "create", "assign"])
def test_windows_job_handle_ownership_without_external_processes(monkeypatch, failure):
    import ctypes
    from types import SimpleNamespace

    calls = []

    class Function:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    kernel = SimpleNamespace(
        CreateJobObjectW=Function("create", 0 if failure == "create" else 123),
        SetInformationJobObject=Function("set", 1),
        AssignProcessToJobObject=Function("assign", 0 if failure == "assign" else 1),
        TerminateJobObject=Function("terminate", 1),
        CloseHandle=Function("close", 1),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    monkeypatch.setattr(
        ctypes, "WinError", lambda code: OSError(code, "fixture Windows error"), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    if failure:
        with pytest.raises(OSError):
            tools._WindowsProcessJob(SimpleNamespace(_handle=456))
        assert len([name for name, _ in calls if name == "close"]) == (
            1 if failure == "assign" else 0
        )
    else:
        job = tools._WindowsProcessJob(SimpleNamespace(_handle=456))
        job.terminate()
        job.close()
        job.close()
        assert ("assign", (123, 456)) in calls
        assert ("terminate", (123, 130)) in calls
        assert len([name for name, _ in calls if name == "close"]) == 1


@pytest.mark.skipif(__import__("os").name != "nt", reason="真实Windows JobObject descendant 验收")
def test_real_owned_descendant_is_terminated_on_cancel(tmp_path):
    import ctypes
    import sys
    from ctypes import wintypes
    from threading import Event, Timer

    marker = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    event = Event()
    timer = Timer(1, event.set)
    timer.start()
    try:
        assert (
            tools.run_process(
                [sys.executable, str(parent), str(child), str(marker)],
                log_path=tmp_path / "owned.log",
                cancel_event=event,
                timeout=3,
            )
            == 130
        )
    finally:
        timer.cancel()
    assert marker.is_file(), "必须先证明真实后代启动，不是只测pre-cancel捷径"
    pid = int(marker.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            # Windows进程终止通知可晚于根进程wait返回；用真实HANDLE有界等待而非PID忙轮询。
            assert kernel.WaitForSingleObject(handle, 5000) == 0, "JobObject取消后后代不能继续活着"
        finally:
            kernel.CloseHandle(handle)
    else:
        assert ctypes.get_last_error() == 87, (
            "无效PID代表此已绑定后代已退出；不能把权限失败当终止成功"
        )


@pytest.mark.parametrize(
    "flag,expected",
    [(None, True), ("--refresh-reference", True), ("--no-refresh-reference", False)],
)
def test_cli_reference_boolean_override_can_disable_enabled_config(
    config, tmp_path, monkeypatch, capsys, flag, expected
):
    import copy

    from ptrade_sim import cli, data_jobs

    value = copy.deepcopy(config)
    value["reference_refresh"]["enabled"] = True
    value["reference_refresh"]["before"] = ["instruments"]
    path = tmp_path / "reference-enabled.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    seen = []

    def pipeline(effective, operation, **kwargs):
        seen.append(
            (
                operation,
                effective["reference_refresh"]["enabled"],
                effective["reference_refresh"]["before"],
            )
        )
        return tools.DataOutcome(
            {"status": "succeeded", "counts": {}, "errors": [], "coverage": []}, 0
        )

    monkeypatch.setattr(data_jobs, "run_data_pipeline", pipeline)
    argv = [
        "data",
        "fetch",
        "--config",
        str(path),
        "--output-dir",
        str(tmp_path / "results"),
        "--json",
    ]
    if flag:
        argv.append(flag)
    args = cli.parse_args(argv)
    assert args.refresh_reference is (None if flag is None else expected)
    assert cli.run_data(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert seen == [("fetch", expected, ["instruments"])], (
        "false必须完整穿过CLI→manager，不可用if value忽略负向覆盖"
    )
    assert tools.read_json(path)["reference_refresh"]["enabled"] is True, (
        "不能修改原配置来假装覆盖生效"
    )


@pytest.mark.parametrize(
    "command,expected",
    [("jobs", 0), ("plan", 0), ("fetch", 0), ("ingest", 130), ("verify", 3), ("sync", 3)],
)
def test_cli_new_commands_exit_codes_trusted_fields_and_output(
    tmp_path, monkeypatch, capsys, command, expected
):
    from ptrade_sim import cli, data_jobs

    calls: list[tuple[object, ...]] = []
    statuses = {"fetch": "succeeded", "ingest": "cancelled", "verify": "failed", "sync": "partial"}

    class Manager:
        def __init__(self, root, path, *, config_overrides):
            calls.append(("init", root, path, config_overrides))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append(("close",))

        def list_jobs(self):
            return [{"id": "fixture", "status": "succeeded"}]

        def plan(self, overrides):
            calls.append(("plan", overrides))
            return {"ready": True, "config": overrides}

        def start(self, operation, overrides, *, idempotency_key):
            calls.append(("start", operation, overrides, idempotency_key))
            return {"id": "fixture"}

        def wait_job(self, job_id):
            return {"id": job_id, "status": statuses[command]}

    monkeypatch.setattr(data_jobs, "DataManager", Manager)
    argv = ["data", command, "--output-dir", str(tmp_path / "results")]
    if command != "jobs":
        argv += [
            "--start",
            "2025-01-02",
            "--end",
            "2025-01-03",
            "--symbols",
            "000001.SZ, ,600000.SH",
            "--datasets",
            "daily_bars,minute_bars",
            "--lake",
            str(tmp_path / "lake"),
            "--db",
            str(tmp_path / "db.duckdb"),
            "--provider",
            "fixture",
            "--idempotency-key",
            "key",
        ]
    args = cli.parse_args(argv)
    assert cli.run_data(args) == expected
    body = json.loads(capsys.readouterr().out)
    assert calls[-1] == ("close",)
    if command == "jobs":
        assert body == {"jobs": [{"id": "fixture", "status": "succeeded"}]}
    else:
        trusted = calls[0][3]
        assert trusted == {
            "lake": str((tmp_path / "lake").resolve()),
            "db": str((tmp_path / "db.duckdb").resolve()),
            "provider": "fixture",
        }
        forwarded = calls[1][1] if command == "plan" else calls[1][2]
        assert forwarded == {
            "start": "2025-01-02",
            "end": "2025-01-03",
            "symbols": ["000001.SZ", "600000.SH"],
            "datasets": ["daily_bars", "minute_bars"],
        }
        if command != "plan":
            assert calls[1][-1] == "key" and body["status"] == statuses[command]


def test_cli_keyboard_interrupt_cancels_then_waits_owned_job(tmp_path, monkeypatch, capsys):
    from ptrade_sim import cli, data_jobs

    calls = []

    class Manager:
        def __init__(self, *args, **kwargs):
            self.waited = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def start(self, *args, **kwargs):
            return {"id": "owned"}

        def wait_job(self, job_id):
            if not self.waited:
                self.waited = True
                raise KeyboardInterrupt()
            return {"id": job_id, "status": "cancelled", "exit_code": 130}

        def cancel_job(self, job_id):
            calls.append(job_id)

    monkeypatch.setattr(data_jobs, "DataManager", Manager)
    args = cli.parse_args(["data", "fetch", "--json", "--output-dir", str(tmp_path)])
    assert cli.run_data(args) == 130
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert calls == ["owned", "closed"], "KeyboardInterrupt必须取消owned job后收结果而非泄漏worker"


@pytest.mark.parametrize(
    "server", ["https://127.0.0.1:8765", "http://remote.invalid:8765", "file:///tmp/x"]
)
def test_cli_cancel_disallows_nonlocal_requests(monkeypatch, server):
    import urllib.request

    from ptrade_sim import cli

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **kw: pytest.fail("不能请求远端取消或shell目标")
    )
    args = cli.parse_args(["data", "cancel", "--job-id", "fixture", "--server", server])
    with pytest.raises(ConfigError):
        cli.run_data(args)


@pytest.mark.parametrize("failure", [None, "invalid-json", "unreachable"])
def test_cli_cancel_http_result_and_failure_are_explicit(monkeypatch, capsys, failure):
    import io
    import urllib.error
    import urllib.request

    from ptrade_sim import cli
    from ptrade_sim.exceptions import DataError

    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if failure == "unreachable":
            raise urllib.error.URLError("fixture connection failed")
        return io.BytesIO(b"not-json" if failure else b'{"id":"owned","status":"cancelled"}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    args = cli.parse_args(
        ["data", "cancel", "--job-id", "unsafe/id", "--server", "http://127.0.0.1:8765/"]
    )
    if failure:
        with pytest.raises(DataError):
            cli.run_data(args)
    else:
        assert cli.run_data(args) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert requests[0].full_url == "http://127.0.0.1:8765/api/data/jobs/unsafe%2Fid/cancel"
    assert requests[0].method == "POST" and requests[0].data == b""


@pytest.mark.parametrize("error,code", [(ConfigError("bad date"), 2)])
def test_cli_main_data_dispatch_reports_expected_errors(monkeypatch, capsys, error, code):
    from ptrade_sim import cli

    def fail(args):
        raise error

    monkeypatch.setattr(cli, "run_data", fail)
    assert cli.main(["data", "status"]) == code
    captured = capsys.readouterr()
    assert "bad date" in captured.out + captured.err
