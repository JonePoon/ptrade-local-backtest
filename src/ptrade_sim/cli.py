"""ptrade-local-backtest 命令行入口。

用法::

    ptrade-local-backtest backtest  [--config ptrade_config.json] [--strategy examples/x.py]
                         [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--capital 100000]
                         [--db-path data/quant.duckdb] [--frequency minute|daily]
                         [--output-dir DIR] [--port 8765]
    ptrade-local-backtest dashboard [--port 8765] [--root ./backtest_results] [--host 127.0.0.1]
    ptrade-local-backtest queue     [--output-dir DIR] [--json]      # 查看运行/排队状态
    ptrade-local-backtest db build  --db data/quant.duckdb --start-year 2019 --end-year 2025
    ptrade-local-backtest db verify --db data/quant.duckdb --data-dir data/
                         [--start-year 2019] [--end-year 2025] [--dates D1,D2] [--json]
    ptrade-local-backtest env       [--json]                         # 打印配置与资源视图
    ptrade-local-backtest --version                                  # 打印版本号

``--json`` 约定：**只有 stdout 上的一份纯 JSON**（日志/进度都不混入），供脚本消费。

配置分层（优先级低→高）：config.example.json ← ptrade_config.json（机器级）← strategy_config.json（策略级）← 环境变量 PT_SIM_* ← CLI 参数
"""

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

from loguru import logger

from ptrade_sim import DISTRIBUTION_NAME, PROJECT_NAME, __version__, resources
from ptrade_sim import config as cfgmod
from ptrade_sim.accelerator import backend_status
from ptrade_sim.config import DEFAULT_PORT, DEFAULT_RESULTS_DIR
from ptrade_sim.exceptions import PtradeSimError, exit_code_for
from ptrade_sim.pipeline import run_backtest

# ============================================================
# 常量
# ============================================================

#: ``db build`` / ``db verify`` 的默认年范围（同一份取值，避免两处漂移）。
#: 库里数据晚于 ``DEFAULT_END_YEAR`` 时，必须显式 ``--end-year`` 扩大，
#: 否则 ``db verify`` 的「日期范围」段看不到那些年份的数据。
DEFAULT_START_YEAR = 2019
DEFAULT_END_YEAR = 2025

#: ``db verify`` 等价性抽样用的「每年已知交易日」。
#: 表里没有的年份回退 ``<year>0603``（6 月初，避开长假与年末调仓）。
_VERIFY_SAMPLE_DATES: dict[int, str] = {
    2019: "20190102",
    2020: "20200603",
    2021: "20210603",
    2022: "20220603",
    2023: "20230601",
    2024: "20240603",
    2025: "20250603",
}

# ============================================================
# 参数
# ============================================================


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog=DISTRIBUTION_NAME,
        description=f"{PROJECT_NAME}（本地分钟级撮合）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{DISTRIBUTION_NAME} {__version__}",
        help="打印版本号后退出",
    )
    sub = parser.add_subparsers(dest="command")

    bt = sub.add_parser("backtest", help="运行回测")
    bt.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认按分层自动查找：模板 ← 机器级 ← 策略级 ← 环境变量）",
    )
    bt.add_argument("--strategy", default=None, help="策略文件路径（覆盖配置）")
    bt.add_argument("--start", default=None, help="回测开始日期 YYYY-MM-DD")
    bt.add_argument("--end", default=None, help="回测结束日期 YYYY-MM-DD")
    bt.add_argument("--capital", type=float, default=None, help="初始资金")
    bt.add_argument("--db-path", default=None, help="DuckDB 库路径（默认 data/quant.duckdb）")
    bt.add_argument(
        "--frequency",
        default=None,
        choices=["minute", "daily"],
        help="回测周期：minute（默认，241 槽/日）| daily（每日 15:00 一次）",
    )
    bt.add_argument("--output-dir", default=None, help="输出目录（默认 ./backtest_results）")
    bt.add_argument("--port", type=int, default=None, help=f"实时看板端口（默认 {DEFAULT_PORT}）")
    bt.add_argument("--no-dashboard", action="store_true", help="不自动拉起实时看板")
    bt.add_argument("--no-queue", action="store_true", help="跳过资源排队，直接开跑（不推荐）")
    bt.add_argument("--no-wait", action="store_true", help="资源不足时不等待，直接退出")
    bt.add_argument("--threads", type=int, default=None, help="预读线程数（覆盖配置）")
    bt.add_argument(
        "--accelerator",
        choices=["auto", "python", "rust"],
        default=None,
        help="分钟索引后端：auto 自动选择；python 参照路径；rust 必须有扩展",
    )

    export = sub.add_parser("export", help="一键转换为 PTrade 客户端单文件策略")
    export.add_argument("--strategy", required=True, help="本地策略目录或 .py 文件")
    export.add_argument("--config", default=None, help="生效配置文件（仅提取策略 params）")
    export.add_argument(
        "--output", default=None, help="输出 .py 文件（默认 production/<策略名>_ptrade.py）"
    )
    export.add_argument("--params", default=None, help="JSON 对象参数覆盖")
    export.add_argument("--params-file", default=None, help="UTF-8 JSON 参数文件")
    export.add_argument(
        "--target-python",
        choices=["3.5", "3.11"],
        default=None,
        help="PTrade 客户端 Python 版本（默认配置 export.target_python 或 3.11）",
    )
    export.add_argument("--check", action="store_true", help="只检查，不写文件")
    export.add_argument("--overwrite", action="store_true", help="替换已有导出文件，绝不替换源文件")
    export.add_argument("--json", action="store_true", help="stdout 只输出转换报告 JSON")

    data = sub.add_parser("data", help="Rust 行情下载、入库与数据任务")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    for operation in ("status", "plan", "fetch", "ingest", "sync", "verify", "jobs", "cancel"):
        command = data_sub.add_parser(operation)
        command.add_argument("--config", default=None, help="数据配置 JSON（不是回测配置）")
        command.add_argument("--output-dir", default=None, help="任务/回测结果根目录")
        command.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
        if operation in ("plan", "fetch", "ingest", "sync", "verify"):
            command.add_argument("--start", default=None)
            command.add_argument("--end", default=None)
            command.add_argument("--symbols", default=None, help="逗号分隔证券代码")
            command.add_argument("--datasets", default=None, help="逗号分隔数据集")
            command.add_argument("--lake", default=None)
            command.add_argument("--db", default=None)
            command.add_argument("--provider", default=None)
            references = command.add_mutually_exclusive_group()
            references.add_argument(
                "--refresh-reference",
                dest="refresh_reference",
                action="store_true",
                default=None,
                help="显式刷新 SDK 参考数据，不注册计划任务",
            )
            references.add_argument(
                "--no-refresh-reference",
                dest="refresh_reference",
                action="store_false",
                default=None,
                help="本次关闭参考刷新，覆盖配置中的 enabled=true",
            )
            command.add_argument("--idempotency-key", default=None)
        if operation == "cancel":
            command.add_argument("--job-id", required=True)
            command.add_argument(
                "--server", default="http://127.0.0.1:8765", help="拥有任务的本地看板服务"
            )

    dbp = sub.add_parser("dashboard", help="启动本地回测看板（FastAPI + Vue 前端）")
    dbp.add_argument("--port", type=int, default=None, help=f"监听端口（默认 {DEFAULT_PORT}）")
    dbp.add_argument("--root", default=None, help=f"结果根目录（默认 ./{DEFAULT_RESULTS_DIR}）")
    dbp.add_argument("--host", default="127.0.0.1", help="监听地址")

    q = sub.add_parser("queue", help="查看回测队列与资源状态")
    q.add_argument("--output-dir", default=None, help="结果根目录")
    q.add_argument("--json", action="store_true", help="以 JSON 输出")

    d = sub.add_parser("db", help="DuckDB 物理库构建/校验")
    dsub = d.add_subparsers(dest="db_command")
    b = dsub.add_parser("build", help="从 parquet 构建 DuckDB 库")
    b.add_argument("--db", default="data/quant.duckdb")
    b.add_argument("--data-dir", default="data")
    b.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    b.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    b.add_argument("--tables", default=None, help="只建指定表（逗号分隔）")
    b.add_argument("--overwrite", action="store_true", help="已存在的表也重建")
    b.add_argument("--threads", type=int, default=8)
    b.add_argument("--memory", default="8GB")
    b.add_argument("--tmp", default=None, help="溢写目录")
    v = dsub.add_parser("verify", help="契约校验 + 与 parquet 等价性校验")
    v.add_argument("--db", default="data/quant.duckdb")
    v.add_argument("--data-dir", default="data")
    v.add_argument(
        "--start-year",
        type=int,
        default=DEFAULT_START_YEAR,
        help=f"契约校验/日期范围的起始年（默认 {DEFAULT_START_YEAR}）",
    )
    v.add_argument(
        "--end-year",
        type=int,
        default=DEFAULT_END_YEAR,
        help=f"契约校验/日期范围的结束年（默认 {DEFAULT_END_YEAR}；库里有更晚的年份时请扩大）",
    )
    v.add_argument(
        "--dates",
        default=None,
        help=(
            "逗号分隔 YYYYMMDD（默认按 --start-year/--end-year 每年抽样，"
            f"如 {_VERIFY_SAMPLE_DATES[DEFAULT_START_YEAR]},{_VERIFY_SAMPLE_DATES[DEFAULT_END_YEAR]}）"
        ),
    )
    v.add_argument("--contract-only", action="store_true", help="只做契约校验")
    v.add_argument("--list-contract", action="store_true", help="打印表契约")
    v.add_argument("--json", action="store_true", help="以 JSON 输出（stdout 只有纯 JSON）")
    n = dsub.add_parser("normalize", help="统一既有库的代码尾缀（.SH→.SS）并可删表")
    n.add_argument("--db", default="data/quant.duckdb")
    n.add_argument("--dry-run", action="store_true", help="只检查，不改动")
    n.add_argument("--drop", default=None, help="顺带删除的表（逗号分隔）")

    sub.add_parser("env", help="打印配置来源、资源快照与缓存策略").add_argument(
        "--json", action="store_true", help="以 JSON 输出（stdout 只有纯 JSON）"
    )

    return parser.parse_args(argv)


def _usage() -> int:
    print(
        "用法: ptrade-local-backtest <command> [options]\n\n"
        "命令:\n"
        "  backtest   运行回测（自动排队 + 实时看板）\n"
        "  export     一键转换 PTrade 客户端单文件策略\n"
        "  dashboard  启动本地回测看板\n"
        "  queue      查看运行/排队状态与资源视图\n"
        "  db         DuckDB 物理库构建/校验\n"
        "  env        打印配置来源、资源快照、缓存策略\n\n"
        "示例:\n"
        "  ptrade-local-backtest backtest --strategy examples/demo_rotation --start 2025-01-01\n"
        "  ptrade-local-backtest backtest --db-path data/quant.duckdb\n"
        "  ptrade-local-backtest queue\n"
        "  ptrade-local-backtest db verify --db data/quant.duckdb --data-dir data/ --end-year 2026\n"
        "  ptrade-local-backtest export --strategy examples/demo_rotation --output production/rotation.py\n"
        "  ptrade-local-backtest env --json\n"
        "  ptrade-local-backtest --version\n\n"
        "配置分层（低→高）：config.example.json ← ptrade_config.json ← strategy_config.json ← PT_SIM_* ← CLI"
    )
    return 1


def _setup_console_logging() -> None:
    logger.remove()
    logger.add(
        lambda m: print(m, end=""),
        level="INFO",
        format="{time:HH:mm:ss} {level} {message}",
    )


# ============================================================
# JSON 输出辅助
# ============================================================
#
# 约定：``--json`` 时 stdout 上**只能有一份纯 JSON**。子调用（dbtools.verify /
# verify_equivalence / config.load）既会 print 也会经 loguru sink print，
# 故采集阶段一律用 :func:`_quiet_stdout` 静音，JSON 本身在退出该上下文后再打印。
# loguru 的控制台 sink 是 ``lambda m: print(m, end="")``，其 ``sys.stdout`` 在
# 调用时才解析，因此 ``contextlib.redirect_stdout`` 对它同样生效。


@contextlib.contextmanager
def _quiet_stdout():
    """把代码块内写往 stdout 的内容（含 loguru 控制台 sink）收进缓冲区。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def _verify_years(args) -> list[int]:
    """``db verify`` 的年份列表（``--start-year``/``--end-year``，反序时自动纠正）。"""
    lo, hi = args.start_year, args.end_year
    if lo > hi:
        lo, hi = hi, lo
    return list(range(lo, hi + 1))


def _verify_dates(args) -> list[str]:
    """等价性抽样日期：``--dates`` 优先，否则按年范围每年取一个已知交易日。"""
    if args.dates:
        return [d.strip() for d in args.dates.split(",") if d.strip()]
    return [_VERIFY_SAMPLE_DATES.get(y, f"{y}0603") for y in _verify_years(args)]


def _contract_json() -> dict:
    """``db verify --list-contract --json`` 的结构化契约。"""
    from ptrade_sim import data_contract as dc

    return {
        "field_mapping": dc.field_mapping_note(),
        "tables": [
            {
                "name": t.name,
                "required": t.required,
                "partitioned": t.partitioned,
                "key": list(t.key),
                "columns": list(t.columns),
                "description": t.desc,
                "derived": list(t.derived),
                "extension": list(t.extension),
                "unsupported": list(t.unsupported),
            }
            for t in dc.ALL_TABLES
        ],
    }


def _db_inventory(con, years: list[int]) -> list[dict]:
    """逐表盘点：是否存在、行数、列差异、日期范围（限定 ``years`` 窗口）。"""
    from ptrade_sim import data_contract as dc

    have = {
        r[0]
        for r in con.sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    lo, hi = f"{min(years)}0101", f"{max(years)}1231"
    out: list[dict] = []
    for t in dc.ALL_TABLES:
        item: dict = {
            "name": t.name,
            "required": t.required,
            "present": t.name in have,
            "rows": None,
            "columns_missing": [],
            "columns_extra": [],
            "date_range": None,
        }
        if t.name in have:
            try:
                item["rows"] = con.sql(f'SELECT count(*) FROM "{t.name}"').fetchone()[0]
            except Exception:
                item["rows"] = None
            cols = {
                r[0]
                for r in con.sql(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{t.name}'"
                ).fetchall()
            }
            item["columns_missing"] = [c for c in t.columns if c not in cols]
            item["columns_extra"] = sorted(cols - set(t.columns))
            if "date" in t.columns:
                try:
                    r = con.sql(
                        f'SELECT min(date), max(date) FROM "{t.name}" '
                        f"WHERE date >= '{lo}' AND date <= '{hi}'"
                    ).fetchone()
                    if r and r[0]:
                        item["date_range"] = [r[0], r[1]]
                except Exception as exc:
                    item["date_range"] = None
                    item["error"] = str(exc)
        out.append(item)
    return out


def _dump_json(payload: dict) -> None:
    """打印 JSON（stdout 唯一输出）。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ============================================================
# backtest
# ============================================================


# ============================================================
# dashboard / queue / db / env
# ============================================================


def run_data(args) -> int:
    """Use the same persistent manager as the dashboard; native stages own ETL."""
    from ptrade_sim.data_jobs import DataManager
    from ptrade_sim.exceptions import ConfigError, DataError

    if args.data_command == "cancel":
        import urllib.error
        import urllib.parse
        import urllib.request

        parsed = urllib.parse.urlsplit(args.server)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigError("取消只能请求拥有任务的本地看板")
        url = (
            args.server.rstrip("/")
            + "/api/data/jobs/"
            + urllib.parse.quote(args.job_id, safe="")
            + "/cancel"
        )
        request = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.load(response)
        except (urllib.error.URLError, ValueError) as exc:
            raise DataError(f"本地看板取消请求失败：{exc}") from exc
        _dump_json(result)
        return 0
    overrides = {}
    trusted: dict[str, object] = {}
    for key in ("start", "end"):
        if getattr(args, key, None) is not None:
            overrides[key] = getattr(args, key)
    for key in ("symbols", "datasets"):
        value = getattr(args, key, None)
        if value is not None:
            overrides[key] = [item.strip() for item in value.split(",") if item.strip()]
    for key in ("lake", "db"):
        if getattr(args, key, None):
            trusted[key] = str(Path(getattr(args, key)).expanduser().resolve())
    if getattr(args, "provider", None):
        trusted["provider"] = args.provider
    reference_override = getattr(args, "refresh_reference", None)
    if reference_override is not None:
        trusted["reference_refresh"] = {"enabled": reference_override}
    root = Path(args.output_dir or DEFAULT_RESULTS_DIR).resolve()
    with DataManager(root, args.config, config_overrides=trusted) as manager:
        if args.data_command == "status":
            result = manager.status()
            code = 0
        elif args.data_command == "jobs":
            result = {"jobs": manager.list_jobs()}
            code = 0
        elif args.data_command == "plan":
            result = manager.plan(overrides)
            code = 0 if result["ready"] else 6
        else:
            record = manager.start(
                args.data_command, overrides, idempotency_key=args.idempotency_key
            )
            try:
                record = manager.wait_job(record["id"])
            except KeyboardInterrupt:
                manager.cancel_job(record["id"])
                record = manager.wait_job(record["id"])
            result = record
            code = int(
                record.get(
                    "exit_code",
                    0
                    if record["status"] == "succeeded"
                    else (130 if record["status"] == "cancelled" else 3),
                )
            )
    if args.json:
        _dump_json(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


def run_dashboard(args) -> int:
    try:
        from ptrade_sim import server
    except ImportError as exc:
        print(f"看板依赖缺失（{exc}）。请安装：pip install 'ptrade-local-backtest[dashboard]'")
        return 6  # 缺可选依赖（见 exceptions.EXIT_CODES）
    root = Path(args.root) if args.root else (Path.cwd() / DEFAULT_RESULTS_DIR)
    if not root.is_absolute():
        root = Path.cwd() / root
    argv = ["--root", str(root), "--host", args.host, "--port", str(args.port or DEFAULT_PORT)]
    return server.main(argv)


def run_queue(args) -> int:
    from ptrade_sim.queue import BacktestQueue, queue_dir_for

    # 读配置，使「并发上限」等与实际回测一致（否则会显示默认值，误导）
    try:
        cfg = cfgmod.load()
    except Exception:
        cfg = {}
    qopt = cfg.get("queue", {}) or {}
    out_root = Path(args.output_dir or cfg.get("output_dir") or (Path.cwd() / DEFAULT_RESULTS_DIR))
    if not out_root.is_absolute():
        out_root = Path.cwd() / out_root
    q = BacktestQueue(
        queue_dir_for(out_root, override=qopt.get("dir")),
        enabled=True,
        max_parallel=qopt.get("max_parallel"),
        cpu_slots_limit=qopt.get("cpu_slots_limit"),
        poll_interval=qopt.get("poll_interval", 10),
    )
    snap = resources.probe(out_root)
    if args.json:
        print(
            json.dumps(
                {
                    "resources": {
                        "cpu_count": snap.cpu_count,
                        "mem_total": snap.mem_total,
                        "mem_available": snap.mem_available,
                        "mem_used_pct": round(snap.mem_used_pct, 1),
                        "disk_free": snap.disk_free,
                    },
                    "queue": q.snapshot(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print("=" * 72)
    print(f"资源：{snap.describe()}")
    print("=" * 72)
    print(q.describe())
    return 0


def run_db(args) -> int:
    from ptrade_sim import dbtools

    if args.db_command == "build":
        tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
        return dbtools.build(
            data_dir=args.data_dir,
            db_path=args.db,
            start_year=args.start_year,
            end_year=args.end_year,
            tables=tables,
            overwrite=args.overwrite,
            threads=args.threads,
            memory=args.memory,
            tmp_dir=args.tmp,
        )
    if args.db_command == "verify":
        if args.list_contract:
            from ptrade_sim import data_contract as dc

            if args.json:
                _dump_json(_contract_json())
                return 0
            print(dc.describe())
            return 0
        import duckdb

        years = _verify_years(args)
        con = duckdb.connect(args.db, read_only=True)
        if args.json:
            # JSON 模式：子调用的输出全部静音，只在最后吐一份纯 JSON
            with _quiet_stdout() as buf:
                rc = dbtools.verify(con, years)
                tables = _db_inventory(con, years)
            con.close()
            equiv_rc = None
            if not args.contract_only:
                with _quiet_stdout():
                    equiv_rc = dbtools.verify_equivalence(
                        args.db, args.data_dir, _verify_dates(args)
                    )
            issues = [ln.strip() for ln in buf.getvalue().splitlines() if "✗" in ln]
            _dump_json(
                {
                    "db": args.db,
                    "years": years,
                    "contract": {
                        "ok": rc == 0,
                        "returncode": rc,
                        "issues": issues,
                    },
                    "tables": tables,
                    "date_range": {
                        "window": {"start": f"{min(years)}0101", "end": f"{max(years)}1231"},
                        "by_table": {t["name"]: t["date_range"] for t in tables if t["date_range"]},
                    },
                    "equivalence": {
                        "skipped": args.contract_only,
                        "dates": [] if args.contract_only else _verify_dates(args),
                        "returncode": equiv_rc,
                    },
                }
            )
            return rc or (equiv_rc or 0)
        rc = dbtools.verify(con, years)
        con.close()
        if args.contract_only:
            return rc
        return rc or dbtools.verify_equivalence(args.db, args.data_dir, _verify_dates(args))
    if args.db_command == "normalize":
        drops = [t.strip() for t in args.drop.split(",")] if args.drop else None
        return dbtools.normalize_suffix(args.db, dry_run=args.dry_run, drop_tables=drops)
    print("用法: ptrade-local-backtest db {build|verify|normalize} ...")
    return 1


def _export_params(args, initial: dict) -> dict:
    """只接受 JSON 对象；文件参数低于 --params 的显式覆盖。"""
    from ptrade_sim.exceptions import ConfigError, ConfigFileNotFoundError

    values = dict(initial)
    raw_items = []
    if args.params_file:
        path = Path(args.params_file)
        try:
            raw_items.append((str(path), path.read_text(encoding="utf-8-sig")))
        except OSError as exc:
            raise ConfigFileNotFoundError(f"参数文件读取失败：{path}：{exc}") from exc
    if args.params is not None:
        raw_items.append(("--params", args.params))
    for origin, raw in raw_items:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"{origin} 不是有效 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{origin} 必须是 JSON 对象")
        values = cfgmod._deep_merge(values, data)
    return values


def run_export(args) -> int:
    """静态转换 PTrade 单文件；无需行情库、无需执行用户策略。"""
    from ptrade_sim.exceptions import StrategyConfigError, StrategyError
    from ptrade_sim.strategy_export import convert_strategy, write_export

    with _quiet_stdout():
        bundle = cfgmod.resolve_strategy(args.strategy)
        cfg = cfgmod.load(args.config, strategy_config=bundle.config)
        params = cfg.get("params", {})
        if not isinstance(params, dict):
            raise StrategyConfigError("策略 params 必须是 JSON 对象")
        params = _export_params(args, params)
        export_config = cfg.get("export", {})
        if not isinstance(export_config, dict):
            raise StrategyConfigError("export 必须是对象")
        target_python = args.target_python or export_config.get("target_python", "3.11")
        result = convert_strategy(
            bundle.py, params=params, target_python=target_python, params_mode="replace"
        )
        ready = bool(result.report.get("ready"))
        files: dict[str, str | None] = {}
        if not args.check:
            output = Path(args.output) if args.output else Path("production") / result.filename
            try:
                paths = write_export(result, output, overwrite=args.overwrite)
                files = {k: str(v) if v is not None else None for k, v in paths.items()}
            except StrategyError:
                if ready:
                    raise  # 路径/写入等真实错误不能冒充兼容诊断。
                report_output = output / result.filename if output.is_dir() else output
                files = {"source": None, "report": str(report_output.with_suffix(".manifest.json"))}
        payload = {
            "ready": ready,
            "filename": result.filename,
            "report": result.report,
            "files": files,
        }
    if args.json:
        _dump_json(payload)
    else:
        print("转换通过" if ready else "转换阻塞（详见诊断）")
        for d in result.report.get("diagnostics", []):
            location = f"{d.get('path', '')}:{d.get('line') or 0}"
            print(f"  [{d.get('severity', 'info')}] {location} {d.get('message', '')}")
        for kind, path in files.items():
            if path:
                print(f"  {kind}: {path}")
    return 0 if ready else 4


def run_env(args) -> int:
    from ptrade_sim.cache import CacheConfig

    if args.json:
        return _run_env_json()

    cfg = cfgmod.load()
    print("=" * 72)
    print("配置来源（优先级低→高）")
    print("=" * 72)
    print(f"  公开模板 : {cfgmod.default_template_path() or '（未找到 config.example.json）'}")
    print(f"  本地私有 : {cfgmod.find_local_config() or '（未找到 ptrade_config.json）'}")
    print("  环境变量 : PT_SIM_*")
    print()
    print(f"  db_path      = {cfg.get('db_path')}  ← 引擎唯一数据源")
    print(f"  frequency    = {cfg.get('frequency')}")
    status = backend_status((cfg.get("acceleration") or {}).get("backend", "auto"))
    print(f"  acceleration = {status['effective']}（{status['reason']}）")
    print(f"  preload      = {cfg.get('preload')}")
    print(f"  queue        = {cfg.get('queue')}")
    print()
    print("=" * 72)
    # 资源探查落在库文件所在盘（结果与库同盘的场景最常见）
    dbp = cfg.get("db_path")
    probe_target = str(Path(dbp).parent) if dbp else None
    snap = resources.probe(probe_target)
    print(f"资源快照：{snap.describe()}")
    if dbp:
        exists = (
            "存在" if Path(dbp).exists() else "**不存在**（需先 ptrade-local-backtest db build）"
        )
        print(f"数据源：{dbp}  {exists}")
        if Path(dbp).exists():
            try:
                from ptrade_sim.data_source import make_source

                s = make_source(dbp)
                print(
                    f"  日线覆盖 {s.coverage('ashare_1d_stock')} | "
                    f"分钟覆盖 {s.coverage('ashare_1m_stock')} | 表 {len(s.tables())} 张"
                )
                s.close()
            except Exception as exc:
                print(f"  读取失败：{exc}")
    prefer = int((cfg.get("preload") or {}).get("threads", 8))
    print(f"建议线程：{resources.suggest_threads(snap, prefer=prefer)}")
    print()
    print(f"缓存策略：{CacheConfig.from_dict(cfg.get('cache')).describe()}")
    print("=" * 72)
    return 0


def _run_env_json() -> int:
    """``env --json``：与 :func:`run_env` 同源采集，但 stdout 只有一份纯 JSON。"""
    import dataclasses

    from ptrade_sim.cache import CacheConfig

    with _quiet_stdout():
        cfg = cfgmod.load()
        dbp = cfg.get("db_path")
        # 资源探查落在库文件所在盘（与文本模式口径一致）
        probe_target = str(Path(dbp).parent) if dbp else None
        snap = resources.probe(probe_target)
        db_exists = False
        coverage: dict[str, object] = {}
        tables: int | None = None
        coverage_error: str | None = None
        if dbp and Path(dbp).exists():
            db_exists = True
            try:
                from ptrade_sim.data_source import make_source

                s = make_source(dbp)
                for name in ("ashare_1d_stock", "ashare_1m_stock"):
                    coverage[name] = s.coverage(name)
                tables = len(s.tables())
                s.close()
            except Exception as exc:
                coverage_error = str(exc)
        prefer = int((cfg.get("preload") or {}).get("threads", 8))
        threads = resources.suggest_threads(snap, prefer=prefer)
        cache = CacheConfig.from_dict(cfg.get("cache"))
        cache_json = {
            "describe": cache.describe(),
            "minute_memory_budget_bytes": cache.resolve_minute_budget(),
            **dataclasses.asdict(cache),
        }
        payload = {
            "config_sources": {
                "template": str(cfgmod.default_template_path() or "") or None,
                "local": str(cfgmod.find_local_config() or "") or None,
                "env": "PT_SIM_*",
            },
            "db_path": dbp,
            "db_exists": db_exists,
            "config": {
                "db_path": dbp,
                "frequency": cfg.get("frequency"),
                "acceleration": cfg.get("acceleration"),
                "preload": cfg.get("preload"),
                "queue": cfg.get("queue"),
            },
            "coverage": coverage,
            "tables": tables,
            "coverage_error": coverage_error,
            "resources": {
                "cpu_count": snap.cpu_count,
                "mem_total": snap.mem_total,
                "mem_available": snap.mem_available,
                "mem_used_pct": round(snap.mem_used_pct, 1),
                "disk_free": snap.disk_free,
                "load_avg": snap.load_avg,
                "psutil_cpu_pct": snap.psutil_cpu_pct,
                "probe_target": probe_target,
                "describe": snap.describe(),
            },
            "suggest_threads": threads,
            "acceleration": backend_status((cfg.get("acceleration") or {}).get("backend", "auto")),
            "cache": cache_json,
        }
    _dump_json(payload)
    return 0


# ============================================================
# 入口
# ============================================================


def main(argv=None) -> int:
    """CLI 入口。

    **退出码**：0 成功 / 1 未分类 / 2 配置·策略定位 / 3 数据 / 4 策略代码 /
    5 资源·队列 / 6 缺依赖。映射由 :func:`ptrade_sim.exceptions.exit_code_for`
    统一给出，让调用脚本能区分失败类别，而不必去解析错误文本。
    """
    _setup_console_logging()
    args = parse_args(argv)
    try:
        return _dispatch(args)
    except PtradeSimError as exc:
        # 子命令内部通常已就地处理；这里是兜底，保证**任何**漏出的可预期错误
        # 都能拿到分类退出码，而不是冒泡成 traceback（那会一律退出 1）。
        logger.error(f"{type(exc).__name__}: {exc}")
        return exit_code_for(exc)


def _dispatch(args) -> int:
    if args.command == "data":
        return run_data(args)
    if args.command == "backtest":
        return run_backtest(args)
    if args.command == "export":
        return run_export(args)
    if args.command == "dashboard":
        return run_dashboard(args)
    if args.command == "queue":
        return run_queue(args)
    if args.command == "db":
        return run_db(args)
    if args.command == "env":
        return run_env(args)
    return _usage()


if __name__ == "__main__":
    sys.exit(main())
