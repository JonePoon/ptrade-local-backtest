"""Guard release integrity, startup validation, and failed-copy atomicity."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project_fixture(tmp_path):
    root = tmp_path / "project"
    suffix = ".exe" if os.name == "nt" else ""
    binaries = {}
    for kind in ("fetch", "store"):
        file = root / ("native-" + kind) / "target" / "release" / ("ptrade-" + kind + suffix)
        file.parent.mkdir(parents=True)
        file.write_bytes((kind + "-native-test-bytes").encode())
        binaries[kind] = file
    for relative in (
        "scripts/install_native_tools.py",
        "scripts/build_native_data.ps1",
        "data_config.example.json",
        "docs/native_data_chain.md",
        "LICENSE",
        "native-fetch/NOTICE.tdxpy",
        "LICENSES/ptrade-sim-MIT.txt",
        "THIRD_PARTY_NOTICES.md",
        "AUTHORS.md",
    ):
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("release fixture: " + relative, encoding="utf-8")
    return root, binaries


def doctor_result(command, **kwargs):
    kind = "store" if "store" in Path(command[0]).name else "fetch"
    assert command[1:] == ["doctor"]
    assert kwargs["timeout"] == 30 and kwargs["check"] is False
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {"status": "succeeded", "engine_version": "v1.5.6" if kind == "store" else None}
        ),
    )


def install_fixture(tmp_path):
    source = tmp_path / "release" / "bin"
    source.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    records = []
    for kind in ("fetch", "store"):
        file = source / ("ptrade-" + kind + suffix)
        file.write_bytes((kind + "-trusted").encode())
        records.append(
            {
                "path": "bin/" + file.name,
                "bytes": file.stat().st_size,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            }
        )
    manifest = source.parent / "MANIFEST.json"
    manifest.write_text(json.dumps({"format_version": 1, "files": records}), encoding="utf-8")
    return source, manifest, records


def test_package_contains_verified_binaries_licenses_and_receipt(tmp_path, monkeypatch, capsys):
    module = load_helper("package_data_release")
    root, binaries = project_fixture(tmp_path)
    monkeypatch.setattr(
        module.subprocess, "run", doctor_result
    )  # Unit fixture only; real CLI is separately exercised.
    output = tmp_path / "out"
    assert module.main(["--project", str(root), "--output", str(output)]) == 0
    receipt = json.loads((output / "native-tools-receipt.json").read_text())
    assert Path(receipt["path"]).name == receipt["path"], (
        "Public receipts must not contain local absolute paths"
    )
    archive_path = output / receipt["path"]
    assert receipt["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive_path) as archive:
        assert {"MANIFEST.json", "LICENSE", "NOTICE.tdxpy", "install_native_tools.py"} <= set(
            archive.namelist()
        )
        manifest = json.loads(archive.read("MANIFEST.json"))
        assert manifest["duckdb_version"] == "1.5.6"
        for record in manifest["files"]:
            data = archive.read(record["path"])
            assert (
                len(data) == record["bytes"]
                and hashlib.sha256(data).hexdigest() == record["sha256"]
            )
        assert all(
            archive.read("bin/" + file.name) == file.read_bytes() for file in binaries.values()
        )
    assert json.loads(capsys.readouterr().out)["path"] == receipt["path"]


@pytest.mark.parametrize("defect", ["exit", "json", "status", "version"])
def test_package_bad_doctor_never_publishes(tmp_path, monkeypatch, defect):
    module = load_helper("package_data_release")
    root, _ = project_fixture(tmp_path)

    def broken(command, **kwargs):
        result = doctor_result(command, **kwargs)
        if defect == "exit":
            result.returncode = 3
        elif defect == "json":
            result.stdout = "not-json"
        elif defect == "status":
            result.stdout = json.dumps({"status": "partial"})
        elif "store" in Path(command[0]).name:
            result.stdout = json.dumps({"status": "succeeded", "engine_version": "v2.0.0"})
        return result

    monkeypatch.setattr(module.subprocess, "run", broken)
    output = tmp_path / "out"
    with pytest.raises(SystemExit):
        module.main(["--project", str(root), "--output", str(output)])
    assert not list(output.glob("*.zip")), (
        "A failed startup must not leave a plausible release archive"
    )


def test_package_requires_notice_and_missing_binary_is_loud(tmp_path, monkeypatch):
    module = load_helper("package_data_release")
    root, binaries = project_fixture(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", doctor_result)
    (root / "native-fetch" / "NOTICE.tdxpy").unlink()
    with pytest.raises(SystemExit, match="required release file missing"):
        module.main(["--project", str(root), "--output", str(tmp_path / "out")])
    binaries["fetch"].unlink()
    with pytest.raises(SystemExit, match="native tool missing"):
        module.main(["--project", str(root), "--output", str(tmp_path / "out")])


def test_archive_digest_check_is_a_real_error_not_an_assert(tmp_path):
    module = load_helper("package_data_release")
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("bin/tool", b"changed")
    records = [{"path": "bin/tool", "bytes": 7, "sha256": "0" * 64}]
    with pytest.raises(RuntimeError, match="digest mismatch"):
        module._verify_archive(path, records)


def test_installer_verifies_manifest_and_staged_bytes(tmp_path, monkeypatch, capsys):
    module = load_helper("install_native_tools")
    source, _, _ = install_fixture(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", doctor_result)
    target = tmp_path / "installed"
    assert module.main(["--source", str(source), "--target", str(target)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["manifest_verified"] is True and len(receipt["installed"]) == 2
    for file in source.iterdir():
        assert (target / file.name).read_bytes() == file.read_bytes()
    assert len(list(target.iterdir())) == 2, (
        "Staging files must be removed after successful install"
    )


@pytest.mark.parametrize(
    "defect", ["digest", "bytes", "missing", "duplicate", "shape", "version", "json"]
)
def test_installer_bad_manifest_blocks_execution_and_preserves_target(
    tmp_path, monkeypatch, defect
):
    module = load_helper("install_native_tools")
    source, manifest, records = install_fixture(tmp_path)
    value = {"format_version": 1, "files": records}
    if defect == "digest":
        records[0]["sha256"] = "0" * 64
    elif defect == "bytes":
        records[0]["bytes"] += 1
    elif defect == "missing":
        records.pop()
    elif defect == "duplicate":
        records.append(records[0].copy())
    elif defect == "shape":
        value = []
    elif defect == "version":
        value["format_version"] = 2
    manifest.write_text("broken" if defect == "json" else json.dumps(value), encoding="utf-8")
    target = tmp_path / "installed"
    target.mkdir()
    old = target / records[0]["path"].split("/")[-1]
    old.write_bytes(b"previous-installed-tool")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Unverified binary must never execute"),
    )
    with pytest.raises(SystemExit):
        module.main(["--source", str(source), "--target", str(target)])
    assert old.read_bytes() == b"previous-installed-tool"
    assert list(target.iterdir()) == [old]


def test_installer_source_change_during_copy_preserves_existing_tools(tmp_path, monkeypatch):
    module = load_helper("install_native_tools")
    source, _, records = install_fixture(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", doctor_result)
    target = tmp_path / "installed"
    target.mkdir()
    old = target / records[0]["path"].split("/")[-1]
    old.write_bytes(b"old-tool")
    copy = module.shutil.copy2

    def changed(src, destination):
        copy(src, destination)
        Path(destination).write_bytes(b"changed-after-validation")

    monkeypatch.setattr(module.shutil, "copy2", changed)
    with pytest.raises(SystemExit, match="changed while copying"):
        module.main(["--source", str(source), "--target", str(target)])
    assert old.read_bytes() == b"old-tool" and list(target.iterdir()) == [old]


def test_installer_explicit_missing_manifest_does_not_silently_downgrade(tmp_path, monkeypatch):
    module = load_helper("install_native_tools")
    source, _, _ = install_fixture(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("missing manifest"))
    with pytest.raises(SystemExit, match="manifest missing"):
        module.main(
            [
                "--source",
                str(source),
                "--target",
                str(tmp_path / "target"),
                "--manifest",
                str(tmp_path / "absent.json"),
            ]
        )
