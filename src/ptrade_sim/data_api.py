"""数据任务 HTTP 边界：只读查询、同源写入、受限参数和持久幂等键。"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ptrade_sim.exceptions import ConfigError, DataError, DependencyError, QueueTimeoutError

Operation = Literal["sync", "fetch", "ingest", "verify"]
Dataset = Literal[
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
]


class Manager(Protocol):
    def status(self) -> dict: ...
    def plan(self, overrides: dict | None = None) -> dict: ...
    def start(
        self, operation: str, overrides: dict, *, idempotency_key: str | None = None
    ) -> dict: ...
    def list_jobs(self) -> list[dict]: ...
    def get_job(self, job_id: str) -> dict | None: ...
    def cancel_job(self, job_id: str) -> dict: ...
    def close(self) -> None: ...


class Overrides(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    symbols: list[str] | None = Field(None, max_length=10000)
    datasets: list[Dataset] | None = Field(None, min_length=1, max_length=10)

    @field_validator("symbols")
    @classmethod
    def symbols_are_identifiers(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        result = []
        seen: set[str] = set()
        for value in values:
            value = value.strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", value) or value in {".", ".."}:
                raise ConfigError("symbols 必须是标的代码，不接受路径、URL或命令")
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @model_validator(mode="after")
    def dates_are_valid(self) -> Overrides:
        for value in (self.start, self.end):
            if value is not None:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ConfigError("日期必须为有效的 YYYY-MM-DD") from exc
        if self.start and self.end and self.start > self.end:
            raise ConfigError("start 不得晚于 end")
        return self


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overrides: Overrides = Field(
        default_factory=lambda: Overrides(start=None, end=None, symbols=None, datasets=None)
    )


class StartRequest(PlanRequest):
    operation: Operation


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _http_error(status: int, message: str, code: str) -> HTTPException:
    return HTTPException(
        status, {"message": message, "code": code}, headers={"Cache-Control": "no-store"}
    )


def _require_origin(request: Request) -> None:
    """无 Origin 的本地/CLI客户端仍可调用；浏览器必须严格同源。"""
    origin = request.headers.get("origin")
    if origin is None:
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            raise _http_error(403, "跨站数据写入请求已拒绝", "origin_rejected")
        return
    try:
        supplied = urlsplit(origin)
        actual = urlsplit(str(request.url))
        supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
        actual_port = actual.port or (443 if actual.scheme == "https" else 80)
        same = (
            supplied.scheme in {"http", "https"}
            and not supplied.username
            and not supplied.password
            and not supplied.path
            and not supplied.query
            and not supplied.fragment
            and supplied.scheme == actual.scheme
            and supplied.hostname == actual.hostname
            and supplied_port == actual_port
        )
    except ValueError:
        same = False
    if not same:
        raise _http_error(403, "仅允许看板同源的数据写入请求", "origin_rejected")


def _public(value: Any) -> Any:
    """防御性去除配置中凭据字段；不把 headers/token/私密 URL 展示给浏览器。"""
    if isinstance(value, dict):
        return {
            key: _public(item)
            for key, item in value.items()
            if not any(
                word in str(key).lower()
                for word in (
                    "token",
                    "secret",
                    "password",
                    "header",
                    "credential",
                    "authorization",
                    "api_key",
                    "apikey",
                    "cookie",
                    "private_key",
                    "url",
                )
            )
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+", "[地址已隐藏]", value)
    return value


def _response(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        _public(value), status_code=status_code, headers={"Cache-Control": "no-store"}
    )


def _invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except QueueTimeoutError as exc:
        raise _http_error(409, str(exc), "data_busy") from exc
    except DependencyError as exc:
        raise _http_error(503, str(exc), "data_dependency_missing") from exc
    except (ConfigError, DataError) as exc:
        raise _http_error(400, str(exc), "data_config_error") from exc
    except FileNotFoundError as exc:
        raise _http_error(404, str(exc), "data_not_found") from exc
    except OSError as exc:
        raise _http_error(503, str(exc), "data_io_error") from exc


def _new_manager(root: Path) -> Manager:
    try:
        from ptrade_sim.data_jobs import DataManager
    except ModuleNotFoundError as exc:
        if exc.name in {"ptrade_sim.data_jobs", "ptrade_sim.data_tools"}:
            raise DependencyError("数据任务模块尚不可用") from exc
        raise
    return DataManager(root)


def _job_id(job_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise _http_error(404, "任务不存在", "data_not_found")


def create_data_router(root: Path, manager: Manager | None = None) -> APIRouter:
    """应用级惰性 manager；关闭时只交还其拥有的 worker。"""
    lock = Lock()
    holder = [manager]

    def get_manager() -> Manager:
        with lock:
            current = holder[0]
            if current is None:
                current = _invoke(_new_manager, root)
                holder[0] = current
            return current

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if holder[0] is not None:
                holder[0].close()

    router = APIRouter(prefix="/api/data", tags=["data"], lifespan=lifespan)

    @router.get("/status")
    def status() -> JSONResponse:
        return _response(_invoke(get_manager().status))

    @router.post("/plan", dependencies=[Depends(_require_origin)])
    def plan(body: PlanRequest = Body(default_factory=PlanRequest)) -> JSONResponse:
        return _response(_invoke(get_manager().plan, body.overrides.model_dump(exclude_none=True)))

    @router.get("/jobs")
    def jobs() -> JSONResponse:
        return _response({"jobs": _invoke(get_manager().list_jobs)})

    @router.post("/jobs", dependencies=[Depends(_require_origin)])
    def start(
        body: StartRequest,
        idempotency_key: str | None = Header(
            None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"
        ),
    ) -> JSONResponse:
        # key 交给持久化 manager，重启也不会重复启动同一请求。
        return _response(
            _invoke(
                get_manager().start,
                body.operation,
                body.overrides.model_dump(exclude_none=True),
                idempotency_key=idempotency_key,
            ),
            202,
        )

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str) -> JSONResponse:
        _job_id(job_id)
        value = _invoke(get_manager().get_job, job_id)
        if value is None:
            raise _http_error(404, "任务不存在", "data_not_found")
        return _response(value)

    @router.post("/jobs/{job_id}/cancel", dependencies=[Depends(_require_origin)])
    def cancel(
        job_id: str, body: EmptyRequest = Body(default_factory=EmptyRequest)
    ) -> JSONResponse:
        _job_id(job_id)
        current = get_manager()
        if _invoke(current.get_job, job_id) is None:
            raise _http_error(404, "任务不存在", "data_not_found")
        return _response(_invoke(current.cancel_job, job_id))

    return router
