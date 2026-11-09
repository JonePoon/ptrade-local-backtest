"""持久任务生命周期：真实manager、短控制线程，不运行生产下载。"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from ptrade_sim import data_jobs as jobs
from ptrade_sim import data_tools as tools
from ptrade_sim.exceptions import ConfigError, QueueTimeoutError

pytestmark = pytest.mark.integration


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for key in tools.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "lake": str(tmp_path / "lake"),
                "db": str(tmp_path / "db.duckdb"),
                "start": "2025-01-02",
                "end": "2025-01-03",
                "symbols": ["000001.SZ"],
                "datasets": ["daily_bars"],
                "fetch_exe": str(tmp_path / "missing-fetch.exe"),
                "store_exe": str(tmp_path / "missing-store.exe"),
                "reference_refresh": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path / "results", path


def outcome(status="succeeded"):
    return tools.DataOutcome(
        {
            "status": status,
            "counts": {"rows": 2},
            "errors": [] if status == "succeeded" else [{"message": "fixture status"}],
            "coverage": [{"requested": 2, "actual": 1}],
        },
        0 if status == "succeeded" else 130 if status == "cancelled" else 3,
    )


def test_manager_get_methods_create_no_directory(configured):
    root, path = configured
    with jobs.DataManager(root, path) as manager:
        assert manager.status()["database"]["exists"] is False
        assert manager.plan()["ready"] is False
        assert manager.list_jobs() == [] and manager.get_job("missing") is None
        assert manager.config_public()["symbols"] == ["000001.SZ"]
    assert not root.exists(), "GET/构造/close不能暗中创建任务或数据目录"


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "cancelled"])
def test_terminal_status_and_report_are_preserved(configured, monkeypatch, status):
    root, path = configured
    monkeypatch.setattr(jobs, "run_data_pipeline", lambda *a, **kw: outcome(status))
    with jobs.DataManager(root, path) as manager:
        first = manager.start("fetch")
        finished = manager.wait_job(first["id"], timeout=3)
        assert finished["status"] == status
        assert finished["counts"] == {"rows": 2}
        assert finished["report"]["status"] == status
        assert finished["finished_at"]
        assert finished["exit_code"] == (
            0 if status == "succeeded" else 130 if status == "cancelled" else 3
        )
        if status == "succeeded":
            assert finished["progress"]["percent"] == 100


def test_eight_threads_same_key_only_one_worker_and_restart(configured, monkeypatch):
    root, path = configured
    entered, release = Event(), Event()
    calls = []
    lock = Lock()

    def pipeline(*args, **kwargs):
        with lock:
            calls.append(args[1])
        entered.set()
        assert release.wait(5), "fixture worker未被测试释放"
        return outcome()

    monkeypatch.setattr(jobs, "run_data_pipeline", pipeline)
    manager = jobs.DataManager(root, path)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(
                pool.map(lambda _: manager.start("fetch", idempotency_key="same-key"), range(8))
            )
        assert entered.wait(2)
        assert len({record["id"] for record in records}) == 1
        assert calls == ["fetch"], "只比较id相等不足以证明没有第二个actor"
        with pytest.raises(ConfigError):
            manager.start("fetch", {"start": "2025-01-01"}, idempotency_key="same-key")
        release.set()
        assert manager.wait_job(records[0]["id"], 3)["status"] == "succeeded"
    finally:
        release.set()
        manager.close()
    with jobs.DataManager(root, path) as restarted:
        again = restarted.start("fetch", idempotency_key="same-key")
        assert again["id"] == records[0]["id"] and again["status"] == "succeeded"
    assert calls == ["fetch"]


def test_queued_cancel_never_runs_second_worker(configured, monkeypatch):
    root, path = configured
    entered, release = Event(), Event()
    calls = []

    def pipeline(config, operation, **kwargs):
        calls.append(operation)
        entered.set()
        assert release.wait(5)
        return outcome()

    monkeypatch.setattr(jobs, "run_data_pipeline", pipeline)
    manager = jobs.DataManager(root, path, max_workers=1)
    try:
        first = manager.start("fetch")
        assert entered.wait(2)
        queued = manager.start("verify")
        cancelled = manager.cancel_job(queued["id"])
        assert cancelled["status"] == "cancelled" and cancelled["exit_code"] == 130
        release.set()
        assert manager.wait_job(first["id"], 3)["status"] == "succeeded"
        assert calls == ["fetch"]
    finally:
        release.set()
        manager.close()


def test_running_cancel_and_close_signal_only_owned_events(configured, monkeypatch):
    root, path = configured
    entered = Event()

    def pipeline(*args, **kwargs):
        entered.set()
        assert kwargs["cancel_event"].wait(5)
        return outcome("succeeded")

    monkeypatch.setattr(jobs, "run_data_pipeline", pipeline)
    manager = jobs.DataManager(root, path)
    first = manager.start("fetch")
    assert entered.wait(2)
    manager.cancel_job(first["id"])
    assert manager.wait_job(first["id"], 3)["status"] == "cancelled", (
        "cancel事件必须压过actor的迟到成功报告"
    )
    entered.clear()
    second = manager.start("verify")
    assert entered.wait(2)
    manager.close()
    manager.close()
    assert manager.get_job(second["id"])["status"] == "cancelled"
    with pytest.raises(QueueTimeoutError):
        manager.start("fetch")


def test_unknown_saved_pid_is_never_signalled(configured, monkeypatch):
    root, path = configured
    job_id = "a" * 32
    record_path = root / ".data_jobs" / job_id / "job.json"
    tools.atomic_json(
        record_path, {"id": job_id, "status": "running", "pid": 999999999, "phase": "native"}
    )
    before = record_path.read_bytes()
    monkeypatch.setattr(os, "kill", lambda *a, **kw: pytest.fail("不允许按未知历史PID发signal"))
    with jobs.DataManager(root, path) as manager:
        record = manager.get_job(job_id)
        assert record["status"] == "failed" and record["errors"][0]["kind"] == "worker_not_owned"
        with pytest.raises(QueueTimeoutError):
            manager.cancel_job(job_id)
    assert record_path.read_bytes() == before, "GET与close不应该重写非owned历史状态"


@pytest.mark.parametrize("error", [ConfigError("bad cfg"), ValueError("worker defect")])
def test_worker_exception_is_recorded_as_failure(configured, monkeypatch, error):
    root, path = configured

    def fail(*a, **kw):
        raise error

    monkeypatch.setattr(jobs, "run_data_pipeline", fail)
    with jobs.DataManager(root, path) as manager:
        first = manager.start("fetch")
        result = manager.wait_job(first["id"], 3)
        assert result["status"] == "failed" and result["errors"][0]["kind"] == type(error).__name__
        assert result["exit_code"] == (2 if isinstance(error, ConfigError) else 1)


def test_native_progress_and_log_redaction_without_state_write(configured):
    root, path = configured
    job_id = "b" * 32
    directory = root / ".data_jobs" / job_id
    tools.atomic_json(
        directory / "job.json", {"id": job_id, "status": "partial", "phase": "finished"}
    )
    tools.atomic_json(
        directory / "progress.json",
        {"phase": "fetch", "completed": 1, "total": 4, "percent": 25, "message": "partial"},
    )
    (directory / "fetch.log").write_text(
        "Authorization: TOPSECRET\nhttps://host?token=URLSECRET\n", encoding="utf-8"
    )
    before = (directory / "job.json").read_bytes()
    with jobs.DataManager(root, path) as manager:
        record = manager.get_job(job_id)
        assert record["status"] == "partial" and record["progress"]["percent"] == 25
        assert "TOPSECRET" not in "\n".join(record["log_tail"])
        assert "URLSECRET" not in "\n".join(record["log_tail"])
        tools.atomic_json(directory / "progress.json", {"completed": "bad", "total": 0})
        assert manager.get_job(job_id)["progress_error"] == "progress_unavailable"
    assert (directory / "job.json").read_bytes() == before


@pytest.mark.parametrize("job_id", ["../bad", "X" * 32, "a" * 31, "a" * 33])
def test_invalid_ids_cannot_create_or_cancel(configured, job_id):
    root, path = configured
    with jobs.DataManager(root, path) as manager:
        assert manager.get_job(job_id) is None
        with pytest.raises(ConfigError):
            manager.cancel_job(job_id)
    assert not root.exists()


def test_mismatched_bad_job_report_and_missing_idempotent_record(configured, monkeypatch):
    from ptrade_sim.exceptions import DataError

    root, path = configured
    job_id = "c" * 32
    tools.atomic_json(
        root / ".data_jobs" / job_id / "job.json", {"id": "d" * 32, "status": "succeeded"}
    )
    with jobs.DataManager(root, path) as manager:
        with pytest.raises(DataError):
            manager.get_job(job_id)
        tools.atomic_json(
            root / ".data_jobs" / job_id / "job.json", {"id": job_id, "status": "succeeded"}
        )
        assert manager.list_jobs()[0]["id"] == job_id
        with pytest.raises(ConfigError):
            manager.start("shell")
        with pytest.raises(ConfigError):
            manager.start("fetch", idempotency_key="bad\nkey")
        with pytest.raises(ConfigError):
            manager._write({"id": "../bad"})


def test_close_cancels_queued_and_running_owned_jobs(configured, monkeypatch):
    root, path = configured
    entered = Event()
    calls = []

    def pipeline(config, operation, **kwargs):
        calls.append(operation)
        entered.set()
        assert kwargs["cancel_event"].wait(5)
        return outcome()

    monkeypatch.setattr(jobs, "run_data_pipeline", pipeline)
    manager = jobs.DataManager(root, path, max_workers=1)
    first = manager.start("fetch")
    assert entered.wait(2)
    second = manager.start("verify")
    manager.close()
    assert manager.get_job(first["id"])["status"] == "cancelled"
    assert manager.get_job(second["id"])["status"] == "cancelled"
    assert calls == ["fetch"]


def test_active_fingerprint_new_key_alias_and_terminal_cancel(configured, monkeypatch):
    root, path = configured
    entered, release = Event(), Event()
    calls = []

    def pipeline(*a, **kw):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return outcome()

    monkeypatch.setattr(jobs, "run_data_pipeline", pipeline)
    manager = jobs.DataManager(root, path)
    try:
        first = manager.start("fetch")
        assert entered.wait(2)
        alias = manager.start("fetch", idempotency_key="new-key")
        assert alias["id"] == first["id"] and calls == [1]
        release.set()
        manager.wait_job(first["id"], 3)
        assert manager.cancel_job(first["id"])["status"] == "succeeded"
        assert manager.wait_job(first["id"], 3)["status"] == "succeeded"
    finally:
        release.set()
        manager.close()


def test_native_subprogress_combines_with_pipeline_progress(configured, monkeypatch):
    root, path = configured
    entered, release = Event(), Event()

    def pipeline(*a, **kw):
        directory = kw["directory"]
        tools.atomic_json(
            directory / "progress.json", {"phase": "fetch", "completed": 1, "total": 4}
        )
        tools.atomic_json(directory / "fetch_progress.json", {"completed": 5, "total": 10})
        entered.set()
        assert release.wait(5)
        return outcome()

    monkeypatch.setattr(jobs, "run_data_pipeline", pipeline)
    manager = jobs.DataManager(root, path)
    try:
        first = manager.start("fetch")
        assert entered.wait(2)
        record = manager.get_job(first["id"])
        assert record["status"] == "running" and record["phase"] == "fetch"
        assert record["progress"]["percent"] == 37.5
    finally:
        release.set()
        manager.close()


def test_missing_require_job_and_cancelled_worker_boundary(configured, monkeypatch):
    from ptrade_sim.exceptions import DataError

    root, path = configured
    job_id = "d" * 32
    tools.atomic_json(
        root / ".data_jobs" / job_id / "job.json", {"id": job_id, "status": "cancelled"}
    )
    monkeypatch.setattr(
        jobs, "run_data_pipeline", lambda *a, **kw: pytest.fail("cancelled worker不得启动")
    )
    with jobs.DataManager(root, path) as manager:
        manager._run(job_id, {}, "fetch", Event())
        manager._run("e" * 32, {}, "fetch", Event())
        assert manager.wait_job(job_id)["status"] == "cancelled"
        with pytest.raises(DataError):
            manager._require_job("e" * 32)


def test_missing_idempotent_target_is_an_error_not_new_job(configured, monkeypatch):
    from ptrade_sim.exceptions import DataError

    root, path = configured
    monkeypatch.setattr(jobs, "run_data_pipeline", lambda *a, **kw: outcome())
    with jobs.DataManager(root, path) as manager:
        first = manager.start("fetch", idempotency_key="known")
        manager.wait_job(first["id"], 3)
        target = manager.directory / first["id"] / "job.json"
        assert target.resolve().is_relative_to(root.resolve())
        target.unlink()
        with pytest.raises(DataError):
            manager.start("fetch", idempotency_key="known")


def test_real_api_date_inventory_is_json_ready(configured, monkeypatch):
    import duckdb
    from fastapi.testclient import TestClient

    from ptrade_sim import server

    root, path = configured
    config = tools.read_json(path)
    connection = duckdb.connect(config["db"])
    connection.execute("CREATE TABLE ashare_test(date DATE, value DOUBLE)")
    connection.execute("INSERT INTO ashare_test VALUES ('2025-01-02',1)")
    connection.close()
    monkeypatch.setenv("PT_DATA_CONFIG", str(path))
    with TestClient(server.create_app(root)) as client:
        response = client.get("/api/data/status")
        assert response.status_code == 200, response.text
        row = response.json()["database"]["tables"][0]
        assert row["start"] == row["end"] == "2025-01-02"
    assert not root.exists(), "有数据的状态读取仍不能创建任务目录"
