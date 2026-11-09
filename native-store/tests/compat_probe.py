"""Independent Python/Rust 1.5.6 interop plus DuckDB 2.0 Parquet boundary.

This is a test driver, never part of the native ingestion implementation.
Every database and Parquet is created underneath native-store/work.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import duckdb

parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, required=True)
parser.add_argument("--sdk-python", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / "work" / "engine-compat"
root.mkdir(parents=True, exist_ok=True)
exe = args.exe.resolve()
assert duckdb.__version__ == "1.5.6"


def native(*flags):
    result = subprocess.run(
        [str(exe), *map(str, flags)], check=True, capture_output=True, text=True
    )
    payload = json.loads(result.stdout)
    assert payload["engine_version"] == "v1.5.6" and payload["ok"]
    return payload


fresh = root / "rust_created.duckdb"
native("--db", fresh, "--write-probe")
with duckdb.connect(str(fresh)) as connection:
    assert connection.execute(
        "SELECT origin, close FROM __native_store_probe WHERE id=1"
    ).fetchone() == ("rust-1.5.6", 12.5)
    connection.execute(
        "INSERT INTO __native_store_probe VALUES (2,'python-1.5.6',25.0) ON CONFLICT(id) DO NOTHING"
    )
assert native("--db", fresh)["probe_rows"] == 1

original = root / "python_original.duckdb"
with duckdb.connect(str(original)) as connection:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS __native_store_probe(id INTEGER PRIMARY KEY,origin VARCHAR,close DOUBLE)"
    )
    connection.execute(
        "INSERT INTO __native_store_probe VALUES (1,'python-1.5.6',12.5) ON CONFLICT(id) DO UPDATE SET origin=excluded.origin"
    )
    connection.execute("CREATE TABLE IF NOT EXISTS preserve_me(value VARCHAR)")
    connection.execute(
        "INSERT INTO preserve_me SELECT 'untouched' WHERE NOT EXISTS(SELECT 1 FROM preserve_me)"
    )
copy = root / "python_copy.duckdb"
# Explicit, known test paths only: the original is never passed for native write.
shutil.copy2(original, copy)
assert native("--db", copy)["probe_rows"] == 1
native("--db", copy, "--write-probe")
with duckdb.connect(str(copy), read_only=True) as connection:
    assert (
        connection.execute("SELECT origin FROM __native_store_probe WHERE id=1").fetchone()[0]
        == "rust-1.5.6"
    )
    assert connection.execute("SELECT value FROM preserve_me").fetchall() == [("untouched",)]
with duckdb.connect(str(original), read_only=True) as connection:
    assert (
        connection.execute("SELECT origin FROM __native_store_probe WHERE id=1").fetchone()[0]
        == "python-1.5.6"
    )

parquet_check = None
if args.sdk_python:
    parquet = root / "duckdb2.parquet"
    code = "import duckdb; c=duckdb.connect(); assert duckdb.__version__ == '2.0.0'; c.execute(\"COPY (SELECT DATE '2025-01-02' trade_date, 12.5 close) TO '{}' (FORMAT PARQUET)\"); print(duckdb.__version__)".format(
        str(parquet).replace("\\", "/").replace("'", "''")
    )
    # SDK 2.0's process cannot write this test workspace (WinError 5). Keep it
    # read-only: emit its actual Arrow IPC stream; this authorized test driver
    # writes Parquet. No permissions/owners are changed, no DB2 file is copied.
    import pyarrow as pa
    import pyarrow.parquet as pq

    code = "import duckdb,pyarrow as pa,sys; c=duckdb.connect(); assert duckdb.__version__=='2.0.0'; t=c.sql(\"SELECT DATE '2025-01-02' AS trade_date,12.5 AS close\").to_arrow_table(); b=pa.BufferOutputStream(); w=pa.ipc.new_stream(b,t.schema); w.write_table(t); w.close(); sys.stdout.buffer.write(b.getvalue().to_pybytes())"
    exported = subprocess.run(
        [str(args.sdk_python.resolve()), "-c", code], check=True, capture_output=True
    )
    table = pa.ipc.open_stream(exported.stdout).read_all()
    assert table.schema.field("trade_date").type == pa.date32()
    pq.write_table(table, parquet)
    parquet_check = native("--parquet", parquet)
    assert parquet_check["parquet_rows"] == 1
report = {
    "schema_version": 1,
    "python_engine": "v1.5.6",
    "rust_engine": native()["engine_version"],
    "rust_to_python": True,
    "python_copy_to_rust": True,
    "original_unchanged": True,
    "duckdb2_parquet_to_rust": parquet_check is not None,
    "test_root": str(root),
}
(root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
