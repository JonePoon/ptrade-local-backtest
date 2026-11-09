"""Explicit CNE SDK connector: committed roots + verified Rust overlay + isolated hfq.

The file is directly executable in the CNE interpreter. It never launches a bar
fetcher, modifies an original generation/pointer, or imports the simulator engine.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

DATASETS = {
    "daily_bars": "curated",
    "index_bars": "curated",
    "minute_bars": "curated",
    "minute_bars_5m": "curated",
    "instruments": "curated",
    "trading_calendar": "curated",
    "trading_status": "curated",
    "index_constituents": "curated",
    "corporate_actions": "curated",
    "share_structure": "curated",
    "adj_factors": "derived",
}
BAR_DATASETS = {"daily_bars", "index_bars", "minute_bars", "minute_bars_5m"}
DEFAULT_DATASETS = [
    "daily_bars",
    "index_bars",
    "minute_bars",
    "minute_bars_5m",
    "instruments",
    "trading_calendar",
    "trading_status",
    "index_constituents",
    "adj_factors",
]
FACTOR_INPUTS = [
    "daily_bars",
    "corporate_actions",
    "instruments",
    "share_structure",
    "trading_calendar",
    "adj_factors",
]
NATIVE_NAME = re.compile(r"part-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.parquet$")
SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)$")


class ReferenceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ReferenceError("invalid_json", f"Invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReferenceError("invalid_json", f"JSON object required: {path}")
    return value


def _inside(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    try:
        result = path.resolve(strict=must_exist)
    except OSError as exc:
        raise ReferenceError("input_missing", f"Unavailable input {path}: {exc}") from exc
    if not result.is_relative_to(root.resolve()):
        raise ReferenceError("path_escape", f"Path outside allowed root: {path}")
    if must_exist and not result.is_file():
        raise ReferenceError("input_type", f"Regular file required: {path}")
    return result


def _relative(path: str, root: Path) -> Path:
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(p in {".", ".."} or ":" in p for p in pure.parts)
    ):
        raise ReferenceError("path_escape", "Native manifest path must be safe relative path")
    return _inside(root.joinpath(*pure.parts), root)


def _digest(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ReferenceError("input_changed", f"Input changed while hashing: {path}")
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".reference-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_sdk(required: bool = False):
    try:
        config = importlib.import_module("cnequity.config")
        datasets = importlib.import_module("cnequity.domain.datasets")
        locator = importlib.import_module("cnequity.storage.locator")
        revisions = importlib.import_module("cnequity.storage.revisions")
        return SimpleNamespace(
            Config=config.Config,
            load_config=config.load_config,
            registry=datasets.DATASETS,
            logical_root=locator.logical_root,
            resolve_root=revisions.resolve_committed_root,
            committed_revision=revisions.committed_revision,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "cnequity":
            raise ReferenceError(
                "sdk_import_failed", "Installed SDK or its dependencies are incomplete"
            ) from exc
        if required:
            raise ReferenceError(
                "sdk_unavailable", "CNE SDK interpreter is required for committed revisions/after"
            ) from exc
        return None
    except ImportError as exc:
        raise ReferenceError("sdk_import_failed", "Installed SDK API import failed") from exc


def _options(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ReferenceError("config_error", "Config must be an object")
    value = dict(config)
    if not value.get("lake"):
        raise ReferenceError("config_error", "lake is required")
    value["lake"] = Path(value["lake"]).expanduser().resolve()
    value["datasets"] = value.get("datasets", DEFAULT_DATASETS)
    if not isinstance(value["datasets"], list) or any(d not in DATASETS for d in value["datasets"]):
        raise ReferenceError("config_error", "Unsupported dataset or non-list datasets")
    value["datasets"] = list(dict.fromkeys(value["datasets"]))
    symbols = value.get("symbols") or []
    if not isinstance(symbols, list) or any(not isinstance(s, str) for s in symbols):
        raise ReferenceError("config_error", "symbols must be a list of canonical strings")
    value["symbols"] = sorted({s.upper().replace(".SS", ".SH") for s in symbols})
    if any(not SYMBOL.fullmatch(s) for s in value["symbols"]):
        raise ReferenceError("config_error", "Invalid symbol")
    for key in ("start", "end"):
        if value.get(key):
            try:
                value[key] = dt.date.fromisoformat(str(value[key]))
            except ValueError as exc:
                raise ReferenceError("config_error", f"Invalid {key} date") from exc
    if value.get("start") and value.get("end") and value["start"] > value["end"]:
        raise ReferenceError("config_error", "start exceeds end")
    return value


def _base_report(stage: str) -> dict:
    return {
        "schema_version": 1,
        "operation": "reference_" + stage,
        "status": "succeeded",
        "dataset_roots": {},
        "native_files": {},
        "sources": [],
        "roots": [],
        "files": [],
        "counts": {},
        "coverage": [],
        "errors": [],
        "warnings": [],
        "timings": {},
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def _native_manifest(lake: Path) -> tuple[dict[str, list[dict]], set[Path]]:
    manifest_path = lake / ".ptrade-fetch" / "manifest.json"
    if not manifest_path.exists():
        return {}, set()
    manifest_path = _inside(manifest_path, lake)
    manifest = _read_json(manifest_path)
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("files"), list)
        or not isinstance(manifest.get("completed"), dict)
    ):
        raise ReferenceError("native_manifest_invalid", "Unsupported/incomplete native manifest")
    by_relative, known = {}, set()
    for record in manifest["files"]:
        if not isinstance(record, dict) or record.get("dataset") not in BAR_DATASETS:
            raise ReferenceError("native_manifest_invalid", "Invalid native file record")
        relative = record.get("path")
        if not isinstance(relative, str) or relative in by_relative:
            raise ReferenceError("native_manifest_invalid", "Duplicate or invalid native path")
        path = _relative(relative, lake)
        if path.suffix.lower() != ".parquet":
            raise ReferenceError("native_manifest_invalid", "Native overlay must be Parquet")
        expected = record.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected.lower()):
            raise ReferenceError("native_manifest_invalid", "Native SHA256 missing/invalid")
        if _digest(path) != expected.lower():
            raise ReferenceError("native_digest_mismatch", f"Native SHA256 differs: {relative}")
        if type(record.get("rows")) is not int or record["rows"] <= 0:
            raise ReferenceError("native_manifest_invalid", "Native rowcount must be positive")
        item = {
            k: record[k]
            for k in (
                "dataset",
                "sha256",
                "rows",
                "symbol",
                "frequency",
                "data_version",
                "volume_unit",
                "fetched_at",
                "start",
                "end",
            )
            if k in record
        }
        item["path"] = str(path)
        by_relative[relative] = item
        known.add(path)
    result: dict[str, list[dict]] = {}
    seen = set()
    for commit in manifest["completed"].values():
        if (
            not isinstance(commit, dict)
            or commit.get("dataset") not in BAR_DATASETS
            or not isinstance(commit.get("files"), list)
        ):
            raise ReferenceError("native_manifest_invalid", "Invalid native completed scope")
        if (
            not isinstance(commit.get("coverage"), dict)
            or commit["coverage"].get("partial") is not False
        ):
            raise ReferenceError(
                "native_partial_scope", "Only proven completed native scopes are usable"
            )
        for relative in commit["files"]:
            if relative not in by_relative:
                raise ReferenceError(
                    "native_manifest_invalid", "Completed native scope names an unlisted file"
                )
            item = by_relative[relative]
            if item["dataset"] != commit["dataset"] or item.get("symbol") != commit.get("symbol"):
                raise ReferenceError(
                    "native_manifest_invalid", "Native scope/file identity mismatch"
                )
            if relative not in seen:
                result.setdefault(item["dataset"], []).append(item)
                seen.add(relative)
    return result, known


def _source_files(
    root: Path, lake: Path, *, exclude: set[Path], reject_native_orphans: bool
) -> list[Path]:
    if not root.exists():
        return []
    resolved = root.resolve()
    if not resolved.is_relative_to(lake):
        raise ReferenceError("path_escape", "SDK committed root is outside the configured lake")
    out = []
    for path in sorted(root.rglob("*.parquet")):
        path = _inside(path, lake)
        if path in exclude:
            continue
        if reject_native_orphans and NATIVE_NAME.fullmatch(path.name):
            # The Rust writer has a fixed UUID naming contract. An unlisted
            # candidate is not a committed source; do not turn it into input.
            continue
        out.append(path)
    return out


def resolve_datasets(config: dict, *, sdk=None) -> dict:
    """Read-only exact file selection. Present-but-empty mapping never falls back."""
    options = _options(config)
    report = _base_report("resolve")
    lake = options["lake"]
    if not lake.is_dir():
        raise ReferenceError("input_missing", "Configured lake directory does not exist")
    sdk = sdk if sdk is not None else _load_sdk()
    native, native_known = _native_manifest(lake)
    roots = SimpleNamespace(curated_root=lake / "curated", derived_root=lake / "derived")
    for dataset in options["datasets"]:
        pointer = lake / "meta" / "revisions" / dataset / "current.json"
        if sdk is None:
            if pointer.exists():
                raise ReferenceError(
                    "sdk_unavailable",
                    f"Committed pointer exists for {dataset}; mutable fallback is forbidden",
                )
            logical = lake / DATASETS[dataset] / dataset
            current, revision = logical, None
        else:
            if dataset not in sdk.registry:
                raise ReferenceError(
                    "sdk_contract_unknown", f"Dataset not registered by installed SDK: {dataset}"
                )
            logical = Path(sdk.logical_root(roots, dataset))
            try:
                current = Path(sdk.resolve_root(logical, dataset=dataset, meta_root=lake / "meta"))
                revision = sdk.committed_revision(logical, dataset=dataset, meta_root=lake / "meta")
            except Exception as exc:
                raise ReferenceError(
                    "sdk_revision_invalid",
                    f"Committed revision resolution failed for {dataset}: {exc}",
                ) from exc
        committed = current.resolve() != logical.resolve()
        source_paths = _source_files(
            current,
            lake,
            exclude=set() if committed else native_known,
            reject_native_orphans=not committed and bool(native_known),
        )
        proofs = [
            {
                "dataset": dataset,
                "path": str(path),
                "sha256": _digest(path),
                "kind": "sdk_committed" if committed else "legacy",
            }
            for path in source_paths
        ]
        overlays = sorted(native.get(dataset, []), key=lambda item: item["path"])
        # Pin files, not mutable root directories, so a concurrent writer cannot
        # smuggle an unlisted shard into the subsequent Rust scan.
        selected = list(dict.fromkeys([p["path"] for p in proofs] + [p["path"] for p in overlays]))
        report["dataset_roots"][dataset] = selected
        report["native_files"][dataset] = overlays
        report["sources"].extend(proofs)
        report["sources"].extend({**item, "kind": "native"} for item in overlays)
        selected_proofs = {p["path"]: p["sha256"] for p in [*proofs, *overlays]}
        digest = hashlib.sha256(
            json.dumps(sorted(selected_proofs.items()), separators=(",", ":")).encode()
        ).hexdigest()
        report["roots"].append(
            {
                "dataset": dataset,
                "path": str(current.resolve()),
                "logical_path": str(logical.resolve()),
                "committed": committed,
                "revision": revision,
                "files": len(selected_proofs),
                "base_files": len(proofs),
                "native_files": len(overlays),
                "sha256": digest,
                "pointer": str(pointer) if pointer.exists() else None,
            }
        )
        if not selected:
            report["warnings"].append(
                {
                    "code": "dataset_absent",
                    "dataset": dataset,
                    "message": "No selected input files; no mutable fallback",
                }
            )
    if sdk is None:
        report["warnings"].append(
            {
                "code": "legacy_without_sdk",
                "message": "No revision pointer: legacy paths and native digests selected; SDK schema validation not performed",
            }
        )
    report["counts"] = {
        "datasets": len(report["dataset_roots"]),
        "files": len(report["sources"]),
        "native_files": sum(map(len, report["native_files"].values())),
        "network_requests": 0,
    }
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return report


def _sdk_config(options: dict, sdk):
    refresh = options.get("reference_refresh") or {}
    path = options.get("sdk_config") or refresh.get("config")
    if path:
        config = sdk.load_config(Path(path))
        return dataclasses.replace(
            config, data_root=options["lake"], duckdb_path=None, _rate_limiters=None
        )
    return sdk.Config(data_root=options["lake"])


def _merge_input(
    dataset: str, paths: list[str], output: Path, options: dict, sdk
) -> tuple[int, list[dict]]:
    import polars as pl
    from cnequity.domain.canonical import dedupe_lazy_by_primary_key
    from cnequity.domain.schemas import DATASET_SCHEMAS, validate_dataframe

    if dataset not in DATASET_SCHEMAS:
        raise ReferenceError("sdk_contract_unknown", f"Unknown SDK schema: {dataset}")
    if not paths:
        return 0, []
    scans = [pl.scan_parquet(path, hive_partitioning=False) for path in paths]
    frame = pl.concat(scans, how="diagonal_relaxed")
    columns = frame.collect_schema().names()
    if options["symbols"] and "symbol" in columns:
        frame = frame.filter(pl.col("symbol").is_in(options["symbols"]))
    if options.get("end"):
        for date_col in ("trade_date", "ex_date", "as_of_date"):
            if date_col in columns:
                frame = frame.filter(pl.col(date_col) <= options["end"])
                break
    # Do not cut left-edge history: sparse hfq alignment/events need prior data.
    frame = dedupe_lazy_by_primary_key(frame, dataset).collect(engine="streaming")
    frame = validate_dataframe(frame, dataset, allow_missing_optional=True)
    if frame.is_empty():
        return 0, []
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output, compression="zstd")
    keys = frame.select("symbol", "trade_date").to_dicts() if dataset == "daily_bars" else []
    return frame.height, keys


def _keys_from_native(report: dict, options: dict) -> set[tuple[str, dt.date]]:
    import polars as pl

    keys = set()
    for item in report["native_files"].get("daily_bars", []):
        frame = pl.read_parquet(item["path"], columns=["symbol", "trade_date"])
        for row in frame.iter_rows(named=True):
            if options["symbols"] and row["symbol"] not in options["symbols"]:
                continue
            date = row["trade_date"]
            if (options.get("start") and date < options["start"]) or (
                options.get("end") and date > options["end"]
            ):
                continue
            keys.add((row["symbol"], date))
    return keys


def refresh_after(config: dict, *, work_dir: str | Path | None = None, sdk=None) -> dict:
    """Compute hfq on an isolated merged view; return only verified factor files."""
    options = _options(config)
    sdk = sdk if sdk is not None else _load_sdk(required=True)
    work = work_dir or options.get("reference_work_dir")
    if work is None:
        raise ReferenceError(
            "config_error", "after requires an explicit independent work directory"
        )
    work = Path(work).expanduser().resolve()
    lake = options["lake"]
    if work.is_relative_to(lake) or lake.is_relative_to(work):
        raise ReferenceError(
            "path_escape", "After working directory must be separate from input lake"
        )
    selected = dict(config)
    selected["datasets"] = list(dict.fromkeys(options["datasets"] + FACTOR_INPUTS))
    report = resolve_datasets(selected, sdk=sdk)
    report["operation"] = "reference_after"
    native_keys = _keys_from_native(report, options)
    if not native_keys:
        report["warnings"].append(
            {
                "code": "no_native_daily_keys",
                "message": "No verified native daily keys in requested scope; hfq was not refreshed",
            }
        )
        report["coverage"].append({"dataset": "adj_factors", "native_keys": 0, "derived": False})
        return report
    work.mkdir(parents=True, exist_ok=True)
    view = Path(tempfile.mkdtemp(prefix="cne-merged-", dir=work))
    sdk_config = _sdk_config(options, sdk)
    view_config = dataclasses.replace(
        sdk_config,
        data_root=view,
        duckdb_path=None,
        config_path=None,
        _rate_limiters=None,
        revision_keep_generations=0,
    )
    view_proofs = []
    merged_keys: set[tuple[str, dt.date]] = set()
    for dataset in FACTOR_INPUTS:
        layer = DATASETS[dataset]
        output = view / layer / dataset / "part-connector.parquet"
        count, keys = _merge_input(
            dataset, report["dataset_roots"].get(dataset, []), output, options, sdk
        )
        if count:
            view_proofs.append(
                {"dataset": dataset, "path": str(output), "sha256": _digest(output), "rows": count}
            )
        merged_keys.update((r["symbol"], r["trade_date"]) for r in keys)
    if not native_keys <= merged_keys:
        raise ReferenceError(
            "overlay_not_visible", "Merged SDK reader inputs do not contain all native keys"
        )
    # Isolated copies, never hardlinks: unexpected SDK writes cannot mutate old
    # immutable generations or shared cache inodes in the original lake.
    symbols = sorted({symbol for symbol, _ in native_keys})
    for symbol in symbols:
        cached = lake / "meta" / "adj_factors_cache" / (symbol.replace(".", "_") + "_hfq.parquet")
        if cached.exists():
            cached = _inside(cached, lake)
            target = view / "meta" / "adj_factors_cache" / cached.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, target)
    factor_api = importlib.import_module("cnequity.derive.adj_factors")
    try:
        result = factor_api.compute_adj_factors(
            view_config, adjust_type="hfq", realign_symbols=symbols, full=True
        )
    except Exception as exc:
        raise ReferenceError(
            "sdk_derive_failed",
            f"Official compute_adj_factors failed in isolated view: {type(exc).__name__}",
        ) from exc
    if any(finding.get("severity") == "error" for finding in result.findings):
        raise ReferenceError(
            "sdk_factor_findings", "Official factor validation reported error findings"
        )
    factor_root = Path(
        sdk.resolve_root(
            view_config.derived_root / "adj_factors",
            dataset="adj_factors",
            meta_root=view_config.meta_root,
        )
    )
    factor_files = _source_files(factor_root, view, exclude=set(), reject_native_orphans=False)
    if not factor_files:
        raise ReferenceError(
            "factor_output_missing", "Official derive produced no readable factor files"
        )
    import polars as pl

    factor = pl.concat([pl.read_parquet(path) for path in factor_files], how="diagonal_relaxed")
    if not {"symbol", "trade_date", "factor", "adjust_type"}.issubset(factor.columns):
        raise ReferenceError(
            "factor_schema_unknown", "Official factor output is missing required fields"
        )
    usable = factor.filter(
        (pl.col("adjust_type") == "hfq") & pl.col("factor").is_finite() & (pl.col("factor") > 0)
    )
    factor_keys = {
        (r["symbol"], r["trade_date"]) for r in usable.select("symbol", "trade_date").to_dicts()
    }
    missing = sorted(native_keys - factor_keys)
    if missing or result.failed:
        raise ReferenceError(
            "native_factor_coverage_missing",
            f"Native hfq keys not fully covered: missing={len(missing)} failed={len(result.failed)}",
        )
    # Verify every original source again after the SDK work. No public mutable
    # pointer was changed to make the isolated SDK reader see new rows.
    for item in report["sources"]:
        if _digest(Path(item["path"])) != item["sha256"]:
            raise ReferenceError("input_changed", "Original source changed during isolated derive")
    proofs = [
        {
            "dataset": "adj_factors",
            "path": str(path),
            "sha256": _digest(path),
            "rows": pl.read_parquet(path).height,
            "kind": "isolated_sdk_factor",
        }
        for path in factor_files
    ]
    report["dataset_roots"]["adj_factors"] = [p["path"] for p in proofs]
    report["files"] = proofs
    report["sources"].extend(proofs)
    report["merged_view"] = str(view)
    report["view_inputs"] = view_proofs
    report["counts"].update(
        {
            "native_daily_keys": len(native_keys),
            "covered_native_hfq_keys": len(native_keys),
            "factor_rows": factor.height,
            "official_tasks": result.task_count,
            "network_requests": None,
            "bar_downloads": 0,
        }
    )
    report["coverage"].append(
        {
            "dataset": "adj_factors",
            "native_keys": len(native_keys),
            "covered": len(native_keys),
            "missing": 0,
            "derived": True,
            "original_generation_changed": False,
            "input_view": str(view),
        }
    )
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return report


def run_reference(
    config: dict, stage: str = "resolve", *, work_dir: str | Path | None = None
) -> dict:
    started = time.monotonic()
    try:
        if stage == "after":
            report = refresh_after(config, work_dir=work_dir)
        elif stage in {"resolve", "before"}:
            report = resolve_datasets(config)
            report["operation"] = "reference_" + stage
            if stage == "before":
                report["warnings"].append(
                    {
                        "code": "existing_reference_inputs",
                        "message": "Reference refresh is coordinator-owned; connector did not refetch",
                    }
                )
        else:
            raise ReferenceError("config_error", "stage must be before, after or resolve")
    except ReferenceError as exc:
        report = _base_report(stage)
        report["status"] = "failed"
        report["errors"].append({"code": exc.code, "message": str(exc)})
    except Exception as exc:
        # Version-specific SDK/Polars failures remain failures, never a legacy
        # fallback or a false successful derive receipt. Do not echo secrets.
        report = _base_report(stage)
        report["status"] = "failed"
        report["errors"].append({"code": "connector_failed", "message": type(exc).__name__})
    report["timings"] = {"elapsed_seconds": time.monotonic() - started}
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CNE committed roots / native overlay SDK connector"
    )
    parser.add_argument("--lake")
    parser.add_argument("--config")
    parser.add_argument("--stage", choices=("resolve", "before", "after"), default="resolve")
    parser.add_argument("--report")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--work-dir")
    args = parser.parse_args(argv)
    output_report = None
    try:
        config = _read_json(Path(args.config)) if args.config else {}
        if args.config:
            base = Path(args.config).resolve().parent
            for key in ("lake", "sdk_config", "reference_work_dir"):
                if config.get(key) and not Path(config[key]).is_absolute():
                    config[key] = str(base / config[key])
            refresh = config.get("reference_refresh")
            if (
                isinstance(refresh, dict)
                and refresh.get("config")
                and not Path(refresh["config"]).is_absolute()
            ):
                refresh["config"] = str(base / refresh["config"])
        for key in ("lake", "start", "end"):
            value = getattr(args, key)
            if value is not None:
                config[key] = value
        if args.report:
            candidate = Path(args.report).expanduser().resolve()
            if candidate.suffix.lower() != ".json":
                raise ReferenceError("output_path_invalid", "Connector report must be a JSON file")
            if config.get("lake") and candidate.is_relative_to(Path(config["lake"]).resolve()):
                raise ReferenceError(
                    "output_path_invalid", "Report cannot overwrite the input lake or generations"
                )
            if args.config and candidate == Path(args.config).resolve():
                raise ReferenceError(
                    "output_path_invalid", "Report cannot overwrite its input config"
                )
            output_report = candidate
        work = args.work_dir or (str(output_report.parent) if output_report else None)
        # SDK messages go to stderr; stdout remains one final machine-readable JSON.
        with contextlib.redirect_stdout(sys.stderr):
            report = run_reference(config, args.stage, work_dir=work)
    except ReferenceError as exc:
        report = _base_report(args.stage)
        report["status"] = "failed"
        report["errors"].append({"code": exc.code, "message": str(exc)})
    if output_report is not None:
        try:
            _atomic_json(output_report, report)
        except OSError as exc:
            report["status"] = "failed"
            report["errors"].append({"code": "report_write_failed", "message": type(exc).__name__})
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "succeeded" else 3


if __name__ == "__main__":
    # Direct execution must not let sibling queue.py/config.py shadow stdlib
    # imports used by the SDK. Expose the package root, not this module folder.
    module_dir = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != module_dir]
    sys.path.insert(0, str(module_dir.parent))
    raise SystemExit(main())
