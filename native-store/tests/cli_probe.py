"""Exercise real CLI control/reporting and cold-start rollback on work data."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import duckdb

p = argparse.ArgumentParser()
p.add_argument("--exe", type=Path, required=True)
p.add_argument("--lake", type=Path, required=True)
a = p.parse_args()
exe = a.exe.resolve()
root = Path(__file__).resolve().parents[1] / "work" / ("cli-probe-" + str(time.time_ns()))
root.mkdir(parents=True)


def invoke(operation, *flags):
    result = subprocess.run(
        [str(exe), operation, *map(str, flags)], capture_output=True, text=True, encoding="utf-8"
    )
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1 and report["operation"] == operation
    return result.returncode, report


code, doctor = invoke("doctor")
assert code == 0 and doctor["engine_version"] == "v1.5.6" and doctor["files"] == []
config = {
    "lake": str(a.lake.resolve()),
    "db": str(root / "cold.duckdb"),
    "start": "2026-11-06",
    "end": "2026-11-06",
    "symbols": ["000001.SZ", "600000.SH"],
    "datasets": ["daily_bars", "minute_bars", "minute_bars_5m"],
    "threads": 2,
    "memory_limit": "1GB",
    "require_volume_v2": True,
}
path = root / "config.json"
path.write_text(json.dumps(config), encoding="utf-8")
code, report = invoke(
    "ingest",
    "--config",
    path,
    "--report",
    root / "failure.json",
    "--progress",
    root / "progress.json",
)
assert code == 3 and report["status"] == "failed" and report["transaction"]["rolled_back"]
assert not report["transaction"]["committed"] and report["before"] == report["after"]
assert any(
    "required_previous_close_anchor" in e["message"] or "required_hfq_anchor" in e["message"]
    for e in report["errors"]
)
with duckdb.connect(str(root / "cold.duckdb"), read_only=True) as con:
    assert (
        con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='ashare_1d_stock'"
        ).fetchone()[0]
        == 0
    )
before = path.read_bytes()
code, unsafe = invoke("ingest", "--config", path, "--report", path)
assert code == 2 and unsafe["status"] == "failed" and path.read_bytes() == before
result = {
    "schema_version": 1,
    "doctor": True,
    "cold_start_rolled_back": True,
    "no_fabricated_anchor": True,
    "config_output_protected": True,
    "root": str(root),
}
(root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
