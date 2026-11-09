use crate::model::{Result, StoreError};
use crate::paths;
use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct NativeFile {
    pub path: PathBuf,
    pub sha256: String,
    #[serde(default)]
    pub rows: Option<u64>,
}

pub const DATASETS: &[&str] = &[
    "daily_bars",
    "index_bars",
    "instruments",
    "trading_calendar",
    "minute_bars",
    "adj_factors",
    "index_constituents",
    "trading_status",
];
fn default_datasets() -> Vec<String> {
    DATASETS.iter().map(|s| (*s).to_owned()).collect()
}
fn default_threads() -> u32 {
    2
}
fn default_memory() -> MemoryLimit {
    MemoryLimit::Text("1GB".into())
}
fn default_true() -> bool {
    true
}
fn default_unit_factor() -> f64 {
    2.0
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum MemoryLimit {
    Text(String),
    Bytes(u64),
}
impl MemoryLimit {
    pub fn value(&self) -> String {
        match self {
            Self::Text(s) => s.clone(),
            Self::Bytes(n) => format!("{n}B"),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct JobConfig {
    #[serde(default)]
    pub lake: PathBuf,
    #[serde(default)]
    pub db: PathBuf,
    #[serde(default)]
    pub start: Option<String>,
    #[serde(default)]
    pub end: Option<String>,
    #[serde(default)]
    pub symbols: Vec<String>,
    #[serde(default)]
    pub dataset_roots: BTreeMap<String, Vec<PathBuf>>,
    #[serde(default)]
    pub native_files: BTreeMap<String, Vec<NativeFile>>,
    #[serde(default = "default_datasets")]
    pub datasets: Vec<String>,
    #[serde(default = "default_threads")]
    pub threads: u32,
    #[serde(default = "default_memory")]
    pub memory_limit: MemoryLimit,
    #[serde(default = "default_true")]
    pub require_volume_v2: bool,
    #[serde(default = "default_unit_factor")]
    pub unit_fault_factor: f64,
    #[serde(default)]
    pub strict_unknowns: bool,
}
impl JobConfig {
    pub fn validate(&mut self, operation: &str) -> Result<()> {
        if self.db.as_os_str().is_empty() {
            return Err(StoreError::config("db is required"));
        }
        if operation != "verify" && !self.lake.is_dir() {
            return Err(StoreError::config(
                "lake must be an existing Parquet lake directory",
            ));
        }
        if self.db.is_dir() {
            return Err(StoreError::config("db must be a file, not a directory"));
        }
        if self.threads == 0 {
            return Err(StoreError::config("threads must be positive"));
        }
        if !self.unit_fault_factor.is_finite() || self.unit_fault_factor < 1.0 {
            return Err(StoreError::config(
                "unit_fault_factor must be finite and >= 1",
            ));
        }
        let memory = self.memory_limit.value();
        if memory.trim().is_empty()
            || !memory
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == ' ')
        {
            return Err(StoreError::config("invalid memory_limit"));
        }
        for value in [&mut self.start, &mut self.end].into_iter().flatten() {
            let date = NaiveDate::parse_from_str(value, "%Y%m%d")
                .or_else(|_| NaiveDate::parse_from_str(value, "%Y-%m-%d"))
                .map_err(|_| StoreError::config(format!("invalid date {value:?}")))?;
            *value = date.format("%Y%m%d").to_string();
        }
        if let (Some(start), Some(end)) = (&self.start, &self.end) {
            if start > end {
                return Err(StoreError::config("start is after end"));
            }
        }
        for symbol in &mut self.symbols {
            *symbol = normalize_symbol(symbol)?;
        }
        self.symbols.sort();
        self.symbols.dedup();
        for dataset in &mut self.datasets {
            *dataset = dataset.trim().to_lowercase();
            if !DATASETS.contains(&dataset.as_str()) && dataset != "minute_bars_5m" {
                return Err(StoreError::config(format!(
                    "unsupported dataset {dataset:?}"
                )));
            }
        }
        self.datasets.sort();
        self.datasets.dedup();
        if self.datasets.is_empty() {
            return Err(StoreError::config("datasets cannot be empty"));
        }
        // A CNE 2.0 source database must never accidentally become the writer's
        // target. Source access is exclusively via curated/derived Parquet.
        if self.lake.is_dir() && paths::is_within(&self.db, &self.lake)? {
            return Err(StoreError::config("db must be outside the source lake"));
        }
        Ok(())
    }
    pub fn wants(&self, name: &str) -> bool {
        self.datasets.iter().any(|s| s == name)
    }
    pub fn scope_hash(&self) -> String {
        let data = serde_json::json!({"writer_schema":1,"start":self.start,"end":self.end,
            "symbols":self.symbols,"datasets":self.datasets,"volume_v2":self.require_volume_v2,
            "unit_factor":self.unit_fault_factor,"strict_unknowns":self.strict_unknowns,"dataset_roots":self.dataset_roots,"native_files":self.native_files});
        format!("{:x}", Sha256::digest(data.to_string().as_bytes()))
    }
}

pub fn normalize_symbol(value: &str) -> Result<String> {
    let value = value.trim().to_uppercase();
    let Some((digits, exchange)) = value.split_once('.') else {
        return Err(StoreError::config(format!("invalid symbol {value:?}")));
    };
    if digits.len() != 6
        || !digits.bytes().all(|b| b.is_ascii_digit())
        || !["SH", "SS", "SZ", "BJ"].contains(&exchange)
    {
        return Err(StoreError::config(format!("invalid symbol {value:?}")));
    }
    Ok(format!(
        "{digits}.{}",
        if exchange == "SH" { "SS" } else { exchange }
    ))
}
