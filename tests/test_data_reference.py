"""Reference SDK connector contracts; all lakes are synthetic or independent work copies."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ptrade_sim import data_reference as R

pytestmark = pytest.mark.unit


def _file(root, relative, data=b"synthetic-parquet-bytes"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _native(
    root,
    *,
    relative="curated/daily_bars/trade_date=2026-11/part-12345678-1234-1234-1234-123456789abc.parquet",
    bad_digest=False,
    partial=False,
):
    path = _file(root, relative, b"native-new-day")
    item = {
        "dataset": "daily_bars",
        "path": relative,
        "sha256": "0" * 64 if bad_digest else hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": 1,
        "symbol": "000001.SZ",
        "frequency": "1d",
        "data_version": "v2",
        "start": "2026-11-06",
        "end": "2026-11-06",
    }
    manifest = {
        "schema_version": 1,
        "files": [item],
        "completed": {
            "scope": {
                "dataset": "daily_bars",
                "symbol": "000001.SZ",
                "files": [relative],
                "coverage": {"partial": partial},
            }
        },
    }
    target = root / ".ptrade-fetch" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def _sdk(root):
    def logical(roots, dataset):
        return (roots.derived_root if dataset == "adj_factors" else roots.curated_root) / dataset

    def resolve(path, *, dataset, meta_root):
        pointer = meta_root / "revisions" / dataset / "current.json"
        if not pointer.exists():
            return path
        record = json.loads(pointer.read_text(encoding="utf-8"))
        if set(record) != {"generation_path"}:
            raise ValueError("bad fake pointer")
        target = meta_root / record["generation_path"]
        if not target.is_dir():
            raise ValueError("missing generation")
        return target

    return SimpleNamespace(
        registry=R.DATASETS,
        logical_root=logical,
        resolve_root=resolve,
        committed_revision=lambda *args, **kwargs: None,
    )


def test_no_pointer_selects_exact_legacy_files_without_mutating_the_lake(tmp_path):
    source = _file(tmp_path, "curated/daily_bars/trade_date=2026-11/part-0.parquet")
    before = source.read_bytes()
    result = R.resolve_datasets(
        {"lake": str(tmp_path), "datasets": ["daily_bars"]}, sdk=_sdk(tmp_path)
    )
    assert result["dataset_roots"] == {"daily_bars": [str(source)]}
    assert result["roots"][0]["committed"] is False
    assert result["sources"][0]["sha256"] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before
    assert not (tmp_path / "meta").exists()


def test_active_generation_plus_verified_native_overlay_excludes_abandoned_mutable(tmp_path):
    stale = _file(
        tmp_path, "curated/daily_bars/trade_date=2026-11/part-0.parquet", b"abandoned-stale"
    )
    active = _file(
        tmp_path,
        "meta/revisions/data/daily_bars/active/trade_date=2026-11/part-0.parquet",
        b"active-old",
    )
    pointer = tmp_path / "meta/revisions/daily_bars/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps({"generation_path": "revisions/data/daily_bars/active"}), encoding="utf-8"
    )
    overlay, _ = _native(tmp_path)
    original_pointer = pointer.read_bytes()
    result = R.resolve_datasets(
        {"lake": str(tmp_path), "datasets": ["daily_bars"]}, sdk=_sdk(tmp_path)
    )
    assert set(result["dataset_roots"]["daily_bars"]) == {str(active), str(overlay)}
    assert str(stale) not in result["dataset_roots"]["daily_bars"]
    assert result["native_files"]["daily_bars"][0]["path"] == str(overlay)
    assert result["roots"][0]["committed"] is True
    assert pointer.read_bytes() == original_pointer


def test_bad_pointer_is_not_replaced_by_a_valid_looking_mutable_fallback(tmp_path):
    _file(tmp_path, "curated/daily_bars/part-0.parquet")
    pointer = tmp_path / "meta/revisions/daily_bars/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{broken", encoding="utf-8")
    with pytest.raises(R.ReferenceError, match="resolution failed") as caught:
        R.resolve_datasets({"lake": str(tmp_path), "datasets": ["daily_bars"]}, sdk=_sdk(tmp_path))
    assert caught.value.code == "sdk_revision_invalid"
    assert pointer.read_text(encoding="utf-8") == "{broken"


def test_sdk_missing_with_a_pointer_fails_closed(tmp_path, monkeypatch):
    _file(tmp_path, "curated/daily_bars/part-0.parquet")
    pointer = tmp_path / "meta/revisions/daily_bars/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(R, "_load_sdk", lambda *args, **kwargs: None)
    result = R.run_reference({"lake": str(tmp_path), "datasets": ["daily_bars"]})
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "sdk_unavailable"
    assert result["dataset_roots"] == {}


@pytest.mark.parametrize(
    "bad_digest,partial,code",
    [(True, False, "native_digest_mismatch"), (False, True, "native_partial_scope")],
)
def test_native_bad_hash_or_partial_scope_never_becomes_a_store_input(
    tmp_path, bad_digest, partial, code
):
    _native(tmp_path, bad_digest=bad_digest, partial=partial)
    with pytest.raises(R.ReferenceError) as caught:
        R.resolve_datasets({"lake": str(tmp_path), "datasets": ["daily_bars"]}, sdk=_sdk(tmp_path))
    assert caught.value.code == code


def test_unlisted_native_shards_do_not_enter_the_legacy_scan(tmp_path):
    overlay, _ = _native(tmp_path)
    orphan = _file(
        tmp_path,
        "curated/daily_bars/trade_date=2026-11/part-12345678-1234-1234-1234-abcdefabcdef.parquet",
        b"uncommitted",
    )
    legacy = _file(tmp_path, "curated/daily_bars/trade_date=2026-11/part-0.parquet", b"legacy")
    result = R.resolve_datasets(
        {"lake": str(tmp_path), "datasets": ["daily_bars"]}, sdk=_sdk(tmp_path)
    )
    assert set(result["dataset_roots"]["daily_bars"]) == {str(legacy), str(overlay)}
    assert str(orphan) not in result["dataset_roots"]["daily_bars"]


def test_native_relative_path_cannot_escape_the_lake(tmp_path):
    _, manifest = _native(tmp_path)
    manifest["files"][0]["path"] = "../foreign.parquet"
    (tmp_path / ".ptrade-fetch/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(R.ReferenceError) as caught:
        R.resolve_datasets({"lake": str(tmp_path), "datasets": ["daily_bars"]}, sdk=_sdk(tmp_path))
    assert caught.value.code == "path_escape"


def test_missing_dataset_mapping_is_explicitly_empty_not_mutable_fallback(tmp_path):
    result = R.resolve_datasets(
        {"lake": str(tmp_path), "datasets": ["daily_bars", "adj_factors"]}, sdk=_sdk(tmp_path)
    )
    assert result["dataset_roots"] == {"daily_bars": [], "adj_factors": []}
    assert {w["dataset"] for w in result["warnings"]} == {"daily_bars", "adj_factors"}


def test_before_is_existing_noop_and_does_not_refetch_reference_sources(tmp_path, monkeypatch):
    _file(tmp_path, "curated/daily_bars/part-0.parquet")
    monkeypatch.setattr(R, "_load_sdk", lambda *args, **kwargs: None)
    result = R.run_reference({"lake": str(tmp_path), "datasets": ["daily_bars"]}, "before")
    assert result["status"] == "succeeded"
    assert result["counts"]["network_requests"] == 0
    assert any(w["code"] == "existing_reference_inputs" for w in result["warnings"])


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"lake": "x", "datasets": ["../bad"]},
        {"lake": "x", "symbols": ["bad"]},
        {"lake": "x", "start": "2026-11-07", "end": "2026-11-06"},
    ],
)
def test_configuration_errors_are_machine_readable_and_have_no_roots(value):
    result = R.run_reference(value)
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "config_error"
    assert result["dataset_roots"] == {}


def test_after_work_path_must_not_be_within_the_original_lake(tmp_path):
    with pytest.raises(R.ReferenceError) as caught:
        R.refresh_after(
            {"lake": str(tmp_path), "datasets": ["daily_bars"]},
            work_dir=tmp_path / "unsafe",
            sdk=_sdk(tmp_path),
        )
    assert caught.value.code == "path_escape"


REAL_SDK_CHECK = r"""
import datetime as dt, hashlib, json, pathlib, sys
import polars as pl
from cnequity.config import Config
from cnequity.domain.schemas import DATASET_SCHEMAS
from cnequity.storage.revisions import RevisionStore
from ptrade_sim.data_reference import run_reference
root=pathlib.Path(sys.argv[1]); lake=root/'input-lake'; work=root/'merged-work'; lake.mkdir()
old=dt.date(2026,11,5); new=dt.date(2026,11,6); stamp=dt.datetime(2026,11,6,tzinfo=dt.timezone.utc)
def daily(date,close):
 return pl.DataFrame([{'symbol':s,'trade_date':date,'open':10.,'high':12.,'low':9.,'close':close,'pre_close':None,'volume':100,'amount':1000.,'source':'tdx_protocol','data_version':'v2','fetched_at':stamp} for s in ['000001.SZ','000002.SZ']],schema=DATASET_SCHEMAS['daily_bars'])
folder=lake/'curated/daily_bars/trade_date=2026-11';folder.mkdir(parents=True); oldfile=folder/'part-0.parquet';daily(old,10.).write_parquet(oldfile)
store=RevisionStore(lake/'meta',lake/'curated',lake/'derived'); active=store.ensure_current('daily_bars')
pointer=lake/'meta/revisions/daily_bars/current.json';pointer_before=pointer.read_bytes();active_files=list(active.rglob('*.parquet'));active_before={str(p):p.read_bytes() for p in active_files}
# Poison only the abandoned mutable copy: a real revision resolver must not read it.
daily(old,11.).write_parquet(oldfile)
newfile=folder/'part-12345678-1234-1234-1234-123456789abc.parquet';daily(new,10.5).write_parquet(newfile)
rel=newfile.relative_to(lake).as_posix();sha=hashlib.sha256(newfile.read_bytes()).hexdigest()
item={'dataset':'daily_bars','path':rel,'sha256':sha,'rows':2,'symbol':'batch-fixture','frequency':'1d','data_version':'v2','start':new.isoformat(),'end':new.isoformat()}
manifest={'schema_version':1,'files':[item],'completed':{'scope':{'dataset':'daily_bars','symbol':'batch-fixture','files':[rel],'coverage':{'partial':False}}}}
(lake/'.ptrade-fetch').mkdir();(lake/'.ptrade-fetch/manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
cache=lake/'meta/adj_factors_cache';cache.mkdir(parents=True)
for s in ['000001_SZ','000002_SZ']:
 pl.DataFrame({'trade_date':[old],'factor':[1.5]},schema={'trade_date':pl.Date,'factor':pl.Float64}).write_parquet(cache/(s+'_hfq.parquet'))
config={'lake':str(lake),'datasets':['daily_bars','adj_factors'],'symbols':['000001.SZ','000002.SZ'],'start':new.isoformat(),'end':new.isoformat()}
resolved=run_reference(config)
assert resolved['status']=='succeeded',resolved
# Verify direct script execution in the SDK interpreter, not only module import.
import subprocess, ptrade_sim.data_reference as connector
config_path=root/'connector-config.json';config_path.write_text(json.dumps(config),encoding='utf-8')
cli=subprocess.run([sys.executable,connector.__file__,'--config',str(config_path),'--stage','resolve'],capture_output=True,text=True,encoding='utf-8',check=True)
cli_report=json.loads(cli.stdout)
assert cli_report['status']=='succeeded',cli_report
assert not any(item['code']=='legacy_without_sdk' for item in cli_report['warnings']),cli_report
assert str(oldfile) not in resolved['dataset_roots']['daily_bars']
assert str(newfile) in resolved['dataset_roots']['daily_bars']
result=run_reference(config,'after',work_dir=work)
assert result['status']=='succeeded',result
assert result['counts']['native_daily_keys']==2
assert result['counts']['covered_native_hfq_keys']==2
factor=pl.concat([pl.read_parquet(p) for p in result['dataset_roots']['adj_factors']])
assert set(factor.filter(pl.col('trade_date')==new)['symbol'])=={'000001.SZ','000002.SZ'}
assert all(p.read_bytes()==active_before[str(p)] for p in active_files)
assert pointer.read_bytes()==pointer_before
merged=pl.concat([pl.read_parquet(p['path']) for p in result['view_inputs'] if p['dataset']=='daily_bars'])
assert set(merged.filter(pl.col('trade_date')==old)['close'])=={10.},merged
print(json.dumps({'status':'succeeded','native_keys':2,'factor_rows':factor.height,'generation_unchanged':True,'pointer_unchanged':True,'new_factor_files':result['dataset_roots']['adj_factors']}))
"""


def test_real_sdk_active_revision_and_native_overlay_derive_new_hfq_keys(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    sdk_python = repository.parent / "cne_lake" / ".venv" / "Scripts" / "python.exe"
    if not sdk_python.exists():
        pytest.skip("CNE SDK interpreter is not installed for the real-SDK holdout")
    env = dict(
        os.environ,
        PYTHONPATH=str(repository / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
    )
    # The SDK interpreter cannot inherit the pytest Temp directory's private
    # Windows permissions; use the task-authorized independent work tree.
    work_root = repository.parent / "work"
    work_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reference-sdk-fixture-", dir=work_root) as fixture:
        result = subprocess.run(
            [str(sdk_python), "-c", REAL_SDK_CHECK, fixture],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=90,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["native_keys"] == 2
    assert summary["factor_rows"] >= 2
    assert summary["generation_unchanged"] is True and summary["pointer_unchanged"] is True


@pytest.mark.parametrize("destination", ["lake-json", "config", "wrong-suffix"])
def test_cli_report_path_cannot_overwrite_source_lake_config_or_non_json(
    tmp_path, capsys, destination
):
    lake = tmp_path / "lake"
    lake.mkdir()
    source = _file(lake, "curated/daily_bars/part-0.parquet")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"lake": str(lake), "datasets": ["daily_bars"]}), encoding="utf-8")
    report = (
        source
        if destination == "wrong-suffix"
        else (config if destination == "config" else lake / "existing.json")
    )
    if destination == "lake-json":
        report.write_text("existing", encoding="utf-8")
    before = report.read_bytes()
    assert R.main(["--config", str(config), "--stage", "resolve", "--report", str(report)]) != 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["errors"][0]["code"] == "output_path_invalid"
    assert report.read_bytes() == before


def test_sdk_unexpected_failure_is_not_succeeded_or_legacy_fallback(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("not a usable factor view")

    monkeypatch.setattr(R, "refresh_after", broken)
    report = R.run_reference({"lake": str(tmp_path)}, "after", work_dir=tmp_path.parent / "work")
    assert report["status"] == "failed"
    assert report["errors"][0]["code"] == "connector_failed"
    assert report["dataset_roots"] == {}
