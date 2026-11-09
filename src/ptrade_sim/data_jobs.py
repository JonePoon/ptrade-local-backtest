"""Persistent local data jobs with owned native processes and cancellation.

Constructing the manager and all GET-like methods are read-only. Job files and
workers are created only by start(), which is called by explicit CLI/UI actions.
"""

from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import re
import threading
import uuid
from pathlib import Path

from ptrade_sim.data_tools import (
    atomic_json,
    binary_status,
    build_plan,
    database_inventory,
    lake_inventory,
    load_data_config,
    public_config,
    read_json,
    resolve_window,
    run_data_pipeline,
    validate_overrides,
)
from ptrade_sim.exceptions import ConfigError, DataError, QueueTimeoutError, exit_code_for

_ID = re.compile(r"^[a-f0-9]{32}$")
_TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled"})


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _fingerprint(operation: str, config: dict) -> str:
    normalized = {k: v for k, v in config.items() if not k.startswith("_")}
    payload = json.dumps(
        {"operation": operation, "config": normalized},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _log_tail(directory: Path) -> list[str]:
    lines: list[str] = []
    for path in sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            continue
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 8192))
            text = handle.read(8192).decode("utf-8", errors="replace")
        text = re.sub(
            r"([?&](?:token|key|api_key|password)=)[^&\s]+", r"\1[redacted]", text, flags=re.I
        )
        text = re.sub(r"(Authorization:\s*)([^\r\n]+)", r"\1[redacted]", text, flags=re.I)
        lines.extend(text.splitlines())
    return lines[-120:]


class DataManager:
    def __init__(
        self,
        root: Path,
        config_path: str | Path | None = None,
        *,
        max_workers: int = 1,
        config_overrides: dict | None = None,
    ):
        self.root = Path(root).resolve()
        self.directory = self.root / ".data_jobs"
        self.config_path = config_path
        self.config_overrides = copy.deepcopy(config_overrides or {})
        self._lock = threading.RLock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ptrade-data"
        )
        self._events: dict[str, threading.Event] = {}
        self._futures: dict[str, concurrent.futures.Future] = {}
        self._closed = False
        self._binary_cache: tuple[str, dict] | None = None

    def _config(self, overrides: dict | None = None) -> dict:
        effective = copy.deepcopy(self.config_overrides)
        effective.update(validate_overrides(overrides))
        return load_data_config(self.config_path, overrides=effective)

    def _safe_dir(self, job_id: str) -> Path | None:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            return None
        path = self.directory / job_id
        if path.is_symlink() or self.directory.is_symlink():
            return None
        if not path.resolve().is_relative_to(self.directory.resolve()):
            return None
        return path

    def _read(self, job_id: str) -> dict | None:
        directory = self._safe_dir(job_id)
        if directory is None or not (directory / "job.json").is_file():
            return None
        if (directory / "job.json").is_symlink():
            raise DataError("任务状态文件不能是符号链接")
        record = read_json(directory / "job.json")
        if record.get("id") != job_id:
            raise DataError("任务状态的 id 不匹配")
        return record

    def _write(self, record: dict) -> None:
        directory = self._safe_dir(record["id"])
        if directory is None:
            raise ConfigError("非法数据任务 id")
        atomic_json(directory / "job.json", record)

    def _require_job(self, job_id: str) -> dict:
        record = self.get_job(job_id)
        if record is None:
            raise DataError("任务状态在处理期间缺失")
        return record

    def config_public(self) -> dict:
        return public_config(self._config())

    def status(self) -> dict:
        config = self._config()
        stamp_parts = []
        for kind in ("fetch", "store"):
            from ptrade_sim.data_tools import resolve_binary

            binary = resolve_binary(kind, config)
            stamp_parts.append((kind, str(binary), binary.stat().st_mtime_ns if binary else None))
        stamp = json.dumps(stamp_parts)
        with self._lock:
            if self._binary_cache is None or self._binary_cache[0] != stamp:
                self._binary_cache = (
                    stamp,
                    {kind: binary_status(kind, config) for kind in ("fetch", "store")},
                )
            binaries = copy.deepcopy(self._binary_cache[1])
        jobs = self.list_jobs()
        database = database_inventory(config["db"])
        return {
            "config": public_config(config),
            "config_public": public_config(config),
            "binaries": binaries,
            "database": database,
            "lake": lake_inventory(config["lake"]),
            "jobs": jobs,
            "closed": self._closed,
        }

    def plan(self, overrides: dict | None = None) -> dict:
        return build_plan(self._config(overrides), "sync")

    def _requests(self) -> dict:
        path = self.directory / "requests.json"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise ConfigError("幂等状态不能是符号链接")
        return read_json(path)

    def start(
        self, operation: str, overrides: dict | None = None, *, idempotency_key: str | None = None
    ) -> dict:
        from ptrade_sim.data_tools import OPERATIONS

        if operation not in OPERATIONS:
            raise ConfigError("未支持的数据操作")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 256
            or any(c in idempotency_key for c in "\r\n\x00")
        ):
            raise ConfigError("Idempotency-Key 必须是 1..256 字符的单行值")
        config = resolve_window(self._config(overrides))
        fingerprint = _fingerprint(operation, config)
        with self._lock:
            if self._closed:
                raise QueueTimeoutError("数据任务管理器已经关闭")
            if self.directory.is_symlink():
                raise ConfigError("数据任务根目录不能是符号链接")
            requests = self._requests()
            key_hash = (
                hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
            )
            if key_hash and key_hash in requests:
                previous = requests[key_hash]
                if previous.get("fingerprint") != fingerprint:
                    raise ConfigError("同一个 Idempotency-Key 不能用于不同任务参数")
                record = self._read(previous["id"])
                if record is None:
                    raise DataError("幂等记录对应的任务状态缺失")
                return self._require_job(record["id"])
            for existing in self.list_jobs():
                if (
                    existing.get("status") not in _TERMINAL
                    and existing.get("fingerprint") == fingerprint
                ):
                    if key_hash:
                        requests[key_hash] = {"fingerprint": fingerprint, "id": existing["id"]}
                        atomic_json(self.directory / "requests.json", requests)
                    return existing
            job_id = uuid.uuid4().hex
            directory = self.directory / job_id
            directory.mkdir(parents=True, exist_ok=False)
            atomic_json(directory / "config.json", config)
            record = {
                "id": job_id,
                "operation": operation,
                "status": "queued",
                "phase": "queued",
                "fingerprint": fingerprint,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "progress": {"percent": 0, "current": 0, "total": 1, "message": "已排队"},
                "config": public_config(config),
                "counts": {},
                "errors": [],
                "coverage": [],
                "report": None,
                "log_tail": [],
            }
            self._write(record)
            if key_hash:
                requests[key_hash] = {"fingerprint": fingerprint, "id": job_id}
                atomic_json(self.directory / "requests.json", requests)
            event = threading.Event()
            self._events[job_id] = event
            self._futures[job_id] = self._executor.submit(
                self._run, job_id, config, operation, event
            )
            return copy.deepcopy(record)

    def _run(self, job_id: str, config: dict, operation: str, event: threading.Event) -> None:
        with self._lock:
            record = self._read(job_id)
            if record is None or record["status"] == "cancelled":
                return
            record.update({"status": "running", "phase": "starting", "started_at": _now()})
            self._write(record)
        directory = self.directory / job_id
        try:
            outcome = run_data_pipeline(
                config,
                operation,
                directory=directory,
                cancel_event=event,
                report_path=directory / "report.json",
                progress_path=directory / "progress.json",
            )
            status = "cancelled" if event.is_set() else outcome.report["status"]
            with self._lock:
                record = self._read(job_id)
                if record is not None:
                    record.update(
                        {
                            "status": status,
                            "phase": "finished",
                            "finished_at": _now(),
                            "counts": outcome.report.get("counts", {}),
                            "errors": outcome.report.get("errors", []),
                            "coverage": outcome.report.get("coverage", []),
                            "report": outcome.report,
                            "exit_code": 130 if status == "cancelled" else outcome.exit_code,
                        }
                    )
                    self._write(record)
        except Exception as exc:
            # Worker boundary records defects as failures, never an empty successful run.
            with self._lock:
                record = self._read(job_id)
                if record is not None:
                    record.update(
                        {
                            "status": "cancelled" if event.is_set() else "failed",
                            "phase": "finished",
                            "finished_at": _now(),
                            "errors": [{"kind": type(exc).__name__, "message": str(exc)}],
                            "exit_code": 130 if event.is_set() else exit_code_for(exc),
                        }
                    )
                    self._write(record)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._read(job_id)
            if record is None:
                return None
            directory = self.directory / job_id
            record = copy.deepcopy(record)
            progress_path = directory / "progress.json"
            if progress_path.is_file() and not progress_path.is_symlink():
                try:
                    progress = read_json(progress_path)
                    completed = progress.get("completed", 0)
                    total = progress.get("total", 1)
                    fraction = progress.get("percent", 100 * completed / max(1, total))
                    percent = max(0, min(100, float(fraction)))
                    phase = progress.get("phase", record.get("phase", ""))
                    native_path = directory / (str(phase) + "_progress.json")
                    if (
                        phase in {"fetch", "ingest", "verify"}
                        and native_path.is_file()
                        and not native_path.is_symlink()
                    ):
                        native = read_json(native_path)
                        current = native.get("completed", native.get("current", 0))
                        maximum = native.get("total", 0)
                        if maximum and record["status"] == "running":
                            percent = max(
                                0,
                                min(
                                    100,
                                    100 * (completed + min(1, current / maximum)) / max(1, total),
                                ),
                            )
                    record["progress"] = {
                        "percent": percent,
                        "current": completed,
                        "total": total,
                        "message": progress.get("message", str(phase)),
                    }
                    if record["status"] not in _TERMINAL:
                        record["phase"] = phase
                except (DataError, OSError, TypeError, ValueError):
                    record["progress_error"] = "progress_unavailable"
            if record["status"] == "succeeded":
                record["progress"] = {"percent": 100, "current": 1, "total": 1, "message": "已完成"}
            if record["status"] in {"queued", "running"} and job_id not in self._futures:
                # After restart only retained state is known; never signal a saved PID.
                record["status"] = "failed"
                record["errors"] = [
                    {
                        "kind": "worker_not_owned",
                        "message": "管理器重启后未持有原任务进程，不能凭 PID 恢复或取消",
                    }
                ]
                record["phase"] = "unavailable"
            record["log_tail"] = _log_tail(directory)
            return record

    def list_jobs(self) -> list[dict]:
        if not self.directory.is_dir() or self.directory.is_symlink():
            return []
        records = []
        for directory in self.directory.iterdir():
            if directory.is_dir() and not directory.is_symlink() and _ID.fullmatch(directory.name):
                record = self.get_job(directory.name)
                if record is not None:
                    records.append(record)
        records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return records[:100]

    def wait_job(self, job_id: str, timeout: float | None = None) -> dict:
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)
        return self._require_job(job_id)

    def cancel_job(self, job_id: str) -> dict:
        with self._lock:
            record = self._read(job_id)
            if record is None:
                raise ConfigError("数据任务不存在")
            if record["status"] in _TERMINAL:
                return self._require_job(job_id)
            event, future = self._events.get(job_id), self._futures.get(job_id)
            if event is None or future is None:
                raise QueueTimeoutError("管理器不持有该任务进程，不能按历史 PID 取消")
            event.set()
            if future.cancel():
                record.update(
                    {
                        "status": "cancelled",
                        "phase": "finished",
                        "finished_at": _now(),
                        "exit_code": 130,
                    }
                )
                self._write(record)
            return self._require_job(job_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for event in self._events.values():
                event.set()
            for job_id, future in self._futures.items():
                if future.cancel():
                    record = self._read(job_id)
                    if record is not None and record["status"] not in _TERMINAL:
                        record.update(
                            {
                                "status": "cancelled",
                                "phase": "finished",
                                "finished_at": _now(),
                                "exit_code": 130,
                            }
                        )
                        self._write(record)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
