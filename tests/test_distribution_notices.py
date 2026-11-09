"""Complete packages must fail closed if actual original attribution is removed."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def packager():
    spec = importlib.util.spec_from_file_location(
        "package_project_release", ROOT / "scripts/package_project_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_zip(path, defect=None):
    files = {
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
        "LICENSES/ptrade-sim-MIT.txt": (ROOT / "LICENSES/ptrade-sim-MIT.txt").read_bytes(),
        "AUTHORS.md": (ROOT / "AUTHORS.md").read_bytes(),
        "THIRD_PARTY_NOTICES.md": (ROOT / "THIRD_PARTY_NOTICES.md").read_bytes(),
        "NOTICE.tdxpy": (ROOT / "native-fetch/NOTICE.tdxpy").read_bytes(),
    }
    if defect in files:
        files.pop(defect)
    elif defect == "changed-mit":
        files["LICENSES/ptrade-sim-MIT.txt"] += b"changed"
    elif defect == "changed-tdx":
        files["NOTICE.tdxpy"] += b"changed"
    elif defect == "copyright":
        files["LICENSE"] = b"Copyright (c) 2026 JonePoon only"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def test_exact_original_notices_survive_complete_distribution(tmp_path):
    packager().verify_licenses(create_zip(tmp_path / "good.zip"), require_tdx=True)


@pytest.mark.parametrize(
    "defect",
    [
        "LICENSE",
        "LICENSES/ptrade-sim-MIT.txt",
        "AUTHORS.md",
        "THIRD_PARTY_NOTICES.md",
        "NOTICE.tdxpy",
        "changed-mit",
        "changed-tdx",
        "copyright",
    ],
)
def test_removing_or_changing_attribution_blocks_distribution(tmp_path, defect):
    with pytest.raises(ValueError):
        packager().verify_licenses(create_zip(tmp_path / "bad.zip", defect), require_tdx=True)
