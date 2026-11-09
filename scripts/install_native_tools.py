"""Install verified native CLI tools into the selected Python package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _manifest_records(path: Path) -> dict[str, dict]:
    if path.stat().st_size > 1024 * 1024:
        raise SystemExit("native manifest exceeds 1 MiB")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"invalid native manifest: {exc}") from exc
    if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int:
        raise SystemExit("invalid native manifest format")
    if manifest["format_version"] != 1 or not isinstance(manifest.get("files"), list):
        raise SystemExit("unsupported native manifest format")
    records = {}
    for record in manifest["files"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise SystemExit("invalid native manifest file record")
        name = record["path"]
        if name in records:
            raise SystemExit("duplicate native manifest path: " + name)
        records[name] = record
    return records


def _doctor(file: Path, kind: str) -> dict:
    try:
        result = subprocess.run(
            [str(file), "doctor"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        report = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError) as exc:
        raise SystemExit(f"native doctor failed for {file.name}: {exc}") from exc
    if not isinstance(report, dict) or result.returncode != 0:
        raise SystemExit("native doctor failed: " + file.name)
    if report.get("status") not in {"ok", "succeeded", "success"}:
        raise SystemExit("native doctor did not succeed: " + file.name)
    if kind == "store" and report.get("engine_version") != "v1.5.6":
        raise SystemExit("store engine version is not v1.5.6")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, type=Path, help="Directory containing native tools"
    )
    parser.add_argument("--target", type=Path, help="Default: installed ptrade_sim/bin")
    parser.add_argument(
        "--manifest", type=Path, help="Default: MANIFEST.json beside the bin directory"
    )
    args = parser.parse_args(argv)
    source = args.source.resolve()
    if args.target is None:
        import ptrade_sim

        target = Path(ptrade_sim.__file__).resolve().parent / "bin"
    else:
        target = args.target.resolve()
    manifest_path = args.manifest.resolve() if args.manifest else source.parent / "MANIFEST.json"
    if args.manifest and not manifest_path.is_file():
        raise SystemExit("native manifest missing: " + str(manifest_path))
    expected = _manifest_records(manifest_path) if manifest_path.is_file() else None
    suffix = ".exe" if os.name == "nt" else ""
    entries = []
    # Verify every input before executing either binary or modifying the target.
    for kind in ("fetch", "store"):
        name = "ptrade-" + kind + suffix
        file = source / name
        if not file.is_file():
            raise SystemExit("missing native tool: " + str(file))
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if expected is not None:
            record = expected.get("bin/" + name)
            if record is None or type(record.get("bytes")) is not int:
                raise SystemExit("native manifest entry missing or invalid: " + name)
            wanted = record.get("sha256")
            if not isinstance(wanted, str) or len(wanted) != 64:
                raise SystemExit("invalid native SHA256: " + name)
            if record["bytes"] != file.stat().st_size or wanted.lower() != digest:
                raise SystemExit("native manifest digest mismatch: " + name)
        entries.append((kind, file, name, digest))
    for kind, file, _, _ in entries:
        _doctor(file, kind)
    target.mkdir(parents=True, exist_ok=True)
    staged = []
    records = []
    try:
        # O(total binary bytes). Hash the staged copy to catch a concurrent source revision.
        for _, source_file, name, digest in entries:
            fd, tempname = tempfile.mkstemp(prefix=name + ".", dir=target)
            os.close(fd)
            temporary = Path(tempname)
            staged.append((temporary, target / name))
            shutil.copy2(source_file, temporary)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                raise SystemExit("native input changed while copying: " + name)
        for temporary, destination in staged:
            temporary.replace(destination)
        records = [{"path": str(target / name), "sha256": digest} for _, _, name, digest in entries]
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    print(json.dumps({"installed": records, "manifest_verified": expected is not None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
