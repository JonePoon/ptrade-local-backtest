"""Native data-chain configuration, process execution and read-only inventory.

Data algorithms are in ptrade-fetch/ptrade-store. This module is the Python
control boundary used by the CLI and dashboard, never a replacement ETL engine.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ptrade_sim.exceptions import ConfigError, DataError, DependencyError, exit_code_for

DATASETS = frozenset(
    {
        "daily_bars",
        "index_bars",
        "minute_bars",
        "minute_bars_5m",
        "instruments",
        "trading_calendar",
        "adj_factors",
        "corporate_actions",
        "index_constituents",
        "trading_status",
    }
)
NATIVE_FETCH_DATASETS = frozenset({"daily_bars", "index_bars", "minute_bars", "minute_bars_5m"})
NATIVE_STORE_DATASETS = DATASETS - {"corporate_actions"}
OPERATIONS = frozenset({"fetch", "ingest", "sync", "verify"})
PUBLIC_OVERRIDES = frozenset({"start", "end", "symbols", "datasets"})
_SYMBOL = re.compile(r"^[0-9]{6}\.(SH|SS|SZ|BJ)$", re.I)
_SECRET = re.compile(r"password|secret|token|authorization|cookie|headers", re.I)
DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "lake": "../cne_lake/lake",
    "db": "../cne_lake/out/fixed.duckdb",
    "start": None,
    "end": None,
    "trailing_days": 12,
    "symbols": [],
    "datasets": [
        "daily_bars",
        "index_bars",
        "minute_bars",
        "minute_bars_5m",
        "corporate_actions",
        "instruments",
        "trading_calendar",
        "adj_factors",
        "index_constituents",
        "trading_status",
    ],
    "provider": "tdx",
    "tdx_hosts": [],
    "concurrency": 4,
    "timeout_secs": 20,
    "retries": 3,
    "requests_per_second": 4,
    "max_pages": 64,
    "page_size": 800,
    "threads": 2,
    "memory_limit": "2GB",
    "require_volume_v2": True,
    "allow_partial": False,
    "resume": True,
    "refresh": True,
    "fetch_exe": None,
    "store_exe": None,
    "reference_refresh": {
        "enabled": False,
        "python": "../cne_lake/.venv/Scripts/python.exe",
        "config": "../cne_lake/lake.toml",
        "timeout_secs": 1800,
        "before": [
            "instruments",
            "trading_calendar",
            "trading_status",
            "corporate_actions",
            "index_constituents",
        ],
        "after": ["derive_adj_factors"],
    },
}
ENV_KEYS = {
    "PT_DATA_LAKE": "lake",
    "PT_DATA_DB": "db",
    "PT_DATA_START": "start",
    "PT_DATA_END": "end",
    "PT_DATA_PROVIDER": "provider",
    "PT_DATA_FETCH_EXE": "fetch_exe",
    "PT_DATA_STORE_EXE": "store_exe",
}


def repository_root() -> Path:
    here = Path(__file__).resolve().parent
    candidate = here.parent.parent
    return candidate if (candidate / "pyproject.toml").exists() else Path.cwd().resolve()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict:
    if path.stat().st_size > max_bytes:
        raise DataError(f"JSON 文件超过预算：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DataError(f"JSON 读取失败：{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise DataError(f"JSON 顶层必须是对象：{path}")
    return value


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _path(value: str | Path, base: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def validate_overrides(overrides: dict | None) -> dict:
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ConfigError("overrides 必须是对象")
    unexpected = set(overrides) - PUBLIC_OVERRIDES
    if unexpected:
        raise ConfigError("任务不接受配置路径/可执行文件/URL覆盖：" + ", ".join(sorted(unexpected)))
    return copy.deepcopy(overrides)


def _date(value: Any, key: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须为 YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ConfigError(f"{key} 必须为 YYYY-MM-DD：{value}") from exc


def load_data_config(
    path: str | Path | None = None, *, overrides: dict | None = None, use_env: bool = True
) -> dict:
    root = repository_root()
    given = path or (os.environ.get("PT_DATA_CONFIG") if use_env else None)
    if given is None:
        for candidate in (
            Path.cwd() / "ptrade_data_config.json",
            root / "ptrade_data_config.json",
            root / "data_config.example.json",
            Path(__file__).parent / "data_config.example.json",
        ):
            if candidate.is_file():
                given = candidate
                break
    config = copy.deepcopy(DEFAULTS)
    base = root
    source = None
    if given is not None:
        source = Path(given).expanduser().resolve()
        if not source.is_file():
            raise ConfigError(f"数据配置不存在：{source}")
        config = _merge(config, read_json(source))
        base = source.parent
    if use_env:
        for name, key in ENV_KEYS.items():
            if name in os.environ:
                config[key] = os.environ[name]
    if overrides:
        config = _merge(config, overrides)
    for key in ("lake", "db"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ConfigError(f"数据配置缺少 {key}")
        config[key] = _path(config[key], base)
    for key in ("fetch_exe", "store_exe"):
        if config.get(key):
            config[key] = _path(config[key], base)
    for key in ("start", "end"):
        config[key] = _date(config.get(key), key)
    if config["start"] and config["end"] and config["start"] > config["end"]:
        raise ConfigError("start 不能晚于 end")
    for key in ("symbols", "datasets"):
        values = config.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ConfigError(f"{key} 必须是字符串数组")
        config[key] = list(dict.fromkeys(values))
    normalized = []
    for symbol in config["symbols"]:
        symbol = symbol.upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ConfigError(f"非法证券代码：{symbol}")
        normalized.append(symbol[:-3] + ".SH" if symbol.endswith(".SS") else symbol)
    config["symbols"] = list(dict.fromkeys(normalized))
    unknown = set(config["datasets"]) - DATASETS
    if unknown or not config["datasets"]:
        raise ConfigError(f"未支持或为空的数据集：{sorted(unknown)}")
    for key in (
        "trailing_days",
        "concurrency",
        "threads",
        "max_pages",
        "page_size",
    ):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"{key} 必须为正整数")
    retries = config.get("retries")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 32:
        raise ConfigError("retries 必须是 0..32 的整数")
    for key, maximum in (("timeout_secs", 3600), ("requests_per_second", 100_000)):
        value = config.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.001 <= value <= maximum
        ):
            raise ConfigError(f"{key} 必须为 0.001..{maximum} 的有限数字")
    if (
        config["concurrency"] > 1024
        or config["page_size"] > 800
        or (config["max_pages"] - 1) * config["page_size"] > 65535
    ):
        raise ConfigError("TDX 并发须≤1024、单页≤800、分页起始偏移须≤65535")
    for key in ("allow_partial", "require_volume_v2", "resume", "refresh"):
        if type(config.get(key)) is not bool:
            raise ConfigError(f"{key} 必须是布尔值")
    references = config.get("reference_refresh")
    if not isinstance(references, dict) or type(references.get("enabled")) is not bool:
        raise ConfigError("reference_refresh.enabled 必须是布尔值")
    for key in ("python", "config"):
        if not isinstance(references.get(key), str) or not references[key]:
            raise ConfigError(f"reference_refresh.{key} 必须为路径字符串")
        references[key] = _path(references[key], base)
    timeout = references.get("timeout_secs")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ConfigError("reference_refresh.timeout_secs 必须为有限正数")
    for key in ("before", "after"):
        if not isinstance(references.get(key), list) or any(
            not isinstance(x, str) or not re.fullmatch(r"[a-z0-9_]+", x) for x in references[key]
        ):
            raise ConfigError(f"reference_refresh.{key} 非法")
    config["_config_path"] = str(source) if source else None
    config["_base_dir"] = str(base)
    return config


def public_config(value: Any, key: str = "") -> Any:
    if _SECRET.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            k: public_config(v, str(k))
            for k, v in value.items()
            if not str(k).startswith("_") and k not in {"fetch_exe", "store_exe"}
        }
    if isinstance(value, list):
        return [public_config(v, key) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host += ":" + str(parts.port)
        names = [item.split("=", 1)[0] for item in parts.query.split("&") if item]
        return urlunsplit((parts.scheme, host, parts.path, "&".join(names), ""))
    return value


def resolve_binary(kind: str, config: dict) -> Path | None:
    if kind not in {"fetch", "store"}:
        raise ConfigError("未知原生组件")
    explicit = config.get(kind + "_exe")
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None
    name = "ptrade-" + kind
    suffix = ".exe" if os.name == "nt" else ""
    root = repository_root()
    candidates = [
        Path(__file__).parent / "bin" / (name + suffix),
        root / ("native-" + kind) / "target" / "release" / (name + suffix),
    ]
    on_path = shutil.which(name)
    if on_path:
        candidates.append(Path(on_path))
    return next((p.resolve() for p in candidates if p.is_file()), None)


def binary_status(kind: str, config: dict, *, probe: bool = True) -> dict:
    path = resolve_binary(kind, config)
    result: dict[str, Any] = {
        "path": str(path) if path else None,
        "exists": path is not None,
        "available": False,
        "version": None,
        "error": None,
    }
    if path is None:
        result["error"] = (
            f"未构建 ptrade-{kind}，执行 cargo build --release --manifest-path native-{kind}/Cargo.toml"
        )
        return result
    if not probe:
        result["error"] = "startup_not_probed"
        return result
    try:
        completed = subprocess.run(
            [str(path), "doctor"],
            capture_output=True,
            timeout=20,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        report = json.loads(completed.stdout)
        if not isinstance(report, dict):
            raise ValueError("doctor JSON 必须是对象")
        capabilities = report.get("capabilities")
        version = capabilities.get("version") if isinstance(capabilities, dict) else None
        result.update(
            {
                "available": _normalized_status(report, completed.returncode) == "succeeded",
                "version": report.get("version") or report.get("engine_version") or version,
                "report": report,
            }
        )
        if not result["available"]:
            result["error"] = "doctor_failed: " + completed.stderr[-1000:]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
    return result


def lake_inventory(lake: str) -> dict:
    root = Path(lake)
    datasets = {}
    for directory in (root / "curated", root / "derived"):
        if directory.is_dir():
            for dataset in directory.iterdir():
                if dataset.is_dir() and dataset.name in DATASETS:
                    files = list(dataset.rglob("*.parquet"))
                    datasets[dataset.name] = {
                        "files": len(files),
                        "bytes": sum(p.stat().st_size for p in files if p.is_file()),
                    }
    return {"path": str(root), "exists": root.is_dir(), "datasets": datasets}


def database_inventory(db: str) -> dict:
    path = Path(db)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "busy": False,
        "tables": [],
        "error": None,
    }
    if not result["exists"]:
        return result
    try:
        import duckdb
    except ImportError as exc:
        result["error"] = str(exc)
        return result
    try:
        connection = duckdb.connect(str(path), read_only=True)
        try:
            for (name,) in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall():
                if not re.fullmatch(r"ashare_[a-z0-9_]+", name):
                    continue
                quoted = '"' + name + '"'
                count = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                columns = {row[0] for row in connection.execute(f"DESCRIBE {quoted}").fetchall()}
                bounds = (
                    connection.execute(f"SELECT min(date),max(date) FROM {quoted}").fetchone()
                    if "date" in columns
                    else (None, None)
                )
                # DuckDB DATE/TIMESTAMP 是 Python 对象；HTTP JSONResponse 不会替我们编码。
                bounds = tuple(
                    value.isoformat()
                    if isinstance(value, (dt.date, dt.datetime))
                    else (str(value) if value is not None else None)
                    for value in bounds
                )
                result["tables"].append(
                    {"name": name, "rows": count, "start": bounds[0], "end": bounds[1]}
                )
        finally:
            connection.close()
    except (duckdb.Error, OSError) as exc:
        result.update({"error": str(exc), "busy": "lock" in str(exc).lower()})
    return result


def resolve_window(config: dict) -> dict:
    result = copy.deepcopy(config)
    if result.get("end") is None:
        today = dt.date.today()
        calendar_files = list(
            (Path(result["lake"]) / "curated" / "trading_calendar").rglob("*.parquet")
        )
        resolved = None
        if calendar_files:
            import duckdb

            connection = duckdb.connect()
            try:
                row = connection.execute(
                    "SELECT max(try_cast(trade_date AS DATE)) FROM read_parquet(?, hive_partitioning=false, union_by_name=true) WHERE is_trading AND try_cast(trade_date AS DATE)<=?",
                    [[str(path) for path in calendar_files], today],
                ).fetchone()
                resolved = row[0] if row else None
            finally:
                connection.close()
        if resolved is None:
            while today.weekday() >= 5:
                today -= dt.timedelta(days=1)
            resolved = today
            result["_calendar_basis"] = "weekday_fallback"
        else:
            result["_calendar_basis"] = "source_trading_calendar"
        result["end"] = resolved.isoformat()
    if result.get("start") is None:
        result["start"] = (
            dt.date.fromisoformat(result["end"]) - dt.timedelta(days=result["trailing_days"])
        ).isoformat()
    return result


def _pipeline_stages(config: dict, operation: str) -> list[str]:
    if operation not in OPERATIONS:
        raise ConfigError("未知数据操作")
    has_bars = bool(set(config["datasets"]) & NATIVE_FETCH_DATASETS)
    if operation == "fetch" and not has_bars:
        raise ConfigError("Rust 下载步骤需要至少一个日线或分钟数据集")
    refresh = config["reference_refresh"]
    steps = []
    if refresh["enabled"] and operation in {"sync", "fetch"}:
        steps.append("reference_before")
    if has_bars and operation in {"sync", "fetch"}:
        steps.append("fetch")
        if refresh["enabled"] and refresh["after"]:
            steps.append("reference_after")
    if operation in {"sync", "ingest", "verify"}:
        if "reference_after" not in steps:
            steps.append("reference_resolve")
        steps.extend(["ingest", "verify"] if operation == "sync" else [operation])
    return steps


def build_plan(config: dict, operation: str = "sync") -> dict:
    config = resolve_window(config)
    steps = _pipeline_stages(config, operation)
    needed = []
    if "fetch" in steps:
        needed.append("fetch")
    if any(step in {"ingest", "verify"} for step in steps):
        needed.append("store")
    binaries = {kind: binary_status(kind, config) for kind in needed}
    missing = [kind for kind, item in binaries.items() if not item["available"]]
    return {
        "config": public_config(config),
        "binaries": binaries,
        "operation": operation,
        "steps": steps,
        "datasets": config["datasets"],
        "symbols": config["symbols"],
        "start": config["start"],
        "end": config["end"],
        "ready": not missing,
        "missing": missing,
        "will_download": "fetch" in steps,
        "will_write": operation in {"ingest", "sync"},
        "lake": lake_inventory(config["lake"]),
    }


@dataclass
class DataOutcome:
    report: dict
    exit_code: int


def _normalized_status(report: dict, returncode: int) -> str:
    status = str(report.get("status", "")).lower()
    # Native cancellation may exit before an atomic final report is published.
    if returncode == 130 or status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"ok", "success", "succeeded"} and returncode == 0:
        return "succeeded"
    if status in {"partial", "incomplete"} and returncode in {0, 4}:
        return "partial"
    return "failed"


def _tail(path: Path, size: int = 8192) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - size))
        return fh.read(size).decode("utf-8", errors="replace")


class _WindowsProcessJob:
    """Kill-on-close applies only to the process handle we just created."""

    def __init__(self, process: subprocess.Popen):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Io(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", Io),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process_handle = int(process._handle)  # type: ignore[attr-defined]  # Windows durable process HANDLE.
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ) or not self.kernel.AssignProcessToJobObject(self.handle, process_handle):
            error = ctypes.get_last_error()
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise ctypes.WinError(error)

    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 130)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_process(
    command: list[str],
    *,
    log_path: Path,
    cancel_event: threading.Event | None = None,
    timeout: float | None = None,
) -> int:
    """One owned process group; no shell, polling is a bounded cancellation wait."""
    if cancel_event is not None and cancel_event.is_set():
        return 130
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    with log_path.open("ab") as log:
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=environment, **options
        )
        job = None
        if os.name == "nt":
            try:
                job = _WindowsProcessJob(process)
            except OSError:
                process.terminate()
                process.wait(timeout=5)
                raise
        started = time.monotonic()
        try:
            while True:
                try:
                    return process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    if (cancel_event is not None and cancel_event.is_set()) or (
                        timeout is not None and time.monotonic() - started > timeout
                    ):
                        if os.name == "nt":
                            assert job is not None
                            job.terminate()
                        else:
                            os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]  # POSIX-only branch.
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        return 130 if cancel_event is not None and cancel_event.is_set() else 124
        finally:
            if job is not None:
                job.close()  # 清理此根进程的所有仍存活后代，不按不可信PID追杀。
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def _stage_report(path: Path, stage: str, code: int, log: Path) -> dict:
    if path.is_symlink():
        raise DataError("任务报告不能是符号链接")
    report = (
        read_json(path)
        if path.is_file()
        else {
            "status": "failed",
            "errors": [{"kind": "native_report_missing", "message": _tail(log), "exit_code": code}],
        }
    )
    for key in ("errors", "coverage", "warnings", "files"):
        if key in report and not isinstance(report[key], list):
            raise DataError(f"{stage} 报告 {key} 必须是数组")
    if "counts" in report and not isinstance(report["counts"], dict):
        raise DataError(f"{stage} 报告 counts 必须是对象")
    report["status"] = _normalized_status(report, code)
    report["operation"] = stage
    return report


def _reference_connector(
    config: dict, stage: str, directory: Path, cancel_event: threading.Event | None
) -> dict:
    refresh = config["reference_refresh"]
    python = Path(refresh["python"])
    if not python.is_file():
        if stage == "reference_after":
            raise DependencyError("after 复权需要已配置的 CNEquity SDK 解释器")
        python = Path(sys.executable)
    script = Path(__file__).with_name("data_reference.py")
    if not script.is_file():
        raise DependencyError("缺少 CNEquity revision connector 模块")
    if stage == "reference_after" and set(refresh["after"]) - {"derive_adj_factors"}:
        raise ConfigError("reference_refresh.after 仅支持独立 merged-view derive_adj_factors")
    snapshot = directory / "reference_config.json"
    atomic_json(snapshot, {k: v for k, v in config.items() if not k.startswith("_")})
    report_path = directory / (stage + "_report.json")
    report_path.unlink(missing_ok=True)  # 不把上一次报告误作本次进程的成功证据。
    log = directory / (stage + ".log")
    command = [
        str(python),
        str(script),
        "--config",
        str(snapshot),
        "--stage",
        "after" if stage == "reference_after" else "resolve",
        "--report",
        str(report_path),
        "--work-dir",
        str(directory / "reference_work"),
    ]
    code = run_process(
        command, log_path=log, cancel_event=cancel_event, timeout=refresh["timeout_secs"]
    )
    report = _stage_report(report_path, stage, code, log)
    if report["status"] == "succeeded":
        roots, native = report.get("dataset_roots"), report.get("native_files")
        if not isinstance(roots, dict) or not isinstance(native, dict):
            raise DataError("revision connector 缺少 dataset_roots/native_files")
        if any(
            not isinstance(paths, list) or any(not isinstance(path, str) for path in paths)
            for paths in roots.values()
        ):
            raise DataError("revision connector 数据根必须是文件路径数组")
        if any(
            not isinstance(files, list) or any(not isinstance(item, dict) for item in files)
            for files in native.values()
        ):
            raise DataError("revision connector native_files 必须是清单数组")
        if any(dataset not in roots for dataset in config["datasets"]):
            raise DataError("revision connector 未明确解析所有请求数据集")
    report["exit_code"] = code
    return report


def _toml_override(text: str, table: str, key: str, value: str) -> str:
    """Replace one scalar in its own table without changing other SDK options."""
    header = re.compile(r"(?m)^[ \t]*\[" + re.escape(table) + r"\][ \t]*(?:#[^\n]*)?$")
    matches = list(header.finditer(text))
    line = key + " = " + json.dumps(value, ensure_ascii=True) + "\n"
    if not matches:
        return text.rstrip() + "\n\n[" + table + "]\n" + line
    if len(matches) != 1:
        raise ConfigError("SDK TOML 重复表：" + table)
    start = matches[0].end()
    following = re.search(r"(?m)^[ \t]*\[", text[start:])
    end = start + following.start() if following else len(text)
    body = text[start:end]
    assignment = re.compile(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=.*$")
    body = (
        assignment.sub(lambda match: line.rstrip(), body, count=1)
        if assignment.search(body)
        else "\n" + line + body
    )
    return text[:start] + body + text[end:]


def _reference_stage(
    config: dict, stage: str, directory: Path, cancel_event: threading.Event | None
) -> dict:
    if stage != "reference_before":
        return _reference_connector(config, stage, directory, cancel_event)
    refresh = config["reference_refresh"]
    steps = refresh["before"]
    if not steps:
        return {"status": "succeeded", "operation": stage, "counts": {}, "errors": []}
    python, original = Path(refresh["python"]), Path(refresh["config"])
    if not python.is_file() or not original.is_file():
        raise DependencyError("参考信息刷新需要已配置的 CNEquity 解释器及配置文件")
    allowed = {
        "instruments",
        "trading_calendar",
        "trading_status",
        "corporate_actions",
        "index_constituents",
        "share_structure",
    }
    if set(steps) - allowed:
        raise ConfigError("reference_refresh.before 仅接受参考信息步骤，不能重复下载 bars")
    original_text = original.read_text(encoding="utf-8")
    original_text = _toml_override(
        original_text, "data", "root", str(Path(config["lake"]).resolve())
    )
    # SDK 2.x 只拥有独立工作库路径，绝不把引擎1.5.6目标传给它。
    original_text = _toml_override(
        original_text, "duckdb", "path", str((directory / "reference-sdk.duckdb").resolve())
    )
    group = "ptrade_refs_" + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    addition = (
        f'\n[job.daily.groups.{group}]\nat = "16:00"\nparallel = false\nsteps = '
        + json.dumps(steps)
        + "\n"
    )
    fd, name = tempfile.mkstemp(prefix=".ptrade-reference-", suffix=".toml", dir=original.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(original_text + addition)
        command = [
            str(python),
            "-m",
            "cnequity",
            "run",
            "daily",
            "--group",
            group,
            "--trade-date",
            config["end"],
            "--config",
            str(temporary),
        ]
        returncode = run_process(
            command,
            log_path=directory / (stage + ".log"),
            cancel_event=cancel_event,
            timeout=refresh["timeout_secs"],
        )
        return {
            "status": "succeeded"
            if returncode == 0
            else ("cancelled" if returncode == 130 else "failed"),
            "operation": stage,
            "exit_code": returncode,
            "counts": {"network_requests": None, "bar_downloads": 0},
            "errors": []
            if returncode == 0
            else [
                {
                    "kind": "sdk_reference_failed",
                    "message": _tail(directory / (stage + ".log")),
                    "exit_code": returncode,
                }
            ],
        }
    finally:
        temporary.unlink(missing_ok=True)


def run_data_pipeline(
    config: dict,
    operation: str,
    *,
    directory: Path,
    cancel_event: threading.Event | None = None,
    report_path: Path | None = None,
    progress_path: Path | None = None,
) -> DataOutcome:
    """Transactional native stages; failed or partial runs never become success."""
    if operation not in OPERATIONS:
        raise ConfigError("未知数据操作")
    directory.mkdir(parents=True, exist_ok=True)
    config = resolve_window(config)
    config_path = directory / "effective_config.json"
    atomic_json(config_path, {k: v for k, v in config.items() if not k.startswith("_")})
    stages = _pipeline_stages(config, operation)
    reports = []
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    status = "succeeded"
    failure_code = 3
    errors: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        if cancel_event is not None and cancel_event.is_set():
            status = "cancelled"
            break
        if progress_path is not None:
            atomic_json(
                progress_path,
                {
                    "status": "running",
                    "phase": stage,
                    "completed": index,
                    "total": len(stages),
                    "percent": 100 * index / len(stages),
                },
            )
        try:
            if stage.startswith("reference_"):
                report = _reference_stage(config, stage, directory, cancel_event)
                code = int(report.get("exit_code", 0 if report["status"] == "succeeded" else 3))
                if report["status"] == "succeeded" and stage in {
                    "reference_resolve",
                    "reference_after",
                }:
                    config["dataset_roots"] = copy.deepcopy(report["dataset_roots"])
                    config["native_files"] = copy.deepcopy(report["native_files"])
                    atomic_json(
                        config_path, {k: v for k, v in config.items() if not k.startswith("_")}
                    )
            else:
                kind = "fetch" if stage == "fetch" else "store"
                executable = resolve_binary(kind, config)
                if executable is None:
                    raise DependencyError(f"缺少 ptrade-{kind}，请先构建 native-{kind}")
                stage_report, stage_progress = (
                    directory / (stage + "_report.json"),
                    directory / (stage + "_progress.json"),
                )
                native_config_path = config_path
                if stage == "fetch":
                    fetch_config = copy.deepcopy(config)
                    if not fetch_config.get("tdx_hosts"):
                        fetch_config.pop(
                            "tdx_hosts", None
                        )  # 使用原生已验证的默认主机，不显式传空数组。
                    fetch_config["datasets"] = [
                        d for d in config["datasets"] if d in NATIVE_FETCH_DATASETS
                    ]
                    if not fetch_config["datasets"]:
                        raise ConfigError("Rust 下载步骤需要至少一个日线或分钟数据集")
                    native_config_path = directory / "fetch_config.json"
                    atomic_json(
                        native_config_path,
                        {k: v for k, v in fetch_config.items() if not k.startswith("_")},
                    )
                else:
                    store_config = copy.deepcopy(config)
                    store_config["datasets"] = [
                        d for d in config["datasets"] if d in NATIVE_STORE_DATASETS
                    ]
                    if not store_config["datasets"]:
                        raise ConfigError(
                            "Rust 入库/校验步骤需要至少一个核心数据集；corporate_actions 仅为参考输入"
                        )
                    native_config_path = directory / "store_config.json"
                    atomic_json(
                        native_config_path,
                        {k: v for k, v in store_config.items() if not k.startswith("_")},
                    )
                command = [
                    str(executable),
                    stage,
                    "--config",
                    str(native_config_path),
                    "--report",
                    str(stage_report),
                    "--progress",
                    str(stage_progress),
                ]
                if stage_report.is_symlink() or stage_progress.is_symlink():
                    raise DataError("原生报告/进度不能是符号链接")
                stage_report.unlink(missing_ok=True)
                stage_progress.unlink(missing_ok=True)
                code = run_process(
                    command, log_path=directory / (stage + ".log"), cancel_event=cancel_event
                )
                report = _stage_report(stage_report, stage, code, directory / (stage + ".log"))
                report["exit_code"] = code
            reports.append(report)
            errors.extend(report.get("errors") or [])
            observed = report["status"]
            if observed in {"failed", "cancelled"}:
                status = observed
                failure_code = code if code in {1, 2, 3, 5, 6, 124} else 3
                break
            if observed == "partial":
                status = "partial"
                if not config["allow_partial"]:
                    break
        except (ConfigError, DataError, DependencyError, OSError, ValueError) as exc:
            errors.append({"kind": type(exc).__name__, "message": str(exc), "stage": stage})
            status = "failed"
            failure_code = exit_code_for(exc)
            break
    finished = dt.datetime.now(dt.timezone.utc).isoformat()
    result = {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "config": public_config(config),
        "stages": reports,
        "errors": errors,
        "counts": {"stages_completed": len(reports), "stages_total": len(stages)},
        "coverage": [row for report in reports for row in (report.get("coverage") or [])]
        + (
            [
                {
                    "dataset": "corporate_actions",
                    "role": "sdk_reference_input",
                    "native_core_table": False,
                }
            ]
            if "corporate_actions" in config["datasets"]
            else []
        ),
        "warnings": [row for report in reports for row in (report.get("warnings") or [])],
    }
    if report_path is not None:
        atomic_json(report_path, result)
    if progress_path is not None:
        atomic_json(
            progress_path,
            {
                "status": status,
                "phase": "finished",
                "completed": len(reports),
                "total": len(stages),
                "percent": 100 if status == "succeeded" else 100 * len(reports) / len(stages),
            },
        )
    code = (
        0
        if status == "succeeded" or (status == "partial" and config["allow_partial"])
        else (130 if status == "cancelled" else failure_code)
    )
    return DataOutcome(result, code)
