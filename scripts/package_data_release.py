"""Create an atomic native-tools archive with startup and SHA256 receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import zipfile
from pathlib import Path


def _doctor(path: Path, kind: str) -> dict:
    try:
        result = subprocess.run(
            [str(path), "doctor"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        report = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError) as exc:
        raise SystemExit(f"native doctor failed for {kind}: {exc}") from exc
    if not isinstance(report, dict) or result.returncode != 0:
        raise SystemExit("native doctor failed: " + kind)
    if report.get("status") not in {"succeeded", "ok"}:
        raise SystemExit("native doctor did not succeed: " + kind)
    if kind == "store" and report.get("engine_version") != "v1.5.6":
        raise SystemExit("incompatible native DuckDB writer")
    return report


def _platform_tag() -> str:
    machine = platform.machine().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    if not architecture or not all(c.isalnum() or c == "_" for c in architecture):
        raise SystemExit("unsupported native architecture")
    return platform.system().lower() + "-" + architecture


def _verify_archive(path: Path, records: list[dict]) -> None:
    with zipfile.ZipFile(path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise RuntimeError("native archive CRC failure: " + corrupt)
        for record in records:
            payload = archive.read(record["path"])
            if (
                len(payload) != record["bytes"]
                or hashlib.sha256(payload).hexdigest() != record["sha256"]
            ):
                raise RuntimeError("native archive digest mismatch: " + record["path"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    suffix = ".exe" if os.name == "nt" else ""
    tag = _platform_tag()
    archive_path = output / ("ptrade-local-backtest-data-" + tag + ".zip")
    records, files = [], []
    for kind in ("fetch", "store"):
        path = project / ("native-" + kind) / "target" / "release" / ("ptrade-" + kind + suffix)
        if not path.is_file():
            raise SystemExit("native tool missing: " + str(path))
        doctor = _doctor(path, kind)
        data = path.read_bytes()
        name = "bin/" + path.name
        records.append(
            {
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "doctor": doctor,
            }
        )
        files.append((name, data, 0o755))
    extras = [
        (project / "scripts" / "install_native_tools.py", "install_native_tools.py"),
        (project / "scripts" / "build_native_data.ps1", "build_native_data.ps1"),
        (project / "data_config.example.json", "data_config.example.json"),
        (project / "docs" / "native_data_chain.md", "native_data_chain.md"),
        (project / "LICENSE", "LICENSE"),
        (project / "LICENSES" / "ptrade-sim-MIT.txt", "LICENSES/ptrade-sim-MIT.txt"),
        (project / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
        (project / "AUTHORS.md", "AUTHORS.md"),
        (project / "native-fetch" / "NOTICE.tdxpy", "NOTICE.tdxpy"),
    ]
    for path, name in extras:
        if not path.is_file():
            raise SystemExit("required release file missing: " + str(path))
        data = path.read_bytes()
        records.append(
            {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
        files.append((name, data, 0o644))
    manifest = {"format_version": 1, "platform": tag, "duckdb_version": "1.5.6", "files": records}
    output.mkdir(parents=True, exist_ok=True)
    fd, tempname = tempfile.mkstemp(prefix=archive_path.name + ".", suffix=".tmp", dir=output)
    os.close(fd)
    temporary = Path(tempname)
    try:
        # Compression is the bottleneck; fixed ZIP metadata keeps source-file mtimes out.
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data, mode in sorted(files):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100000 | mode) << 16
                archive.writestr(info, data)
            archive.writestr(
                "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            )
        _verify_archive(temporary, records)  # Real checks, also active under python -O.
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {
        "path": archive_path.name,
        "bytes": archive_path.stat().st_size,
        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "manifest": manifest,
    }
    receipt_path = output / "native-tools-receipt.json"
    fd, tempname = tempfile.mkstemp(prefix=receipt_path.name + ".", dir=output)
    os.close(fd)
    temporary = Path(tempname)
    try:
        temporary.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        temporary.replace(receipt_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({k: v for k, v in receipt.items() if k != "manifest"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
