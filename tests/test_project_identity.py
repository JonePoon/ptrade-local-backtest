"""Independent product identity must not erase the reused framework's provenance."""

from __future__ import annotations

import hashlib
import runpy
import sys
from pathlib import Path

import pytest

import ptrade_local_backtest as public
from ptrade_sim import cli, runtime

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def test_distribution_first_developer_and_compatibility_entrypoints():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert metadata["name"] == "ptrade-local-backtest" and metadata["version"] == "0.1.0"
    assert metadata["authors"][0]["name"] == metadata["maintainers"][0]["name"] == "JonePoon"
    assert (
        metadata["scripts"]["ptrade-local-backtest"]
        == metadata["scripts"]["ptrade-local"]
        == "ptrade_local_backtest:main"
    )
    assert metadata["scripts"]["ptrade-sim"] == "ptrade_sim.cli:main"
    assert metadata["urls"]["Homepage"] == "https://github.com/JonePoon/ptrade-local-backtest"
    assert metadata["urls"]["Framework reference"] == "https://github.com/ceke233/ptrade-sim"


def test_public_api_reuses_exact_compatible_implementation():
    assert public.BacktestEngine is runtime.BacktestEngine
    assert public.main is cli.main
    assert public.PROJECT_NAME == "ptrade 本地回测框架"
    assert public.PRIMARY_DEVELOPER == "JonePoon" and public.__version__ == "0.1.0"


def test_primary_module_version_uses_new_identity(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ptrade-local-backtest", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("ptrade_local_backtest", run_name="__main__")
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "ptrade-local-backtest 0.1.0"


def test_original_mit_and_third_party_notice_are_preserved():
    original = (ROOT / "LICENSES/ptrade-sim-MIT.txt").read_bytes()
    assert (
        hashlib.sha256(original).hexdigest()
        == "1c6df15d98d43336bd3bd7117d2739a1e303468458d71848bb30c1a4cb1eac70"
    )
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 JonePoon" in text and "Copyright (c) 2026 ceke233" in text
    assert "ceke233/ptrade-sim" in (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    notice = (ROOT / "native-fetch/NOTICE.tdxpy").read_bytes()
    assert (
        hashlib.sha256(notice).hexdigest()
        == "b65ba06aec3e18cd1fad4b402a5ae74af64db175a7df7ae3ceb68ff62aae5456"
    )
