"""Package this project's verified wheels, native tools and exact Git source ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_zip(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive CRC mismatch: " + str(path))
        names = archive.namelist()
        if any(PurePosixPath(n).is_absolute() or ".." in PurePosixPath(n).parts for n in names):
            raise ValueError("Unsafe archive path")
        return names


def verify_licenses(path: Path, *, require_tdx: bool) -> None:
    """Keep exact reused-code copyright notices, not just their filenames."""
    with zipfile.ZipFile(path) as archive:
        files = [name for name in archive.namelist() if not name.endswith("/")]
        original = [name for name in files if name.endswith("LICENSES/ptrade-sim-MIT.txt")]
        if not original or any(
            digest(archive.read(name))
            != "1c6df15d98d43336bd3bd7117d2739a1e303468458d71848bb30c1a4cb1eac70"
            for name in original
        ):
            raise ValueError("Original ptrade-sim MIT copyright missing or changed")
        licenses = [name for name in files if name == "LICENSE" or name.endswith("/LICENSE")]
        if not licenses or not any(
            b"Copyright (c) 2026 JonePoon" in archive.read(name)
            and b"Copyright (c) 2026 ceke233" in archive.read(name)
            for name in licenses
        ):
            raise ValueError("Project/dependency copyright notice missing")
        for required in ("AUTHORS.md", "THIRD_PARTY_NOTICES.md"):
            names = [name for name in files if name == required or name.endswith("/" + required)]
            if not names or not all(archive.read(name).strip() for name in names):
                raise ValueError("Attribution file missing or empty: " + required)
        if require_tdx:
            notices = [
                name for name in files if name == "NOTICE.tdxpy" or name.endswith("/NOTICE.tdxpy")
            ]
            if not notices or any(
                digest(archive.read(name))
                != "b65ba06aec3e18cd1fad4b402a5ae74af64db175a7df7ae3ceb68ff62aae5456"
                for name in notices
            ):
                raise ValueError("Original tdxpy notice missing or changed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--platform", default="windows-x64")
    args = parser.parse_args(argv)
    root = args.output.resolve()
    version = args.version
    source = args.source_zip.resolve()
    source_names = verify_zip(source)
    required_source = (
        "LICENSE",
        "LICENSES/ptrade-sim-MIT.txt",
        "THIRD_PARTY_NOTICES.md",
        "AUTHORS.md",
        "README.md",
        "src/ptrade_local_backtest/__init__.py",
    )
    if not all(any(n.endswith("/" + part) for n in source_names) for part in required_source):
        raise ValueError(
            "Source archive is missing identity, compatibility or original-license files"
        )
    core = root / ("ptrade_local_backtest-" + version + "-py3-none-any.whl")
    native_candidates = list(root.glob("ptrade_local_backtest_native-" + version + "-*.whl"))
    if len(native_candidates) != 1:
        raise ValueError("Select an output containing exactly one platform native wheel")
    native = native_candidates[0]
    tools = root / ("ptrade-local-backtest-data-" + args.platform + ".zip")
    sdist = root / ("ptrade_local_backtest-" + version + ".tar.gz")
    for archive_path in (core, native, tools):
        names = verify_zip(archive_path)
        if not any(n.endswith("LICENSES/ptrade-sim-MIT.txt") for n in names):
            raise ValueError("Original framework MIT notice missing: " + archive_path.name)
    verify_licenses(source, require_tdx=True)
    verify_licenses(core, require_tdx=True)
    verify_licenses(native, require_tdx=False)
    verify_licenses(tools, require_tdx=True)
    core_names = verify_zip(core)
    if "ptrade_sim/web/dist/index.html" not in core_names:
        raise ValueError("Core wheel has no built dashboard")
    if any(n.endswith((".exe", ".dll", ".pyd", ".so")) for n in core_names):
        raise ValueError("Platform binary in universal core wheel")
    assets = [core, native, tools, sdist, source]
    for file in assets:
        if not file.is_file():
            raise ValueError("Missing release asset: " + str(file))
    records = [
        {"name": f.name, "bytes": f.stat().st_size, "sha256": digest(f.read_bytes())}
        for f in assets
    ]
    checks = "".join(r["sha256"] + "  " + r["name"] + "\n" for r in records)
    install = f"""# ptrade 本地回测框架 {version} · {args.platform}

第一开发者/维护者：JonePoon。框架参考和基础代码复用范围见源码中的THIRD_PARTY_NOTICES.md；原始MIT全文和ceke233版权随分发保留。

在解包目录打开PowerShell：

~~~powershell
py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install "./packages/{core.name}[duckdb,dashboard,perf]" ./packages/{native.name}
if ($LASTEXITCODE -ne 0) {{ throw "Python包安装失败" }}
Expand-Archive ./packages/{tools.name} ./native-tools
& ./.venv/Scripts/python.exe ./native-tools/install_native_tools.py --source ./native-tools/bin --manifest ./native-tools/MANIFEST.json
if ($LASTEXITCODE -ne 0) {{ throw "原生工具校验/安装失败" }}
& ./.venv/Scripts/ptrade-local-backtest.exe --version
& ./.venv/Scripts/python.exe -c "import ptrade_local_backtest as p; print(p.PROJECT_NAME,p.PRIMARY_DEVELOPER,p.__version__)"
& ./.venv/Scripts/ptrade-local-backtest.exe dashboard --root ./backtest_results --host 127.0.0.1 --port 8765
~~~

打开 http://127.0.0.1:8765 。已备齐本地历史行情和参考数据后，本地回测/复盘/静态导出不依赖PTrade云服务；在线行情更新仍需要对应数据源。配置与完整指南在source/{source.name}，或 https://github.com/JonePoon/ptrade-local-backtest 。内部ptrade_sim导入和ptrade-sim命令是旧策略兼容入口，产品与发行身份是独立项目。
"""
    (root / "INSTALL.md").write_text(install, encoding="utf-8", newline="\n")
    bundle = root / ("ptrade-local-backtest-" + version + "-" + args.platform + ".zip")
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in (core, native, tools):
            archive.write(file, "packages/" + file.name)
        archive.write(source, "source/" + source.name)
        archive.writestr("INSTALL.md", install)
        archive.writestr("SHA256SUMS", checks)
    verify_zip(bundle)
    records.append(
        {"name": bundle.name, "bytes": bundle.stat().st_size, "sha256": digest(bundle.read_bytes())}
    )
    (root / "SHA256SUMS").write_text(
        "".join(r["sha256"] + "  " + r["name"] + "\n" for r in records),
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "project": "ptrade-local-backtest",
        "version": version,
        "git_revision": args.revision,
        "status": "verified",
        "assets": records,
    }
    (root / "release-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
