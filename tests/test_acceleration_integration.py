"""Rust 接入必须维持撮合、T+1、数据缺槽与内存预算，而非仅能导入。"""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest
from polars.testing import assert_frame_equal

from ptrade_sim import cli, config, pipeline
from ptrade_sim.accelerator import backend_status
from ptrade_sim.cache import CacheConfig, estimate_bytes
from ptrade_sim.conventions import DAY_SLOTS
from ptrade_sim.runtime import DataFeed, DayMinuteData


def test_acceleration_environment_and_cli_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("PT_SIM_ACCELERATOR", "rust")
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"acceleration": {"backend": "python"}}), encoding="utf-8")
    cfg = config.load(cfg_file)
    assert cfg["acceleration"]["backend"] == "rust"
    args = cli.parse_args(["backtest", "--accelerator", "python"])
    assert pipeline._resolve(args, cfg)["acceleration"]["backend"] == "python"


@pytest.mark.parametrize("value", [{"backend": "cuda"}, "rust", None, {"backend": None}])
def test_invalid_acceleration_is_a_configuration_error(value):
    cfg = {
        "start_date": "2025-01-02",
        "end_date": "2025-01-08",
        "db_path": "x",
        "strategy": "s.py",
        "acceleration": value,
    }
    assert any("acceleration" in item for item in config.validate(cfg))


def _day(backend):
    slots = np.array([0, 1, 1, 3, 0, 2, 240], dtype=np.int32)
    arrays = [np.arange(7, dtype=np.float64) + i for i in range(6)]
    return DayMinuteData(slots, *arrays, {"a": (0, 4), "b": (4, 7)}, backend=backend)


def test_python_reference_batch_and_missing_slots():
    md = _day("python")
    assert md.rows_of(["a", "b", "missing", "a"], 1) == [1, -1, -1, 1]
    assert md.row_of("a", 2) == -1
    assert md.rows_upto("a", 1, False) == slice(0, 1)
    assert md.rows_upto("a", 1, True) == slice(0, 3)
    assert md.rows_upto("missing", 240, True) == slice(0, 0)
    assert md.backend == "python"
    assert estimate_bytes(md) == md.nbytes


def _require_rust():
    if not backend_status("auto")["available"]:
        pytest.skip("optional Rust extension is not installed")


def test_native_index_memory_is_visible_to_lru():
    _require_rust()
    reference, native = _day("python"), _day("rust")
    assert native.backend == "rust"
    assert native.nbytes - reference.nbytes == native._index.nbytes + sys.getsizeof(native._index)
    assert native._index.nbytes > native.slot.nbytes  # 除 slot 副本还必须计入 code/range 索引。
    assert estimate_bytes(native) == native.nbytes
    for slot in [-2, 0, 1, 2, 3, 240, 241, 2**31 - 1]:
        assert native.rows_of(["a", "b", "missing", "a"], slot) == reference.rows_of(
            ["a", "b", "missing", "a"], slot
        )
        for code in ["a", "b", "missing"]:
            for include in [False, True]:
                assert native.rows_upto(code, slot, include) == reference.rows_upto(
                    code, slot, include
                )


STRATEGY = """
def initialize(context):
    set_universe(['000001.SZ', '600000.SS', '000002.SZ'])
    g.day = 0
    g.bars = 0
    g.blocked = 0
    g.history_total = 0.0
    run_daily(context, rebalance, time='09:31')

def rebalance(context):
    g.day += 1
    if g.day % 2:
        order_target('000001.SZ', 100)
        order_target('600000.SS', 200)
        if order_target('000001.SZ', 0) is None:
            g.blocked += 1
    else:
        order_target('000001.SZ', 0)
        order_target('600000.SS', 0)

def handle_data(context, data):
    g.bars += 1
    if get_frequency() == 'minute' and context.blotter.current_dt.strftime('%H:%M') == '09:45':
        frame = get_history(5, '1m', field=['close'], security_list=['000001.SZ', '600000.SS'], fq=None, include=False)
        g.history_total += float(frame['close'].sum())
"""


@pytest.mark.integration
@pytest.mark.parametrize("frequency", ["minute", "daily"])
@pytest.mark.parametrize("preload_mode", ["rolling", "all"])
def test_python_and_rust_backtests_have_identical_trades_and_equity(
    engine_factory, frequency, preload_mode
):
    _require_rust()
    results = []
    for backend in ["python", "rust"]:
        engine = engine_factory(
            STRATEGY,
            frequency=frequency,
            acceleration={"backend": backend},
            preload={"mode": preload_mode, "threads": 2, "rolling_window_days": 3},
        )
        try:
            daily = engine.run()
            trades = engine.trades_frame().drop("order_id")
            assert trades.height == 10, "不能拿两个没有成交的回测证明一致性"
            assert engine.g.blocked == 3, "当天买入不能由 Rust 接入变成当天可卖"
            assert not engine._failed_funcs
            assert engine.g.bars == (len(DAY_SLOTS) * 5 if frequency == "minute" else 5)
            assert engine.feed.acceleration["effective"] == backend
            results.append((daily, trades, engine.g.history_total))
        finally:
            engine.feed.src.close()
    assert_frame_equal(results[0][0], results[1][0], check_exact=True)
    assert_frame_equal(results[0][1], results[1][1], check_exact=True)
    assert results[0][2] == results[1][2]


@pytest.mark.integration
def test_native_minute_cache_evicts_with_real_native_memory(tiny_db):
    _require_rust()
    feed = DataFeed(
        str(tiny_db),
        "20250102",
        "20250108",
        threads=2,
        cache_config=CacheConfig(minute_memory_budget="8MB"),
        acceleration_backend="rust",
    )
    try:
        first = feed.minute_day("20250102")
        assert first is not None
        feed.cache.minute._budget = first.nbytes * 2 - 1
        feed.minute_day("20250103")
        feed.minute_day("20250106")
        assert feed.cache.minute.stats.puts == 3
        assert feed.cache.minute.stats.evictions == 2
        assert feed.cache.minute.stats.bytes <= feed.cache.minute._budget
    finally:
        feed.src.close()
