"""Reproducible native-store benchmark using synthetic Parquet only.

Python creates test inputs and independently checks Rust's output. The ingest
path itself is the standalone Rust executable, not a Python bridge.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, required=True)
parser.add_argument("--codes", type=int, default=600)
parser.add_argument("--days", type=int, default=5)
parser.add_argument("--minute-days", type=int, default=2)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
if min(args.codes, args.days, args.minute_days) < 1:
    parser.error("codes/days/minute-days must be positive")
base = Path(__file__).resolve().parents[1]
root = (args.output or base / "work" / ("benchmark-" + str(time.time_ns()))).resolve()
root.mkdir(parents=True, exist_ok=True)
lake = root / "lake"
db = root / "target.duckdb"
exe = args.exe.resolve()
con = duckdb.connect()
assert duckdb.__version__ == "1.5.6"
days = []
day = date(2025, 1, 2)
while len(days) < args.days:
    if day.weekday() < 5:
        days.append(day)
    day += timedelta(days=1)
con.execute("CREATE TABLE dates(d DATE)")
con.executemany("INSERT INTO dates VALUES (?)", [(d,) for d in days])
con.execute(
    "CREATE TABLE syms AS SELECT lpad(CAST(i AS VARCHAR),6,'0')||'.SZ' symbol FROM range(1,?) t(i)",
    [args.codes + 1],
)
provenance = "'benchmark_fixture' AS source,'v2' AS data_version,TIMESTAMPTZ '2025-03-01 00:00:00+00' AS fetched_at"
queries = {
    "daily_bars": f"SELECT symbol,d AS trade_date,10.0 AS open,10.5 AS high,9.5 AS low,10.0 AS close,10.0 AS pre_close,1000::BIGINT AS volume,10000.0 AS amount,'历史测试名' AS name,0 AS is_st,0 AS is_delisted,{provenance} FROM syms CROSS JOIN dates",
    "adj_factors": f"SELECT symbol,d AS trade_date,'hfq' AS adjust_type,1.0 AS factor,{provenance} FROM syms CROSS JOIN dates",
    "trading_status": f"SELECT symbol,d AS trade_date,TRUE AS is_trading,'normal' AS status,FALSE AS risk_warning,{provenance} FROM syms CROSS JOIN dates",
    "instruments": f"SELECT symbol,'当前测试名' AS name,'SZ' AS exchange,'stock' AS asset_type,DATE '1990-01-01' AS list_date,NULL::DATE AS delist_date,NULL::VARCHAR AS prev_symbol,{provenance} FROM syms",
    "trading_calendar": f"SELECT d AS trade_date,TRUE AS is_trading,{provenance} FROM dates",
    "index_bars": f"SELECT '000300.SH' AS symbol,d AS trade_date,'1d' AS frequency,3000.0 AS open,3001.0 AS high,2999.0 AS low,3000.0 AS close,3000.0 AS pre_close,1000::BIGINT AS volume,3000000.0 AS amount,{provenance} FROM dates",
    "index_constituents": f"SELECT '000300.SH' AS index_symbol,symbol,(SELECT min(d) FROM dates) AS as_of_date,100.0/{args.codes} AS weight,{provenance} FROM syms",
}
minute_days = days[-min(args.minute_days, args.days) :]
quoted = ",".join("DATE '" + d.isoformat() + "'" for d in minute_days)
queries["minute_bars"] = (
    f"SELECT symbol,d AS trade_date,CAST(d AS TIMESTAMP)+CASE WHEN i<120 THEN INTERVAL '9 hours 31 minutes'+i*INTERVAL '1 minute' ELSE INTERVAL '13 hours 1 minute'+(i-120)*INTERVAL '1 minute' END AS bar_time,'1m' AS frequency,10.0 AS open,10.0 AS high,10.0 AS low,10.0 AS close,100::BIGINT AS volume,1000.0 AS amount,{provenance} FROM syms CROSS JOIN dates CROSS JOIN range(240) t(i) WHERE d IN ({quoted})"
)
for name, query in queries.items():
    if name != "daily_bars":
        query = query.replace("'v2' AS data_version", "'v1' AS data_version")
    directory = lake / "curated" / name / "trade_date=2099-12"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "data.parquet"
    con.execute(
        "COPY ("
        + query
        + ") TO '"
        + str(path).replace("\\", "/").replace("'", "''")
        + "' (FORMAT PARQUET)"
    )
con.close()
config = {
    "lake": str(lake),
    "db": str(db),
    "start": days[0].isoformat(),
    "end": days[-1].isoformat(),
    "symbols": [],
    "datasets": list(queries),
    "threads": 2,
    "memory_limit": "1GB",
    "require_volume_v2": True,
}
config_path = root / "config.json"
config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
results = {}
for stage in ["cold", "warm"]:
    start = time.perf_counter()
    result = subprocess.run(
        [
            str(exe),
            "ingest",
            "--config",
            str(config_path),
            "--report",
            str(root / (stage + ".json")),
        ],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    report = json.loads(result.stdout)
    assert report["ok"] and report["transaction"]["committed"], report
    results[stage] = {
        "seconds": time.perf_counter() - start,
        "counts": report["counts"],
        "timings": report["timings"],
    }
    if stage == "warm":
        assert report["counts"]["rows_inserted"] == 0 and report["counts"]["rows_updated"] == 0
        assert report["counts"]["files_changed"] == 0
with duckdb.connect(str(db), read_only=True) as output:
    assert (
        output.execute("SELECT count(*) FROM ashare_1d_stock").fetchone()[0]
        == args.codes * args.days
    )
    assert (
        output.execute("SELECT count(*) FROM ashare_1m_stock").fetchone()[0]
        == args.codes * len(minute_days) * 240
    )
    assert (
        output.execute(
            "SELECT count(*) FROM ashare_1m_stock WHERE trade_time LIKE ?", ["%09:30:00"]
        ).fetchone()[0]
        == 0
    )
    assert (
        output.execute(
            "SELECT count(*) FROM ashare_1d_stock WHERE preclose IS NULL OR adj_factor IS NULL"
        ).fetchone()[0]
        == 0
    )
    assert output.execute("SELECT min(volume),max(volume) FROM ashare_1m_stock").fetchone() == (
        100.0,
        100.0,
    )
result = {
    "schema_version": 1,
    "engine_version": "v1.5.6",
    "codes": args.codes,
    "days": args.days,
    "minute_days": len(minute_days),
    "native": results,
    "independent_python_validation": True,
    "output": str(root),
}
(root / "benchmark.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False))
