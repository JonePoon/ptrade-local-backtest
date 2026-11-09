"""数据管理 HTTP 测试及隔离浏览器 fixture。所有 start 均为自建内存任务。"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ptrade_sim import data_api, server
from ptrade_sim.exceptions import ConfigError, DataError, DependencyError, QueueTimeoutError

pytestmark = pytest.mark.integration


class FakeManager:
    """读方法不推进任务；fixture worker 独立更新进度，不调用生产执行器。"""

    def __init__(self, root: Path, duration: float = 0):
        self.root, self.duration = root, duration
        self.lock = Lock()
        self.records: dict[str, dict] = {}
        self.keys: dict[str, tuple[str, str]] = {}
        self.starts: list[tuple[str, dict, str | None]] = []
        self.plans: list[dict] = []
        self.events: dict[str, Event] = {}
        self.threads: list[Thread] = []
        self.closed = False
        self.failure = None

    def status(self):
        return {
            "config": {
                "lake": "隔离fixture湖",
                "db": "隔离fixture库",
                "start": "2025-01-02",
                "end": "2025-01-03",
                "symbols": ["000001.SZ", "600000.SH"],
                "datasets": ["daily_bars", "minute_bars"],
                "provider": "fixture",
                "headers": {"Authorization": "HIDDEN-TOKEN"},
                "api_key": "HIDDEN-KEY",
                "private_url": "HIDDEN-URL",
            },
            "binaries": {
                key: {"path": sys.executable, "available": True, "version": None, "error": None}
                for key in ("fetch", "store")
            },
            "database": {
                "path": "隔离fixture库",
                "exists": True,
                "busy": False,
                "tables": [
                    {"name": "DailyBar", "rows": 4, "start": "2025-01-02", "end": "2025-01-03"},
                    {"name": "MinuteBar", "rows": None, "start": None, "end": None},
                ],
            },
            "lake": {"exists": True, "datasets": {"daily_bars": {"partitions": 1}}},
            "jobs": self.list_jobs(),
        }

    def plan(self, overrides=None):
        self.plans.append(copy.deepcopy(overrides))
        config = {**self.status()["config"], **(overrides or {})}
        return {
            "config": config,
            "ready": True,
            "steps": [{"operation": "fetch"}, {"operation": "ingest"}],
            "missing": [],
            "symbols": config["symbols"],
            "datasets": config["datasets"],
            "start": config["start"],
            "end": config["end"],
            "will_download": True,
            "will_write": True,
        }

    def start(self, operation, overrides, idempotency_key=None):
        if self.failure:
            raise self.failure
        fingerprint = json.dumps({"operation": operation, "overrides": overrides}, sort_keys=True)
        with self.lock:
            if idempotency_key in self.keys:
                previous, job_id = self.keys[idempotency_key]
                if previous != fingerprint:
                    raise ConfigError("幂等键与另一份参数绑定")
                return copy.deepcopy(self.records[job_id])
            job_id = uuid.uuid4().hex
            self.starts.append((operation, copy.deepcopy(overrides), idempotency_key))
            job = {
                "id": job_id,
                "status": "queued",
                "operation": operation,
                "phase": "queued",
                "progress": {"percent": 0, "current": 0, "total": 2, "message": "fixture任务排队"},
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "counts": {},
                "errors": [],
                "coverage": {},
                "report": None,
                "log_tail": ["隔离fixture；不会启动Rust或生产任务"],
            }
            self.records[job_id] = job
            self.events[job_id] = Event()
            if idempotency_key:
                self.keys[idempotency_key] = (fingerprint, job_id)
            if self.duration:
                thread = Thread(target=self._worker, args=(job_id,), daemon=True)
                self.threads.append(thread)
                thread.start()
            return copy.deepcopy(job)

    def _worker(self, job_id):
        event = self.events[job_id]
        for tick in range(1, 21):
            if event.wait(self.duration / 20):
                return
            with self.lock:
                job = self.records[job_id]
                job.update(
                    status="running",
                    phase="fixture-processing",
                    progress={
                        "percent": tick * 5,
                        "current": min(2, tick // 10),
                        "total": 2,
                        "message": "正在处理两个fixture标的",
                    },
                )
        with self.lock:
            job = self.records[job_id]
            partial = job["operation"] == "fetch"
            job.update(
                status="partial" if partial else "succeeded",
                phase="done",
                finished_at=datetime.now(timezone.utc).isoformat(),
                counts={"symbols": 2, "rows": 4},
                coverage={"requested_days": 2, "actual_days": 1 if partial else 2},
                errors=["fixture演示有限覆盖"] if partial else [],
                report={"fixture": True, "partial": partial, "symbols": 2, "rows": 4},
            )
            job["log_tail"].append("fixture完成：" + job["status"])

    def list_jobs(self):
        with self.lock:
            return copy.deepcopy(list(reversed(self.records.values())))

    def get_job(self, job_id):
        with self.lock:
            return copy.deepcopy(self.records.get(job_id))

    def cancel_job(self, job_id):
        with self.lock:
            job = self.records[job_id]
            if job["status"] in {"queued", "running"}:
                self.events[job_id].set()
                job.update(
                    status="cancelled",
                    phase="cancelled",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                job["log_tail"].append("fixture任务已取消")
            return copy.deepcopy(job)

    def close(self):
        self.closed = True
        for event in self.events.values():
            event.set()
        for thread in self.threads:
            thread.join(timeout=1)


@pytest.fixture
def manager(tmp_path):
    return FakeManager(tmp_path)


@pytest.fixture
def client(manager):
    app = FastAPI()
    app.include_router(data_api.create_data_router(manager.root, manager))
    with TestClient(app) as value:
        yield value


def _start(client, key="request-1", **overrides):
    return client.post(
        "/api/data/jobs",
        json={"operation": "fetch", "overrides": overrides},
        headers={"Origin": "http://testserver", "Idempotency-Key": key},
    )


def test_gets_do_not_start_or_plan_tasks(client, manager):
    assert client.get("/api/data/status").status_code == 200
    assert client.get("/api/data/jobs").json() == {"jobs": []}
    assert client.get("/api/data/jobs/missing").status_code == 404
    assert manager.starts == [] and manager.plans == [], "GET不可暗中执行下载或预览更新"


def test_status_redacts_private_config_and_preserves_unknowns(client):
    response = client.get("/api/data/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "HIDDEN-" not in response.text
    body = response.json()
    assert body["binaries"]["fetch"]["version"] is None
    assert body["database"]["tables"][1]["rows"] is None, "未知覆盖不能改成0"


def test_plan_is_readonly_and_fields_are_normalized(client, manager):
    response = client.post(
        "/api/data/plan",
        json={
            "overrides": {
                "start": "2025-01-02",
                "end": "2025-01-03",
                "symbols": [" 000001.SZ ", "000001.SZ"],
                "datasets": ["daily_bars"],
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["will_download"] is True and response.json()["ready"] is True
    assert "HIDDEN-" not in response.text
    assert manager.starts == []
    assert manager.plans == [
        {
            "start": "2025-01-02",
            "end": "2025-01-03",
            "symbols": ["000001.SZ"],
            "datasets": ["daily_bars"],
        }
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.invalid",
        "null",
        "http://testserver:99",
        "https://testserver",
        "http://evil@testserver",
        "http://testserver/path",
        "http://testserver?x=1",
        "http://testserver#x",
        "http://testserver:bad",
    ],
)
@pytest.mark.parametrize("path", ["/api/data/jobs", "/api/data/plan"])
def test_foreign_origins_never_create_tasks(client, manager, origin, path):
    response = client.post(
        path,
        json={"operation": "sync"} if path.endswith("jobs") else {},
        headers={"Origin": origin},
    )
    assert response.status_code == 403
    assert manager.starts == [] and manager.plans == []


def test_cancel_origin_is_checked_before_manager(client, manager):
    job = _start(client).json()
    response = client.post(
        "/api/data/jobs/" + job["id"] + "/cancel",
        json={},
        headers={"Origin": "http://evil.invalid"},
    )
    assert response.status_code == 403
    assert manager.get_job(job["id"])["status"] == "queued"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_missing_origin_cross_site_is_rejected(client, manager, site):
    response = client.post(
        "/api/data/jobs", json={"operation": "fetch"}, headers={"Sec-Fetch-Site": site}
    )
    assert response.status_code == 403
    assert manager.starts == []


def test_default_port_origin_matches(tmp_path):
    manager = FakeManager(tmp_path)
    app = FastAPI()
    app.include_router(data_api.create_data_router(tmp_path, manager))
    with TestClient(app, base_url="http://localhost:80") as client:
        assert (
            client.post(
                "/api/data/plan", json={}, headers={"Origin": "http://localhost"}
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "shell"},
        {"operation": "sync", "executable": "cmd.exe"},
        {"operation": "sync", "overrides": {"db": "C:/other.db"}},
        {"operation": "sync", "overrides": {"lake": "../other"}},
        {"operation": "sync", "overrides": {"url": "http://evil"}},
        {"operation": "sync", "overrides": {"headers": {"X": "Y"}}},
        {"operation": "sync", "overrides": {"start": "2025-02-30"}},
        {"operation": "sync", "overrides": {"start": "2025-02-01", "end": "2025-01-01"}},
        {"operation": "sync", "overrides": {"start": "yesterday"}},
        {"operation": "sync", "overrides": {"datasets": []}},
        {"operation": "sync", "overrides": {"datasets": ["arbitrary"]}},
        {"operation": "sync", "overrides": {"symbols": ["../file"]}},
        {"operation": "sync", "overrides": {"symbols": ["."]}},
        {"operation": "sync", "overrides": {"symbols": ["http://evil"]}},
        {"operation": "sync", "overrides": {"symbols": ["x;rm"]}},
    ],
)
def test_unapproved_fields_and_invalid_inputs_are_rejected(client, manager, body):
    response = client.post("/api/data/jobs", json=body)
    assert response.status_code == 422
    assert manager.starts == []


@pytest.mark.parametrize("key", ["bad/key", "bad\\key", "", "x" * 129])
def test_invalid_idempotency_key_is_rejected(client, manager, key):
    response = _start(client, key=key)
    assert response.status_code == 422
    assert manager.starts == []


def test_key_is_forwarded_and_retries_do_not_duplicate(client, manager):
    first, repeated = _start(client), _start(client)
    assert first.status_code == repeated.status_code == 202
    assert first.json()["id"] == repeated.json()["id"]
    assert manager.starts == [("fetch", {}, "request-1")], (
        "必须把同一key传入持久层，不得新建第二个执行器"
    )
    changed = _start(client, start="2025-01-02")
    assert changed.status_code == 400
    assert len(manager.starts) == 1


def test_concurrent_requests_share_one_persistent_key(client, manager):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: _start(client, key="parallel"), range(8)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(manager.starts) == 1


@pytest.mark.parametrize("operation", ["sync", "fetch", "ingest", "verify"])
def test_operations_are_forwarded(client, manager, operation):
    response = client.post(
        "/api/data/jobs",
        json={"operation": operation, "overrides": {"symbols": [], "datasets": ["daily_bars"]}},
    )
    assert response.status_code == 202
    assert manager.starts[-1] == (operation, {"symbols": [], "datasets": ["daily_bars"]}, None)


def test_partial_and_report_remain_partial(client, manager):
    job = _start(client).json()
    with manager.lock:
        manager.records[job["id"]].update(
            status="partial",
            progress={"percent": 100},
            report={"partial": True},
            errors=["部分覆盖"],
            coverage={"requested": 2, "actual": 1},
        )
    response = client.get("/api/data/jobs/" + job["id"])
    assert response.json()["status"] == "partial"
    assert response.json()["report"] == {"partial": True}
    assert client.get("/api/data/jobs").json()["jobs"][0]["status"] == "partial"


def test_cancel_is_idempotent_and_preserves_terminal_status(client, manager):
    job = _start(client).json()
    path = "/api/data/jobs/" + job["id"] + "/cancel"
    first, again = client.post(path, json={}), client.post(path, json={})
    assert first.status_code == again.status_code == 200
    assert first.json()["status"] == again.json()["status"] == "cancelled"
    with manager.lock:
        manager.records[job["id"]]["status"] = "succeeded"
    assert client.post(path, json={}).json()["status"] == "succeeded"
    assert client.post(path, json={"pid": 123}).status_code == 422
    assert client.post("/api/data/jobs/missing/cancel", json={}).status_code == 404


@pytest.mark.parametrize("name", ["..", "bad:id", "..%2Foutside", "..%5Coutside", "x" * 129])
def test_job_paths_do_not_escape(client, name):
    assert client.get("/api/data/jobs/" + name).status_code == 404
    assert client.post("/api/data/jobs/" + name + "/cancel", json={}).status_code == 404


@pytest.mark.parametrize(
    "error, code",
    [
        (ConfigError("bad config"), 400),
        (DataError("invalid data"), 400),
        (DependencyError("missing engine"), 503),
        (QueueTimeoutError("busy"), 409),
        (FileNotFoundError("missing"), 404),
        (OSError("io"), 503),
    ],
)
def test_expected_manager_errors_are_http_errors(client, manager, error, code):
    manager.failure = error
    response = _start(client)
    assert response.status_code == code
    assert str(error) in response.json()["detail"]["message"]
    assert response.headers["cache-control"] == "no-store"


def test_programming_errors_are_not_hidden(client, manager):
    manager.failure = TypeError("programming bug")
    with pytest.raises(TypeError, match="programming bug"):
        _start(client)


def test_public_values_redact_urls_and_credentials(client, manager, monkeypatch):
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "config": {
                "provider": "https://private.invalid/path?token=X",
                "nested": [{"cookie": "DO-NOT-EXPOSE", "public": "kept"}],
            }
        },
    )
    response = client.get("/api/data/status")
    assert response.status_code == 200
    assert "private.invalid" not in response.text and "DO-NOT-EXPOSE" not in response.text
    assert response.json()["config"]["nested"][0] == {"public": "kept"}


def test_none_overrides_are_omitted(client, manager):
    response = client.post(
        "/api/data/jobs",
        json={
            "operation": "verify",
            "overrides": {"symbols": None, "start": None, "datasets": None},
        },
    )
    assert response.status_code == 202
    assert manager.starts == [("verify", {}, None)]


@pytest.mark.parametrize(
    "missing", ["ptrade_sim.data_jobs", "ptrade_sim.data_tools", "other_dependency"]
)
def test_missing_loader_dependencies_are_distinguished(tmp_path, monkeypatch, missing):
    import builtins

    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "ptrade_sim.data_jobs":
            raise ModuleNotFoundError("missing test module", name=missing)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    error = DependencyError if missing.startswith("ptrade_sim.") else ModuleNotFoundError
    with pytest.raises(error):
        data_api._new_manager(tmp_path)


@pytest.mark.parametrize("dataset", ["minute_bars_5m", "corporate_actions"])
def test_all_manager_datasets_are_selectable(client, manager, dataset):
    response = client.post("/api/data/plan", json={"overrides": {"datasets": [dataset]}})
    assert response.status_code == 200
    assert manager.plans[-1] == {"datasets": [dataset]}


def test_real_default_manager_status_plan_are_readonly(tmp_path, monkeypatch):
    from ptrade_sim import data_tools

    root = tmp_path / "results"
    lake = tmp_path / "empty-lake"
    database = tmp_path / "missing.duckdb"
    config_path = tmp_path / "data-config.json"
    _config = {
        "lake": str(lake),
        "db": str(database),
        "start": "2025-01-02",
        "end": "2025-01-03",
        "symbols": ["000001.SZ", "600000.SH"],
        "datasets": ["daily_bars"],
        "fetch_exe": str(tmp_path / "missing-fetch.exe"),
        "store_exe": str(tmp_path / "missing-store.exe"),
        "api_key": "NOT-PUBLIC",
        "reference_refresh": {"enabled": False},
    }
    config_path.write_text(json.dumps(_config), encoding="utf-8")
    for key in data_tools.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PT_DATA_CONFIG", str(config_path))
    # 不patch factory，使用真正DataManager；不调用start，不创建正式湖/库或任务目录。
    with TestClient(server.create_app(root)) as client:
        response = client.get("/api/data/status")
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["config"]["symbols"] == ["000001.SZ", "600000.SH"]
        assert status["binaries"]["fetch"]["available"] is False
        assert status["binaries"]["store"]["available"] is False
        assert status["database"]["exists"] is False
        assert "NOT-PUBLIC" not in response.text
        assert client.get("/api/data/jobs").json() == {"jobs": []}
        preview = client.post(
            "/api/data/plan", json={"overrides": {"datasets": ["minute_bars_5m"]}}
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["ready"] is False
    assert not (root / ".data_jobs").exists()
    assert not lake.exists() and not database.exists(), "GET/plan不能偷偷下载或创建数据库"


@pytest.mark.parametrize("kind", ["missing-config", "invalid-json", "invalid-range"])
def test_real_default_manager_configuration_errors_are_explicit(tmp_path, monkeypatch, kind):
    from ptrade_sim import data_tools

    root = tmp_path / "results"
    config_path = tmp_path / "invalid-config.json"
    if kind == "invalid-json":
        config_path.write_text("{", encoding="utf-8")
    elif kind == "invalid-range":
        config_path.write_text(
            json.dumps(
                {
                    "lake": str(tmp_path / "lake"),
                    "db": str(tmp_path / "db.duckdb"),
                    "start": "2025-02-01",
                    "end": "2025-01-01",
                }
            ),
            encoding="utf-8",
        )
    for name in data_tools.ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PT_DATA_CONFIG", str(config_path))
    with TestClient(server.create_app(root)) as client:
        status = client.get("/api/data/status")
        plan = client.post("/api/data/plan", json={})
        assert status.status_code == plan.status_code == 400
        assert status.json()["detail"]["code"] == "data_config_error"
        assert plan.json()["detail"]["message"]
    assert not root.exists(), "只读配置错误不能创建任务目录或自动启动修复"


def test_lazy_manager_initializes_once_and_closes(tmp_path, monkeypatch):
    manager = FakeManager(tmp_path)
    calls = []
    monkeypatch.setattr(data_api, "_new_manager", lambda root: calls.append(root) or manager)
    app = server.create_app(tmp_path)
    assert calls == [], "构造旧看板不应初始化数据执行器"
    with TestClient(app) as client:
        assert client.get("/api/ping").status_code == 200
        assert calls == []
        assert client.get("/api/data/status").status_code == 200
        assert client.get("/api/data/jobs").status_code == 200
        assert calls == [tmp_path]
    assert manager.closed is True


def test_unused_manager_is_not_created_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        data_api, "_new_manager", lambda root: pytest.fail("无数据请求不应创建manager")
    )
    with TestClient(server.create_app(tmp_path)) as client:
        assert client.get("/api/ping").status_code == 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="仅提供内存fixture的浏览器验收服务器")
    parser.add_argument("--serve-fixture", action="store_true", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    import uvicorn

    with TemporaryDirectory(prefix="ptrade-data-ui-") as tmp:
        root = Path(tmp)
        manager = FakeManager(root, duration=12)
        data_api._new_manager = lambda value: manager
        app = server.create_app(root)

        @app.get("/__fixture")
        def evidence():
            return {
                "fixture": True,
                "starts": manager.starts,
                "jobs": manager.list_jobs(),
                "closed": manager.closed,
            }

        # create_app末尾是SPA catch-all，证据路由必须在它前面。
        app.router.routes.insert(0, app.router.routes.pop())
        print("FIXTURE_ONLY http://127.0.0.1:" + str(args.port) + " root=" + str(root), flush=True)
        runner = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
        )

        @app.post("/__fixture/shutdown")
        def shutdown():
            runner.should_exit = True
            return {"fixture": True, "shutdown": True}

        runner.run()
        print("FIXTURE_CLOSED", flush=True)
