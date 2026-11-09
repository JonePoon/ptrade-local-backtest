"""可复现 Rust/Python 索引及完整引擎对照；默认只生成临时合成行情库。

输出每次原始耗时、median、内容 digest、内存与工作负载，不承诺固定倍数。
实际库测试需同时指定 --db-path/--strategy/--start/--end；数据库只读。
"""

# ruff: noqa: E402  # 支持未 editable 安装的源码检出；先加入 src 再导入本地包。
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import numpy as np
from loguru import logger

from ptrade_sim import config as cfgmod
from ptrade_sim.accelerator import backend_status
from ptrade_sim.conventions import DAY_SLOTS
from ptrade_sim.data_contract import contract_of
from ptrade_sim.exceptions import ConfigError, DependencyError, PtradeSimError, exit_code_for
from ptrade_sim.runtime import BacktestEngine, DayMinuteData


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timed(fn, repeats: int) -> dict:
    checksum = fn()  # 两个后端都先预热，计时不包含测试用 query 列表构造。
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        observed = fn()
        samples.append(time.perf_counter() - t0)
        if observed != checksum:
            raise RuntimeError("same backend returned different index results")
    return {"seconds": samples, "median_seconds": statistics.median(samples), "checksum": checksum}


def _index_benchmark(codes_count: int, queries_count: int, repeats: int) -> dict:
    codes = [f"{i + 1:06d}.SZ" for i in range(codes_count)]
    # 刻意包含缺槽与重复槽，不用只有完整 241 根的理想化数据证明等价。
    axis = np.array(sorted([i for i in range(241) if i % 17 != 8] + [1]), dtype=np.int32)
    slots = np.tile(axis, codes_count)
    starts = {c: (i * len(axis), (i + 1) * len(axis)) for i, c in enumerate(codes)}
    values = np.arange(len(slots), dtype=np.float64) / 10000.0 + 10.0
    arrays = [values.copy() for _ in range(6)]
    datasets, build_seconds = {}, {}
    for backend in ("python", "rust"):
        t0 = time.perf_counter()
        datasets[backend] = DayMinuteData(slots, *arrays, starts, backend=backend)
        build_seconds[backend] = time.perf_counter() - t0
    queries = [
        ("missing" if i % 97 == 0 else codes[i % codes_count], (i * 17) % 243 - 1)
        for i in range(queries_count)
    ]
    workloads = {}
    for backend, md in datasets.items():

        def scalar(md=md):
            return sum(md.row_of(code, slot) for code, slot in queries)

        def ranges(md=md):
            return sum(
                md.rows_upto(code, slot, bool(i % 2)).stop for i, (code, slot) in enumerate(queries)
            )

        def batch(md=md):
            return sum(sum(md.rows_of(codes, slot)) for slot in range(241))

        workloads[backend] = {
            "scalar": _timed(scalar, repeats),
            "ranges": _timed(ranges, repeats),
            "batch": _timed(batch, repeats),
            "build_seconds": build_seconds[backend],
            "nbytes": md.nbytes,
        }
    speedup = {}
    for name in ("scalar", "ranges", "batch"):
        a, b = workloads["python"][name], workloads["rust"][name]
        if a["checksum"] != b["checksum"]:
            raise RuntimeError(f"Python/Rust index mismatch: {name}")
        speedup[name] = a["median_seconds"] / b["median_seconds"]
    return {
        "codes": codes_count,
        "rows": len(slots),
        "queries": queries_count,
        "batch_queries": codes_count * 241,
        "repeats": repeats,
        "equivalent": True,
        "workloads": workloads,
        "speedup": speedup,
    }


def _business_days(n: int) -> list[str]:
    days, current = [], date(2024, 1, 2)
    while len(days) < n:
        if current.weekday() < 5:
            days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


def _select_for(table: str, mapping: dict) -> str:
    contract = contract_of(table)
    if contract is None:
        raise RuntimeError(f"missing contract {table}")
    return ", ".join(f'{mapping[c]} AS "{c}"' for c in contract.columns)


def _synthetic_database(path: Path, codes_count: int, days_count: int, threads: int) -> list[str]:
    """生成契约内的五张必需物理表；不连接也不改已有行情库。"""
    days = _business_days(days_count)
    con = duckdb.connect(str(path))
    try:
        con.execute(f"SET threads={threads}")
        con.execute("CREATE TABLE bench_days (date VARCHAR, ordinal INTEGER, iso VARCHAR)")
        con.executemany(
            "INSERT INTO bench_days VALUES (?,?,?)",
            [(d, i, f"{d[:4]}-{d[4:6]}-{d[6:]}") for i, d in enumerate(days)],
        )
        con.execute("CREATE TABLE bench_codes (code VARCHAR, name VARCHAR, ordinal INTEGER)")
        con.executemany(
            "INSERT INTO bench_codes VALUES (?,?,?)",
            [(f"{i + 1:06d}.SZ", f"合成股票{i + 1}", i) for i in range(codes_count)],
        )
        con.execute("CREATE TABLE bench_slots (slot INTEGER, hm VARCHAR)")
        con.executemany("INSERT INTO bench_slots VALUES (?,?)", list(enumerate(DAY_SLOTS)))
        con.execute("CREATE TABLE ashare_calendar AS SELECT date FROM bench_days")
        basic = contract_of("ashare_stock_basic")
        basic_map = dict.fromkeys(basic.columns, "CAST(NULL AS VARCHAR)")
        basic_map.update(
            {
                "code": "code",
                "symbol": "code",
                "name": "name",
                "market": "'主板'",
                "list_date": "'19910403'",
                "list_status": "'L'",
                "industry": "'合成'",
            }
        )
        con.execute(
            "CREATE TABLE ashare_stock_basic AS SELECT "
            + _select_for("ashare_stock_basic", basic_map)
            + " FROM bench_codes"
        )
        base = "(10.0 + b.ordinal * 0.01)"
        close = f"round({base} * (1 + (d.ordinal + 1) * 0.001), 2)"
        previous = f"round({base} * (1 + d.ordinal * 0.001), 2)"
        daily = {
            "code": "b.code",
            "date": "d.date",
            "open": previous,
            "high": f"{close} + 0.02",
            "low": f"{previous} - 0.02",
            "close": close,
            "volume": "1000000.0",
            "money": f"1000000.0 * ({previous} + {close}) / 2",
            "preclose": previous,
            "adj_factor": "1.0",
            "vwap": f"({previous} + {close}) / 2",
            "name": "b.name",
            "is_st": "0",
            "is_delisted": "0",
            "change": f"{close} - {previous}",
            "pct_chg": f"100 * ({close} / {previous} - 1)",
        }
        con.execute(
            "CREATE TABLE ashare_1d_stock AS SELECT "
            + _select_for("ashare_1d_stock", daily)
            + " FROM bench_days d CROSS JOIN bench_codes b ORDER BY d.date,b.code"
        )
        idx = {k: f's."{k}"' for k in contract_of("ashare_1d_index").columns}
        idx["code"] = "'000300.SS'"
        con.execute(
            "CREATE TABLE ashare_1d_index AS SELECT "
            + _select_for("ashare_1d_index", idx)
            + " FROM ashare_1d_stock s WHERE s.code='000001.SZ'"
        )
        price = "round(s.open + (s.close - s.open) * t.slot / 240.0, 2)"
        minute = {
            "code": "s.code",
            "date": "s.date",
            "trade_time": "d.iso || ' ' || t.hm || ':00'",
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": "1000.0",
            "money": f"1000.0 * {price}",
            "preclose": "s.preclose",
            "change": f"{price} - s.preclose",
            "pct_chg": f"100 * ({price} / s.preclose - 1)",
        }
        con.execute(
            "CREATE TABLE ashare_1m_stock AS SELECT "
            + _select_for("ashare_1m_stock", minute)
            + " FROM ashare_1d_stock s JOIN bench_days d ON s.date=d.date CROSS JOIN bench_slots t ORDER BY s.date,s.code,t.slot"
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return days


SYNTHETIC_STRATEGY = """
def initialize(context):
    g.pool = []
    g.day = 0
    g.checksum = 0.0
    g.bar_count = 0
    run_daily(context, rebalance, time='09:31')

def before_trading_start(context, data):
    # 股票池必须在回测时钟已设到交易日之后确定，避免初始化空时钟产生空池。
    g.pool = get_Ashares()
    set_universe(g.pool)

def rebalance(context):
    g.day += 1
    for code in g.pool[:5]:
        order_target(code, 100 if g.day % 2 else 0)

def handle_data(context, data):
    g.bar_count += len(data)
    g.checksum += sum(bar.close for bar in data.values())
    if context.blotter.current_dt.strftime('%H:%M') == '09:45':
        frame = get_history(5, '1m', field=['close'], security_list=g.pool[:3], fq=None, include=False)
        g.checksum += float(frame['close'].sum())
"""


def _engine_once(config: dict, strategy: Path, output: Path, backend: str) -> tuple[float, dict]:
    cfg = {**config, "acceleration": {"backend": backend}}
    t0 = time.perf_counter()
    engine = BacktestEngine(cfg, str(strategy), output)
    try:
        daily = engine.run()
        trades = engine.trades_frame()
        duration = time.perf_counter() - t0
        if engine._failed_funcs or engine.feed.src.data_errors():
            raise RuntimeError("benchmark strategy/data failure; timing is invalid")
        bar_count = getattr(engine.g, "bar_count", None)
        if bar_count is not None and (bar_count <= 0 or trades.height <= 0):
            raise RuntimeError(
                "synthetic benchmark performed no active bar/trade work; timing is invalid"
            )
        if "order_id" in trades.columns:
            trades = trades.drop("order_id")  # UUID 无关成交语义，不以随机标识误判差异。
        facts = {
            "daily_sha256": _digest(daily.write_csv()),
            "trades_sha256": _digest(trades.write_csv()),
            "trade_count": trades.height,
            "final_value": engine.portfolio.total_value,
            "bar_checksum": getattr(engine.g, "checksum", None),
            "bar_count": bar_count,
        }
        return duration, facts
    finally:
        engine.feed.src.close()
        # 只释放此引擎拥有的日志句柄；Windows 临时目录不能删除仍打开的日志文件。
        with contextlib.suppress(ValueError):
            logger.remove(engine._log_sink_id)


def _engine_benchmark(args, work: Path) -> dict:
    if args.db_path:
        if not all((args.strategy, args.start, args.end)):
            raise ConfigError("--db-path requires --strategy, --start and --end")
        bundle = cfgmod.resolve_strategy(args.strategy)
        cfg = cfgmod.load(args.config, use_env=False, strategy_config=bundle.config)
        strategy = bundle.py
        cfg.update(
            {
                "db_path": str(Path(args.db_path).resolve()),
                "start_date": args.start,
                "end_date": args.end,
                "frequency": "minute",
            }
        )
        dataset = "existing_read_only_database"
    else:
        db_path = work / "benchmark.duckdb"
        days = _synthetic_database(db_path, args.engine_codes, args.engine_days, args.threads)
        strategy = work / "strategy.py"
        strategy.write_text(SYNTHETIC_STRATEGY, encoding="utf-8")
        cfg = {
            "db_path": str(db_path),
            "start_date": f"{days[0][:4]}-{days[0][4:6]}-{days[0][6:]}",
            "end_date": f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:]}",
            "frequency": "minute",
            "capital_base": 1000000,
            "benchmark": "000300.SS",
        }
        dataset = "synthetic_contract_database"
    cfg["preload"] = {"mode": "rolling", "threads": args.threads, "rolling_window_days": 3}
    if not args.db_path:
        cfg["cache"] = {"minute_memory_budget": "16MB", "duckdb_memory_limit": "256MB"}
    # 真实库对照保留用户缓存预算；不能用合成微型库的 16MB 迫使每分钟重新读大库。
    samples = {"python": [], "rust": []}
    reference = None
    for repeat in range(args.repeats):
        # 交替先后顺序，降低 OS 页缓存和机器同时负载对后端比较的系统性偏差。
        order = ("python", "rust") if repeat % 2 == 0 else ("rust", "python")
        for backend in order:
            duration, facts = _engine_once(cfg, strategy, work / f"{backend}_{repeat}", backend)
            samples[backend].append(duration)
            if reference is None:
                reference = facts
            elif facts != reference:
                raise RuntimeError(f"backtest behavior mismatch: {backend}, repeat={repeat}")
    medians = {k: statistics.median(v) for k, v in samples.items()}
    return {
        "dataset": dataset,
        "codes": args.engine_codes if not args.db_path else None,
        "days": args.engine_days if not args.db_path else None,
        "repeats": args.repeats,
        "seconds": samples,
        "median_seconds": medians,
        "speedup": medians["python"] / medians["rust"],
        "equivalent": True,
        "result": reference,
        "timing_scope": "engine construction + data reads + strategy run + progress/CSV/export snapshots; excludes DB creation and final pipeline metrics",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--engine-codes", type=int, default=200)
    parser.add_argument("--engine-days", type=int, default=20)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--skip-engine", action="store_true")
    parser.add_argument("--db-path")
    parser.add_argument("--strategy")
    parser.add_argument("--config")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if (
        min(
            args.codes,
            args.queries,
            args.repeats,
            args.engine_codes,
            args.engine_days,
            args.threads,
        )
        < 1
    ):
        parser.error("all workload sizes must be positive")
    try:
        status = backend_status("rust")
        if not status["available"]:
            raise DependencyError(str(status["reason"]))
        report = {
            "environment": {
                "platform": platform.platform(),
                "python": sys.version,
                "numpy": np.__version__,
                "duckdb": duckdb.__version__,
                "native": status,
                "threads": args.threads,
            }
        }
        logger.disable("ptrade_sim")  # 两个后端统一去掉日志输出，保留进度/CSV IO。
        report["index"] = _index_benchmark(args.codes, args.queries, args.repeats)
        if not args.skip_engine:
            with tempfile.TemporaryDirectory(prefix="ptrade-rust-bench-") as work:
                report["engine"] = _engine_benchmark(args, Path(work))
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0
    except PtradeSimError as exc:
        print(str(exc), file=sys.stderr)
        return exit_code_for(exc)
    finally:
        logger.enable("ptrade_sim")


if __name__ == "__main__":
    sys.exit(main())
