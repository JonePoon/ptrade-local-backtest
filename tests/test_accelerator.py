"""原生索引与 numpy 的逐行对照；不连真实行情库。

回归重点：重复槽必须取首行；right bound 必须含全部重复；缺依赖才可
自动回退；原生自有副本必须计入 LRU。原生未安装时只跳过原生对照，
导入失败/API 不兼容不得被当成无依赖跳过。
"""

from __future__ import annotations

import gc
import json
import weakref
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from ptrade_sim import accelerator
from ptrade_sim.cache import Cache, estimate_bytes
from ptrade_sim.exceptions import ConfigError, DataError, DependencyError, exit_code_for

pytestmark = pytest.mark.unit


def _fake_native():
    class FakeIndex:
        def __init__(self, slot, starts, slot_count):
            self.args = slot, starts, slot_count

        @property
        def nbytes(self):
            return 64

        def row_of(self, code, slot_idx):
            return -1

        def rows_upto(self, code, slot_idx, include):
            return 0, 0

        def rows_of(self, codes, slot_idx):
            return [-1] * len(codes)

    return SimpleNamespace(INDEX_API_VERSION=1, __version__="0.1.0", MinuteIndex=FakeIndex)


def _missing_module(name):
    raise ModuleNotFoundError(f"No module named {name!r}", name=name)


@pytest.fixture
def native_factory():
    status = accelerator.backend_status("rust")
    if status["state"] == "missing":
        pytest.skip("原生对照需要安装 native crate 产出的 ptrade-sim-native wheel")
    assert status["state"] == "available", f"安装的扩展已损坏，不能静默 skip：{status}"

    def build(slot, starts, slot_count=None):
        result = accelerator.make_minute_index(slot, starts, slot_count, backend="rust")
        assert result is not None
        return result

    return build


def _reference_row(slot, starts, code, query):
    if code not in starts:
        return -1
    start, end = starts[code]
    offset = int(np.searchsorted(slot[start:end], query, side="left"))
    if offset < end - start and slot[start + offset] == query:
        return start + offset
    return -1


def _reference_bounds(slot, starts, code, query, include):
    if code not in starts:
        return 0, 0
    start, end = starts[code]
    offset = int(np.searchsorted(slot[start:end], query, side="right" if include else "left"))
    return start, start + offset


def test_explicit_python_never_imports_or_constructs_optional_extension(monkeypatch):
    def unexpected_import(name):
        pytest.fail(f"显式 Python 后端不得探查可选扩展：{name}")

    monkeypatch.setattr(accelerator, "import_module", unexpected_import)
    # This must not even inspect native input: the caller retains its numpy path.
    assert accelerator.make_minute_index(None, None, 241, backend="python") is None
    status = accelerator.backend_status("python")
    assert status == {
        "requested": "python",
        "effective": "python",
        "available": False,
        "version": None,
        "reason": "explicit Python backend",
        "state": "disabled",
        "module": "_ptrade_sim_native",
    }
    assert json.loads(json.dumps(status)) == status


def test_auto_falls_back_only_when_extension_itself_is_missing(monkeypatch):
    monkeypatch.setattr(accelerator, "import_module", _missing_module)
    assert accelerator.make_minute_index(None, None, backend="auto") is None
    status = accelerator.backend_status()
    assert status["requested"] == "auto"
    assert status["effective"] == "python"
    assert status["available"] is False
    assert status["state"] == "missing"
    assert status["version"] is None
    assert "_ptrade_sim_native" in status["reason"]
    assert json.loads(json.dumps(status, ensure_ascii=False)) == status


def test_required_rust_raises_existing_dependency_error_with_exit_code_six(monkeypatch):
    monkeypatch.setattr(accelerator, "import_module", _missing_module)
    with pytest.raises(DependencyError) as caught:
        accelerator.make_minute_index(None, None, backend="rust")
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)
    assert exit_code_for(caught.value) == 6
    status = accelerator.backend_status("rust")
    assert status["effective"] is None
    assert status["state"] == "missing"


@pytest.mark.parametrize("backend", ["Rusty", "", "  ", "native", None, 1])
def test_invalid_backend_is_configuration_error_before_any_native_import(monkeypatch, backend):
    monkeypatch.setattr(accelerator, "import_module", lambda name: pytest.fail("不应探查扩展"))
    for function in (
        lambda: accelerator.make_minute_index(None, None, backend=backend),
        lambda: accelerator.backend_status(backend),
    ):
        with pytest.raises(ConfigError) as caught:
            function()
        assert exit_code_for(caught.value) == 2


@pytest.mark.parametrize("backend", ["auto", "rust"])
@pytest.mark.parametrize(
    "failure",
    [
        ModuleNotFoundError("No module named 'native_dependency'", name="native_dependency"),
        ImportError("DLL load failed"),
        OSError("native library cannot load"),
        RuntimeError("native initialization failed"),
    ],
)
def test_import_failures_are_observable_and_never_auto_fallback(monkeypatch, backend, failure):
    def fail_import(name):
        raise failure

    monkeypatch.setattr(accelerator, "import_module", fail_import)
    status = accelerator.backend_status(backend)
    assert status["state"] == "incompatible"
    assert status["effective"] is None
    assert status["available"] is False
    assert type(failure).__name__ in status["reason"]
    assert str(failure) in status["reason"]
    assert json.loads(json.dumps(status)) == status
    with pytest.raises(DependencyError) as caught:
        accelerator.make_minute_index(None, None, backend=backend)
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize(
    "defect",
    ["api_version", "missing_api", "bool_api", "version", "constructor", "rows_of", "nbytes"],
)
def test_stale_or_incomplete_extension_is_not_misreported_as_absent(monkeypatch, defect):
    native = _fake_native()
    if defect == "api_version":
        native.INDEX_API_VERSION = 999
    elif defect == "missing_api":
        del native.INDEX_API_VERSION
    elif defect == "bool_api":
        native.INDEX_API_VERSION = True
    elif defect == "version":
        native.__version__ = 1
    elif defect == "constructor":
        native.MinuteIndex = None
    elif defect == "rows_of":
        native.MinuteIndex.rows_of = None
    else:
        native.MinuteIndex.nbytes = None
    monkeypatch.setattr(accelerator, "import_module", lambda name: native)
    status = accelerator.backend_status("auto")
    assert status["state"] == "incompatible"
    assert status["effective"] is None
    assert status["available"] is False
    assert "接口不兼容" in status["reason"]
    with pytest.raises(DependencyError):
        accelerator.make_minute_index(None, None)


def test_available_status_and_factory_use_agreed_argument_order(monkeypatch):
    native = _fake_native()
    monkeypatch.setattr(accelerator, "import_module", lambda name: native)
    slot = np.array([0, 1], dtype=np.int32)
    starts = {"000001.SZ": (0, 2)}
    result = accelerator.make_minute_index(slot, starts, np.int64(241), backend="auto")
    assert result.args == (slot, starts, 241)
    assert result.args[0] is slot, "适配层不应先复制一次再让 Rust 再复制一次"
    assert result.args[1] is not starts, "传给原生的是映射快照"
    status = accelerator.backend_status("rust")
    assert status == {
        "requested": "rust",
        "effective": "rust",
        "available": True,
        "version": "0.1.0",
        "reason": "Rust 原生分钟索引（API 1）",
        "state": "available",
        "module": "_ptrade_sim_native",
    }
    assert json.loads(json.dumps(status)) == status


@pytest.mark.parametrize(
    "backend,normalized", [("AUTO", "auto"), (" Rust ", "rust"), ("\tPython\n", "python")]
)
def test_backend_case_and_whitespace_are_normalized_like_configuration(
    monkeypatch, backend, normalized
):
    monkeypatch.setattr(accelerator, "import_module", lambda name: _fake_native())
    status = accelerator.backend_status(backend)
    assert status["requested"] == normalized
    assert status["effective"] == ("python" if normalized == "python" else "rust")
    result = accelerator.make_minute_index(
        np.array([0], dtype=np.int32), {"a": (0, 1)}, backend=backend
    )
    assert (result is None) == (normalized == "python")


def test_environment_cannot_override_explicit_backend(monkeypatch):
    monkeypatch.setenv("PTRADE_ACCELERATION_BACKEND", "rust")
    monkeypatch.setenv("PTRADE_BACKEND", "rust")
    monkeypatch.setattr(accelerator, "import_module", _missing_module)
    assert accelerator.make_minute_index(None, None) is None
    assert accelerator.backend_status("python")["effective"] == "python"


@pytest.mark.parametrize("slot_count", [0, -1, 1.5, "241", 2**100])
def test_invalid_axis_metadata_is_a_data_error(native_factory, slot_count):
    with pytest.raises(DataError):
        native_factory(np.array([0], dtype=np.int32), {"a": (0, 1)}, slot_count)


@pytest.mark.parametrize(
    "slot,starts",
    [
        (np.array([0, 1], dtype=np.int64), {"a": (0, 2)}),
        (np.array([[0, 1]], dtype=np.int32), {"a": (0, 2)}),
        (np.array([0, 1], dtype=">i4"), {"a": (0, 2)}),
        (np.array([0, 1], dtype=np.int32), {"a": (-1, 1)}),
        (np.array([0, 1], dtype=np.int32), {"a": (0, -1)}),
        (np.array([0, 1], dtype=np.int32), {"a": (2, 1)}),
        (np.array([0, 1], dtype=np.int32), {"a": (0, 3)}),
        (np.array([2, 1], dtype=np.int32), {"a": (0, 2)}),
    ],
)
def test_bad_input_never_creates_an_index_with_plausible_wrong_rows(native_factory, slot, starts):
    with pytest.raises(DataError) as caught:
        native_factory(slot, starts)
    assert exit_code_for(caught.value) == 3


def test_nonarray_input_remains_a_programming_type_error(native_factory):
    with pytest.raises(TypeError):
        native_factory([0, 1], {"a": (0, 2)})


def test_exact_rows_and_bounds_match_numpy_for_every_day_boundary(native_factory):
    slot = np.array([0, 2, 2, 7, 240, 1, 1, 3], dtype=np.int32)
    starts = {"a": (0, 5), "b": (5, 8), "empty": (4, 4), "overlap": (2, 5)}
    index = native_factory(slot, starts, 241)
    # Mutation sentinel: lower_bound -> binary_search/right_bound changes these.
    assert index.row_of("a", 2) == 1, "重复槽选成后续行会改变成交价"
    assert index.rows_upto("a", 2, False) == (0, 1), "不含当前槽不得泄漏重复当前 bar"
    assert index.rows_upto("a", 2, True) == (0, 3), "含当前槽必须包括全部重复 bar"
    codes = ["b", "unknown", "a", "empty", "overlap", "b"]
    for query in range(-2, 244):
        expected = [_reference_row(slot, starts, code, query) for code in codes]
        assert index.rows_of(codes, query) == expected
        for code in codes:
            assert index.row_of(code, query) == _reference_row(slot, starts, code, query)
            for include in (False, True):
                assert index.rows_upto(code, query, include) == _reference_bounds(
                    slot, starts, code, query, include
                )
    assert index.rows_of([], 0) == []
    assert index.rows_of(tuple(codes), 1) == [-1 if code != "b" else 5 for code in codes]


def test_int32_extrema_numpy_scalars_and_unbounded_python_integers_match_numpy(native_factory):
    low, high = np.iinfo(np.int32).min, np.iinfo(np.int32).max
    slot = np.array([low, -1, 0, 0, high], dtype=np.int32)
    starts = {"a": (0, 5)}
    # Axis metadata must not clamp the data to [0, slot_count).
    index = native_factory(slot, starts, 1)
    queries = [
        -(10**100),
        np.int64(-(2**63)),
        low - 1,
        np.int32(low),
        -1,
        np.int64(0),
        np.int32(high),
        high + 1,
        np.int64(2**63 - 1),
        10**100,
    ]
    for query in queries:
        assert index.row_of("a", query) == _reference_row(slot, starts, "a", query)
        assert index.rows_of(["a", "unknown"], query) == [
            _reference_row(slot, starts, "a", query),
            -1,
        ]
        for include in (False, True):
            assert index.rows_upto("a", query, include) == _reference_bounds(
                slot, starts, "a", query, include
            )


def test_random_multi_code_gaps_duplicates_and_empty_ranges_match_numpy(native_factory):
    rng = np.random.default_rng(20190512)
    blocks = []
    starts = {}
    cursor = 0
    for i in range(64):
        block = np.sort(rng.integers(-12, 264, size=i * 3, dtype=np.int32))
        blocks.append(block)
        starts[f"{i:06}.SZ"] = cursor, cursor + len(block)
        cursor += len(block)
    slot = np.concatenate(blocks)
    index = native_factory(slot, starts, 241)
    codes = [*reversed(starts), "unknown", next(iter(starts))]
    for query in [*range(-14, 266), -(10**30), 10**30]:
        assert index.rows_of(codes, query) == [
            _reference_row(slot, starts, code, query) for code in codes
        ]
        for code in codes:
            for include in (False, True):
                assert index.rows_upto(code, query, include) == _reference_bounds(
                    slot, starts, code, query, include
                )


def test_strided_readonly_arrays_are_copied_in_logical_order(native_factory):
    slot = np.array([0, 999, 2, 999, 2, 999, 240, 999], dtype=np.int32)[::2]
    slot.flags.writeable = False
    starts = {"a": (0, 4)}
    index = native_factory(slot, starts)
    assert not slot.flags.c_contiguous
    assert index.row_of("a", 2) == 1
    assert index.rows_upto("a", 2, True) == (0, 3)
    assert index.row_of("a", 240) == 3


def test_input_mutation_and_collection_cannot_invalidate_native_snapshot(native_factory):
    slot = np.array([0, 2, 2, 240], dtype=np.int32)
    starts = {"a": (0, 4)}
    reference = weakref.ref(slot)
    index = native_factory(slot, starts)
    slot[:] = 99
    starts.clear()
    del slot
    gc.collect()
    assert reference() is None, "原生不得保留 Python ndarray 引用导致重复预算/长区间驻留"
    assert index.row_of("a", 2) == 1
    assert index.rows_upto("a", 2, True) == (0, 3)
    assert index.row_of("a", 99) == -1


def test_generic_mapping_and_unicode_codes_keep_exact_key_semantics(native_factory):
    class Ranges(Mapping):
        def __getitem__(self, code):
            return {"中文": (0, 2), "empty": (2, 2)}[code]

        def __iter__(self):
            return iter(("中文", "empty"))

        def __len__(self):
            return 2

    index = native_factory(np.array([0, 1], dtype=np.int32), Ranges())
    assert index.rows_of(["中文", "中文", "empty", "unknown"], 1) == [1, 1, -1, -1]
    assert index.rows_upto("empty", 1, True) == (2, 2)


def test_nbytes_counts_owned_slot_and_utf8_allocations_and_is_readonly(native_factory):
    small = native_factory(np.zeros(1, dtype=np.int32), {"a": (0, 1)})
    large = native_factory(np.zeros(101, dtype=np.int32), {"a": (0, 101)})
    unicode = native_factory(np.zeros(1, dtype=np.int32), {"中文": (0, 1)})
    ascii_code = native_factory(np.zeros(1, dtype=np.int32), {"ab": (0, 1)})
    assert type(small.nbytes) is int
    assert small.nbytes > 4
    assert large.nbytes - small.nbytes == 400, "额外100个 int32 自有槽必须计入预算"
    assert unicode.nbytes - ascii_code.nbytes == 4, "代码内存按 UTF-8 字节而非字符数计"
    assert estimate_bytes(large) == large.nbytes
    with pytest.raises(AttributeError):
        small.nbytes = 0
    before = large.nbytes
    for query in range(-1, 242):
        large.row_of("a", query)
        large.rows_upto("a", query, True)
        large.rows_of(["a", "missing"], query)
    assert large.nbytes == before, "查询不得累积按槽缓存"


def test_zero_rows_and_no_ranges_are_a_valid_empty_snapshot(native_factory):
    index = native_factory(np.empty(0, dtype=np.int32), {}, None)
    assert index.row_of("unknown", 0) == -1
    assert index.rows_upto("unknown", 0, True) == (0, 0)
    assert index.rows_of(["unknown"], 0) == [-1]
    assert index.nbytes > 0


def test_market_sized_batch_and_long_running_lru_accounting_stay_bounded(native_factory):
    # >500 codes and 420 successive synthetic days expose retained index memory,
    # without a real quote database or brittle timing/RSS assertions.
    code_count, minute_count = 600, 241
    slot = np.tile(np.arange(minute_count, dtype=np.int32), code_count)
    starts = {f"{i:06}.SZ": (i * minute_count, (i + 1) * minute_count) for i in range(code_count)}
    first = native_factory(slot, starts, minute_count)
    codes = [*reversed(starts), "missing", "000000.SZ"]
    assert first.rows_of(codes, 240) == [
        *[i * minute_count + 240 for i in reversed(range(code_count))],
        -1,
        240,
    ]
    cache = Cache("native-days", memory_budget=first.nbytes * 2)
    for day in range(420):
        cache.put(day, native_factory(slot, starts, minute_count))
    assert len(cache) == 2
    assert cache.stats.evictions == 418
    assert cache.stats.bytes == first.nbytes * 2
    assert cache.stats.peak_bytes == first.nbytes * 2
    cache.clear()
    assert cache.stats.bytes == 0
    assert len(cache) == 0


def test_concurrent_batches_share_only_immutable_index_state(native_factory):
    slot = np.array([0, 2, 2, 7, 240], dtype=np.int32)
    index = native_factory(slot, {"a": (0, 5)}, 241)
    codes = ["a", "missing", "a"] * 128
    before = index.nbytes

    def query(_):
        for _ in range(40):
            assert index.rows_of(codes, 2) == [1, -1, 1] * 128
        return index.rows_upto("a", 2, True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(query, range(12))) == [(0, 3)] * 12
    assert index.nbytes == before
