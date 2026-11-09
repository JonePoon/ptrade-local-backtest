"""Optional native minute-row indexing, selected by explicit configuration.

The runtime keeps its numpy implementation when this factory returns None.
Only a missing extension permits automatic fallback; incompatible installations
remain observable through metadata and DependencyError.
"""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Literal, Protocol, cast

import numpy as np
from numpy.typing import NDArray

from ptrade_sim.exceptions import ConfigError, DataError, DependencyError

_NATIVE_MODULE = "_ptrade_sim_native"
_INDEX_API_VERSION = 1
Backend = Literal["auto", "python", "rust"]


class MinuteIndex(Protocol):
    """Structural contract consumed by DayMinuteData, not a Python fallback."""

    @property
    def nbytes(self) -> int:
        """Owned Rust allocation payload, excluding borrowed numpy inputs."""
        ...

    def row_of(self, code: str, slot_idx: int) -> int:
        """Return the first exact row, or -1."""
        ...

    def rows_upto(self, code: str, slot_idx: int, include: bool) -> tuple[int, int]:
        """Return half-open bounds with left/right searchsorted semantics."""
        ...

    def rows_of(self, codes: Sequence[str], slot_idx: int) -> list[int]:
        """Return one exact row per code, preserving order and duplicates."""
        ...


@dataclass(frozen=True)
class _NativeState:
    state: Literal["available", "missing", "incompatible"]
    module: ModuleType | None = None
    version: str | None = None
    reason: str | None = None
    error: Exception | None = None


def _check_backend(backend: str) -> Backend:
    if not isinstance(backend, str) or backend.strip().lower() not in ("auto", "python", "rust"):
        raise ConfigError(f"acceleration backend 必须为 auto/python/rust，实际为 {backend!r}")
    return cast(Backend, backend.strip().lower())


def _probe_native() -> _NativeState:
    try:
        native = import_module(_NATIVE_MODULE)
    except ModuleNotFoundError as exc:
        # A missing transitive dependency is NOT an absent optional module.
        if exc.name == _NATIVE_MODULE:
            return _NativeState(
                "missing",
                reason=f"未安装可选扩展 {_NATIVE_MODULE}（ptrade-local-backtest-native wheel）",
                error=exc,
            )
        return _NativeState(
            "incompatible", reason=f"扩展依赖导入失败：{type(exc).__name__}: {exc}", error=exc
        )
    except Exception as exc:
        # Scoped to the import boundary. No import failure is silently treated
        # as Python fallback; its type/message/cause remain available to callers.
        return _NativeState(
            "incompatible", reason=f"扩展加载失败：{type(exc).__name__}: {exc}", error=exc
        )

    raw_version = getattr(native, "__version__", None)
    version = raw_version if isinstance(raw_version, str) and raw_version else None
    api_version = getattr(native, "INDEX_API_VERSION", None)
    cls = getattr(native, "MinuteIndex", None)
    problems = []
    if type(api_version) is not int or api_version != _INDEX_API_VERSION:
        problems.append(f"INDEX_API_VERSION={api_version!r}，需要 {_INDEX_API_VERSION}")
    if version is None:
        problems.append("缺少非空字符串 __version__")
    if not callable(cls):
        problems.append("缺少 MinuteIndex 构造器")
    else:
        for name in ("row_of", "rows_upto", "rows_of"):
            if not callable(getattr(cls, name, None)):
                problems.append(f"MinuteIndex 缺少 {name}")
        if getattr(cls, "nbytes", None) is None:
            problems.append("MinuteIndex 缺少 nbytes")
    if problems:
        reason = "原生索引接口不兼容：" + "; ".join(problems)
        return _NativeState("incompatible", native, version, reason, ImportError(reason))
    return _NativeState("available", native, version, "Rust 原生分钟索引（API 1）")


def backend_status(backend: str = "auto") -> dict:
    """JSON-ready status without constructing an index or reading environment.

    Explicit python deliberately does not import/probe Rust. In that case
    available=False means no native backend was selected/probed, state=disabled.
    For auto/rust, available describes an importable, API-compatible extension.
    """
    requested = _check_backend(backend)
    if requested == "python":
        return {
            "requested": requested,
            "effective": "python",
            "available": False,
            "version": None,
            "reason": "explicit Python backend",
            "state": "disabled",
            "module": _NATIVE_MODULE,
        }
    state = _probe_native()
    available = state.state == "available"
    effective = "rust" if available else None
    if requested == "auto" and state.state == "missing":
        effective = "python"
    return {
        "requested": requested,
        "effective": effective,
        "available": available,
        "version": state.version,
        "reason": state.reason,
        "state": state.state,
        "module": _NATIVE_MODULE,
    }


def make_minute_index(
    slot: NDArray[np.int32],
    starts: Mapping[str, tuple[int, int]],
    slot_count: int | None = None,
    *,
    backend: str = "auto",
) -> MinuteIndex | None:
    """Build the optional immutable index, or None for the numpy runtime path.

    Native input is one-dimensional native-endian int32, sorted within each
    code's valid half-open range. Strided/read-only arrays are accepted and
    copied. slot_count is optional positive axis metadata, never a query clamp.
    Construction is O(N+C) for disjoint ranges; persistent storage is O(N+C).
    """
    requested = _check_backend(backend)
    if requested == "python":
        return None
    state = _probe_native()
    if state.state == "missing" and requested == "auto":
        return None
    if state.state != "available":
        raise DependencyError(state.reason) from state.error

    if not isinstance(slot, np.ndarray):
        raise TypeError("minute slot 必须为 numpy.ndarray")
    if slot.ndim != 1 or slot.dtype != np.dtype(np.int32):
        raise DataError("minute slot 必须为一维 native-endian int32 numpy 数组")
    if slot_count is not None:
        try:
            slot_count = operator.index(slot_count)
        except TypeError as exc:
            raise DataError("slot_count 必须为正整数或 None") from exc
        if slot_count <= 0:
            raise DataError("slot_count 必须为正整数或 None")
    native = state.module
    assert native is not None  # Internal loader invariant, not input validation.
    try:
        return native.MinuteIndex(slot, dict(starts), slot_count)
    except (ValueError, OverflowError) as exc:
        raise DataError(f"非法分钟索引：{exc}") from exc
