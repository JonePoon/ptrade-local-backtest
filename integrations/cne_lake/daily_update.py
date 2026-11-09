"""Existing daily entry point for the native data chain; no schedule registration.

Default target remains cne_lake/out/fixed.duckdb. Configuration and CLI can
select another lake/database. The old Python path is retained by --backend python.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PT_ROOT = ROOT / "ptrade-local-backtest"
CNE_PY = HERE / ".venv" / "Scripts" / "python.exe"
PT_PY = PT_ROOT / ".venv" / "Scripts" / "python.exe"
LAKE = HERE / "lake"
ENGINE_DB = HERE / "out" / "fixed.duckdb"
TRAILING_DAYS = 12


def _run(command: list[str], log_file) -> int:
    environment = dict(os.environ)
    environment.update(
        {
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    log_file.write(
        json.dumps(
            {"command": command, "started_at": dt.datetime.now().isoformat()}, ensure_ascii=False
        )
        + "\n"
    )
    log_file.flush()
    process = subprocess.run(
        command, cwd=PT_ROOT, env=environment, stdout=log_file, stderr=log_file, check=False
    )
    log_file.write(f"exit={process.returncode}\n")
    log_file.flush()
    return process.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["rust", "python"], default="rust")
    parser.add_argument("--config", default=None, help="Rust data configuration JSON")
    parser.add_argument("--lake", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None, help="独立任务结果根目录")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--no-reference-refresh", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Read-only plan, never download/ingest")
    args = parser.parse_args(argv)
    if args.backend == "rust":
        command = [
            str(PT_PY),
            "-m",
            "ptrade_sim.cli",
            "data",
            "plan" if args.plan else "sync",
            "--json",
        ]
        # 未指定路径时尊重配置；只有无配置的旧每日入口才沿用正式默认路径。
        for key, fallback in (("lake", LAKE), ("db", ENGINE_DB)):
            value = getattr(args, key)
            if value is not None or not args.config:
                command += ["--" + key, str((value or fallback).resolve())]
        if args.output_dir is not None:
            command += ["--output-dir", str(args.output_dir.resolve())]
        if args.config:
            command += ["--config", str(Path(args.config).resolve())]
        command += [
            "--no-refresh-reference" if args.no_reference_refresh else "--refresh-reference"
        ]
        for key in ("start", "end", "symbols"):
            value = getattr(args, key)
            if value is not None:
                command += ["--" + key, value]
        if args.plan:
            environment = dict(os.environ)
            environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
            return subprocess.run(command, cwd=PT_ROOT, env=environment, check=False).returncode
        log_root = (args.output_dir.resolve() / "logs") if args.output_dir else HERE / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / f"daily_update_{dt.date.today():%Y%m%d}.log"
        with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
            return _run(command, log_file)
    args.lake = args.lake or LAKE
    args.db = args.db or ENGINE_DB
    if args.plan:
        print(
            json.dumps(
                {
                    "backend": "python",
                    "lake": str(args.lake),
                    "db": str(args.db),
                    "minute_enabled": True,
                    "write": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    log_root = (args.output_dir.resolve() / "logs") if args.output_dir else HERE / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / f"daily_update_{dt.date.today():%Y%m%d}.log"
    with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
        code = _run(
            [
                str(CNE_PY),
                "-m",
                "cnequity",
                "run",
                "daily",
                "--group",
                "core",
                "--config",
                str(HERE / "lake.toml"),
            ],
            log_file,
        )
        if code != 0:
            return code
        end = args.end
        if end is None:
            date = dt.date.today()
            while date.weekday() >= 5:
                date -= dt.timedelta(days=1)
            end = date.isoformat()
        start = (
            args.start
            or (dt.date.fromisoformat(end) - dt.timedelta(days=TRAILING_DAYS)).isoformat()
        )
        command = [
            str(PT_PY),
            str(HERE / "run.py"),
            "build",
            "--lake",
            str(args.lake.resolve()),
            "--db",
            str(args.db.resolve()),
            "--start",
            start,
            "--end",
            end,
            "--with-minute",
        ]
        if args.symbols:
            command += ["--symbols", args.symbols]
        return _run(command, log_file)


if __name__ == "__main__":
    sys.exit(main())
