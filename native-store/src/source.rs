use crate::config::{JobConfig, DATASETS};
use crate::model::{Cancellation, FileReport, Finding, Report, Result, StoreError};
use crate::sql;
use duckdb::Connection;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

pub struct SourceFile {
    pub path: PathBuf,
    pub printable: String,
    pub digest: String,
    pub bytes: u64,
    pub changed: bool,
}
pub struct Source {
    pub name: String,
    pub files: Vec<SourceFile>,
    pub columns: BTreeSet<String>,
}
pub type Sources = BTreeMap<String, Source>;
pub fn digest_file(path: &Path) -> Result<(String, u64)> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut total = 0;
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        digest.update(&buffer[..n]);
        total += n as u64;
    }
    Ok((format!("{:x}", digest.finalize()), total))
}
fn files_for(cfg: &JobConfig, dataset: &str) -> Result<Vec<PathBuf>> {
    let mut files = vec![];
    // Explicit roots are authoritative, including empty lists. Never fall back
    // from an SDK committed generation to abandoned mutable curated history.
    let roots = cfg.dataset_roots.get(dataset).cloned().unwrap_or_else(|| {
        vec![
            cfg.lake.join("curated").join(dataset),
            cfg.lake.join("derived").join(dataset),
        ]
    });
    for directory in roots {
        if directory.is_file() {
            if !directory
                .extension()
                .is_some_and(|e| e.eq_ignore_ascii_case("parquet"))
            {
                return Err(
                    StoreError::config("dataset root file must be Parquet").dataset(dataset)
                );
            }
            // Explicit SDK generation files are not native overlays. A
            // dataset may mix SDK files with native files, or carry an empty
            // native_files list. Matching native items are SHA-checked below;
            // do not require SDK files to be mislabeled as native evidence.
            files.push(directory);
            continue;
        }
        if !directory.is_dir() {
            continue;
        }
        for entry in WalkDir::new(directory).follow_links(false) {
            let entry = entry.map_err(|e| StoreError::new("io", e.to_string(), 1))?;
            if entry.file_type().is_file()
                && entry
                    .path()
                    .extension()
                    .is_some_and(|e| e.eq_ignore_ascii_case("parquet"))
            {
                files.push(entry.into_path());
            }
        }
    }
    files.sort();
    files.dedup();
    Ok(files)
}
pub fn discover(
    con: &Connection,
    cfg: &JobConfig,
    report: &mut Report,
    cancel: &Cancellation,
) -> Result<Sources> {
    let mut load: BTreeSet<String> = cfg.datasets.iter().cloned().collect();
    if cfg.wants("daily_bars")
        || cfg.wants("minute_bars")
        || cfg.wants("adj_factors")
        || cfg.wants("trading_status")
    {
        for name in ["instruments", "adj_factors", "trading_status", "daily_bars"] {
            load.insert(name.into());
        }
    }
    let has_ledger = sql::exists(con, "_native_store_files")?;
    let scope = cfg.scope_hash();
    let mut result = Sources::new();
    for name in load {
        cancel.check()?;
        let paths = files_for(cfg, &name)?;
        if paths.is_empty() {
            let required = matches!(
                name.as_str(),
                "daily_bars" | "index_bars" | "minute_bars" | "instruments" | "trading_calendar"
            ) && cfg.wants(&name);
            if required {
                return Err(
                    StoreError::data("required dataset has no Parquet files").dataset(&name)
                );
            }
            if cfg.wants(&name) {
                report.unknown(Finding::new(
                    "missing_optional_dataset",
                    "no source facts available; existing target information is preserved",
                    Some(&name),
                    None,
                    None,
                ));
            }
            continue;
        }
        let mut files = vec![];
        for path in paths {
            cancel.check()?;
            let (digest, bytes) = digest_file(&path)?;
            if let Some(evidence) = cfg.native_files.get(&name).and_then(|items| {
                items
                    .iter()
                    .find(|e| e.path.canonicalize().ok() == path.canonicalize().ok())
            }) {
                if evidence.sha256.to_lowercase() != digest {
                    return Err(StoreError::data(
                        "native overlay SHA256 differs from verified manifest",
                    )
                    .dataset(&name));
                }
            }
            let printable = path.to_string_lossy().replace('\\', "/");
            let previous = if has_ledger {
                sql::texts(con,&format!("SELECT sha256 FROM _native_store_files WHERE dataset={} AND path={} AND scope={}",sql::lit(&name),sql::lit(&printable),sql::lit(&scope)))?.into_iter().next()
            } else {
                None
            };
            let changed = previous.as_ref() != Some(&digest);
            report.files.push(FileReport {
                dataset: name.clone(),
                path: printable.clone(),
                bytes,
                sha256: digest.clone(),
                state: if changed {
                    "new_or_changed"
                } else {
                    "previously_committed"
                }
                .into(),
            });
            files.push(SourceFile {
                path,
                printable,
                digest,
                bytes,
                changed,
            });
        }
        result.insert(
            name.clone(),
            Source {
                name,
                files,
                columns: BTreeSet::new(),
            },
        );
    }
    report.counts.insert(
        "files_changed".into(),
        result
            .values()
            .flat_map(|s| &s.files)
            .filter(|f| f.changed)
            .count() as u64,
    );
    report.counts.insert(
        "input_bytes".into(),
        result
            .values()
            .flat_map(|s| &s.files)
            .map(|f| f.bytes)
            .sum(),
    );
    Ok(result)
}
pub fn raw(name: &str) -> String {
    format!("__raw_{name}")
}
pub fn frame(name: &str) -> String {
    format!("__src_{name}")
}
impl Source {
    pub fn optional_at(&self, column: &str, ty: &str, alias: &str) -> String {
        if self.columns.contains(column) {
            format!("TRY_CAST({alias}.{} AS {ty})", sql::ident(column))
        } else {
            format!("NULL::{ty}")
        }
    }
    pub fn optional(&self, column: &str, ty: &str) -> String {
        if self.columns.contains(column) {
            format!("TRY_CAST({} AS {ty})", sql::ident(column))
        } else {
            format!("NULL::{ty}")
        }
    }
}
fn required_columns(name: &str) -> Vec<&'static str> {
    let mut result = vec!["source", "data_version", "fetched_at"];
    match name {
        "daily_bars" | "index_bars" => result.extend([
            "symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]),
        "minute_bars" | "minute_bars_5m" => result.extend([
            "symbol",
            "trade_date",
            "bar_time",
            "frequency",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]),
        "instruments" => result.extend(["symbol", "name", "exchange", "asset_type"]),
        "trading_calendar" => result.extend(["trade_date", "is_trading"]),
        "trading_status" => result.extend(["symbol", "trade_date", "status", "is_trading"]),
        "adj_factors" => result.extend(["symbol", "trade_date", "adjust_type", "factor"]),
        "index_constituents" => result.extend(["index_symbol", "symbol", "as_of_date", "weight"]),
        _ => {}
    }
    result
}
pub fn materialize(
    con: &Connection,
    cfg: &JobConfig,
    sources: &mut Sources,
    report: &mut Report,
    cancel: &Cancellation,
) -> Result<()> {
    let mut total = 0;
    for source in sources.values_mut() {
        cancel.check()?;
        let input = source
            .files
            .iter()
            .map(|f| sql::lit(&f.printable))
            .collect::<Vec<_>>()
            .join(",");
        // Never infer a date from trade_date=YYYY-MM directories. Physical DATE
        // wins, including files filed under an incorrect month directory.
        con.execute_batch(&format!("CREATE OR REPLACE TEMP TABLE {} AS SELECT * FROM read_parquet([{input}],union_by_name=true,hive_partitioning=false,filename=true,file_row_number=true)",sql::ident(&raw(&source.name))))?;
        source.columns = sql::columns(con, &raw(&source.name))?;
        for column in required_columns(&source.name) {
            if !source.columns.contains(column) {
                return Err(
                    StoreError::schema(format!("missing source column {column}"))
                        .dataset(&source.name),
                );
            }
        }
        let count = sql::count(
            con,
            &format!("SELECT count(*) FROM {}", sql::ident(&raw(&source.name))),
        )?;
        total += count;
        let day_column = if source.name == "index_constituents" {
            "as_of_date"
        } else {
            "trade_date"
        };
        let date = if source.columns.contains(day_column) {
            sql::date8(&sql::ident(day_column))
        } else {
            "NULL::VARCHAR".into()
        };
        let symbol = if source.columns.contains("symbol") {
            sql::code("symbol")
        } else {
            "NULL::VARCHAR".into()
        };
        con.execute_batch(&format!("CREATE OR REPLACE TEMP VIEW {} AS SELECT *,{date} AS __date,{symbol} AS __code,epoch_us(fetched_at)::BIGINT AS __fetch FROM {}",sql::ident(&frame(&source.name)),sql::ident(&raw(&source.name))))?;
        let symbol_scope = if source.columns.contains("symbol") {
            sql::symbol_filter(cfg, "__code")
        } else {
            "TRUE".into()
        };
        let relation = sql::ident(&frame(&source.name));
        let invalid=sql::count(con,&format!("SELECT count(*) FROM {relation} WHERE {symbol_scope} AND (__fetch IS NULL OR source IS NULL OR lower(source)='mock' {})",if source.columns.contains(day_column) {"OR __date IS NULL"} else {""}))?;
        if invalid > 0 {
            return Err(StoreError::data(format!("{invalid} invalid date/provenance/mock-source rows; Hive month labels are not dates")).dataset(&source.name));
        }
        if source.name.starts_with("minute_bars") {
            let expected = if source.name == "minute_bars_5m" {
                "5m"
            } else {
                "1m"
            };
            let invalid=sql::count(con,&format!("SELECT count(*) FROM {relation} WHERE {symbol_scope} AND frequency IS DISTINCT FROM {}",sql::lit(expected)))?;
            if invalid > 0 {
                return Err(StoreError::data(format!(
                    "{invalid} wrong-frequency rows in {expected} dataset"
                ))
                .dataset(&source.name));
            }
            let kinds = sql::texts(
                con,
                &format!("SELECT DISTINCT typeof(bar_time) FROM {relation}"),
            )?;
            if kinds.iter().any(|t| t.contains("WITH TIME ZONE")) {
                return Err(StoreError::schema(
                    "bar_time must be naive Asia/Shanghai, not tz-aware",
                )
                .dataset(&source.name));
            }
        }
        if source.name == "index_bars" && source.columns.contains("frequency") {
            let invalid=sql::count(con,&format!("SELECT count(*) FROM {relation} WHERE {symbol_scope} AND frequency IS DISTINCT FROM '1d'"))?;
            if invalid > 0 {
                return Err(StoreError::data(
                    "index_bars is daily; index intraday requires minute_bars with a bar_time",
                )
                .dataset(&source.name));
            }
        }
        let selected = if source.columns.contains(day_column) {
            format!("{} AND {symbol_scope}", sql::bounds(cfg, "__date", false))
        } else {
            symbol_scope
        };
        let dates = if source.columns.contains(day_column) {
            sql::texts(con,&format!("SELECT coalesce(min(__date),'')||'|'||coalesce(max(__date),'') FROM {relation} WHERE {selected}"))?.into_iter().next().unwrap_or_default()
        } else {
            "|".into()
        };
        let (min, max) = dates.split_once('|').unwrap_or(("", ""));
        let selected_count = sql::count(
            con,
            &format!("SELECT count(*) FROM {relation} WHERE {selected}"),
        )?;
        let frequencies = if source.columns.contains("frequency") {
            sql::texts(
                con,
                &format!(
                    "SELECT DISTINCT frequency FROM {relation} WHERE {selected} ORDER BY frequency"
                ),
            )?
        } else {
            vec![]
        };
        let symbols = if source.columns.contains("symbol") {
            sql::texts(
                con,
                &format!("SELECT DISTINCT __code FROM {relation} WHERE {selected} ORDER BY __code"),
            )?
        } else {
            vec![]
        };
        report.coverage.push(serde_json::json!({"dataset":source.name,"rows":selected_count,"min_date":if min.is_empty(){None}else{Some(min)},"max_date":if max.is_empty(){None}else{Some(max)},"symbols":symbols,"frequencies":frequencies,"requested":cfg.wants(&source.name),"status":if selected_count==0&&cfg.wants(&source.name){"partial"}else{"observed"}}));
    }
    report.counts.insert("rows_observed_input".into(), total);
    Ok(())
}
pub fn ensure_stable(sources: &Sources, cancel: &Cancellation) -> Result<()> {
    for source in sources.values() {
        for file in &source.files {
            cancel.check()?;
            let (digest, bytes) = digest_file(&file.path)?;
            if digest != file.digest || bytes != file.bytes {
                return Err(StoreError::data(
                    "input Parquet changed while transaction was staging",
                )
                .dataset(&source.name));
            }
        }
    }
    Ok(())
}
pub fn commit_files(con: &Connection, cfg: &JobConfig, sources: &Sources) -> Result<()> {
    let scope = cfg.scope_hash();
    for source in sources.values() {
        for file in &source.files {
            con.execute_batch(&format!("INSERT INTO _native_store_files VALUES ({},{},{},{},{},current_timestamp) ON CONFLICT(dataset,path,scope) DO UPDATE SET sha256=excluded.sha256,bytes=excluded.bytes,committed_at=excluded.committed_at",sql::lit(&source.name),sql::lit(&file.printable),sql::lit(&scope),sql::lit(&file.digest),file.bytes))?;
        }
    }
    Ok(())
}
pub fn changed(sources: &Sources, name: &str) -> bool {
    sources
        .get(name)
        .is_some_and(|s| s.files.iter().any(|f| f.changed))
}
pub fn capabilities() -> serde_json::Value {
    serde_json::json!({"backend":"rust-duckdb-c-api","required_engine":"v1.5.6","datasets":DATASETS,"extra_dataset":"minute_bars_5m (observe-only; no 5m physical contract)","source_storage":"Parquet only; never source DuckDB","writer":"single-writer transaction, key upsert","progress":"atomic JSON snapshot"})
}
