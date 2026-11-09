use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::Instant;

pub type Result<T> = std::result::Result<T, StoreError>;
#[derive(Debug, Clone, Serialize)]
pub struct StoreError {
    pub kind: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dataset: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub table: Option<String>,
    #[serde(skip)]
    pub exit_code: i32,
}
impl StoreError {
    pub fn new(kind: &str, message: impl Into<String>, code: i32) -> Self {
        Self {
            kind: kind.into(),
            message: message.into(),
            dataset: None,
            table: None,
            exit_code: code,
        }
    }
    pub fn config(message: impl Into<String>) -> Self {
        Self::new("configuration", message, 2)
    }
    pub fn data(message: impl Into<String>) -> Self {
        Self::new("data", message, 3)
    }
    pub fn schema(message: impl Into<String>) -> Self {
        Self::new("schema", message, 3)
    }
    pub fn dependency(message: impl Into<String>) -> Self {
        Self::new("dependency", message, 6)
    }
    pub fn cancelled() -> Self {
        Self::new(
            "cancelled",
            "cancellation requested; transaction not committed",
            130,
        )
    }
    pub fn dataset(mut self, name: &str) -> Self {
        self.dataset = Some(name.into());
        self
    }
    pub fn table(mut self, name: &str) -> Self {
        self.table = Some(name.into());
        self
    }
}
impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.kind, self.message)
    }
}
impl std::error::Error for StoreError {}
impl From<duckdb::Error> for StoreError {
    fn from(e: duckdb::Error) -> Self {
        Self::new("duckdb", e.to_string(), 1)
    }
}
impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        Self::new("io", e.to_string(), 1)
    }
}
impl From<serde_json::Error> for StoreError {
    fn from(e: serde_json::Error) -> Self {
        Self::config(e.to_string())
    }
}

#[derive(Clone, Default)]
pub struct Cancellation {
    flag: Arc<AtomicBool>,
    interrupt: Arc<Mutex<Option<Arc<duckdb::InterruptHandle>>>>,
}
impl Cancellation {
    pub fn request(&self) {
        self.flag.store(true, Ordering::Release);
        if let Ok(guard) = self.interrupt.lock() {
            if let Some(handle) = guard.as_ref() {
                handle.interrupt();
            }
        }
    }
    pub fn is_requested(&self) -> bool {
        self.flag.load(Ordering::Acquire)
    }
    pub fn check(&self) -> Result<()> {
        if self.is_requested() {
            Err(StoreError::cancelled())
        } else {
            Ok(())
        }
    }
    pub fn bind(&self, con: &duckdb::Connection) {
        if let Ok(mut guard) = self.interrupt.lock() {
            *guard = Some(con.interrupt_handle());
        }
    }
}

#[derive(Default, Serialize)]
pub struct TransactionReport {
    pub committed: bool,
    pub rolled_back: bool,
}
#[derive(Default, Serialize)]
pub struct TableReport {
    pub table: String,
    pub keys: Vec<String>,
    pub before: u64,
    pub after: u64,
    pub selected: u64,
    pub inserted: u64,
    pub updated: u64,
    pub unchanged: u64,
    pub stale: u64,
    pub deleted_keys: u64,
}
#[derive(Serialize)]
pub struct FileReport {
    pub dataset: String,
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
    pub state: String,
}
#[derive(Serialize)]
pub struct Finding {
    pub kind: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dataset: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub table: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rows: Option<u64>,
}
impl Finding {
    pub fn new(
        kind: &str,
        message: impl Into<String>,
        dataset: Option<&str>,
        table: Option<&str>,
        rows: Option<u64>,
    ) -> Self {
        Self {
            kind: kind.into(),
            message: message.into(),
            dataset: dataset.map(str::to_owned),
            table: table.map(str::to_owned),
            rows,
        }
    }
}
#[derive(Serialize)]
pub struct Validation {
    pub check: String,
    pub table: String,
    pub rows: u64,
    pub passed: bool,
}
#[derive(Serialize)]
pub struct Report {
    pub schema_version: u32,
    pub operation: String,
    pub status: String,
    pub ok: bool,
    pub engine_version: Option<String>,
    pub counts: BTreeMap<String, u64>,
    pub coverage: Vec<Value>,
    pub files: Vec<FileReport>,
    pub tables: Vec<TableReport>,
    pub before: BTreeMap<String, u64>,
    pub after: BTreeMap<String, u64>,
    pub duplicates: BTreeMap<String, u64>,
    pub errors: Vec<StoreError>,
    pub warnings: Vec<Finding>,
    pub unknowns: Vec<Finding>,
    pub validation: Vec<Validation>,
    pub transaction: TransactionReport,
    pub started_at: DateTime<Utc>,
    pub finished_at: Option<DateTime<Utc>>,
    pub timings: BTreeMap<String, u64>,
    pub capabilities: Value,
    #[serde(skip)]
    pub exit_code: i32,
    #[serde(skip)]
    start: Option<Instant>,
}
impl Report {
    pub fn new(operation: &str) -> Self {
        Self {
            schema_version: 1,
            operation: operation.into(),
            status: "succeeded".into(),
            ok: true,
            engine_version: None,
            counts: BTreeMap::new(),
            coverage: vec![],
            files: vec![],
            tables: vec![],
            before: BTreeMap::new(),
            after: BTreeMap::new(),
            duplicates: BTreeMap::new(),
            errors: vec![],
            warnings: vec![],
            unknowns: vec![],
            validation: vec![],
            transaction: TransactionReport::default(),
            started_at: Utc::now(),
            finished_at: None,
            timings: BTreeMap::new(),
            capabilities: Value::Null,
            exit_code: 0,
            start: Some(Instant::now()),
        }
    }
    pub fn unknown(&mut self, finding: Finding) {
        self.unknowns.push(finding);
    }
    pub fn finish(&mut self, error: Option<StoreError>) {
        if let Some(error) = error {
            self.exit_code = error.exit_code;
            self.status = if error.kind == "cancelled" {
                "cancelled"
            } else {
                "failed"
            }
            .into();
            self.ok = false;
            self.errors.push(error);
        } else if !self.unknowns.is_empty()
            || self
                .coverage
                .iter()
                .any(|v| v.get("status").and_then(Value::as_str) == Some("partial"))
        {
            self.status = "partial".into();
        }
        self.finished_at = Some(Utc::now());
        self.timings.insert(
            "total_ms".into(),
            self.start
                .take()
                .map(|s| s.elapsed().as_millis() as u64)
                .unwrap_or(0),
        );
        self.counts.insert("files".into(), self.files.len() as u64);
        self.counts
            .insert("duplicates".into(), self.duplicates.values().sum());
        for (name, value) in [
            ("selected", self.tables.iter().map(|t| t.selected).sum()),
            ("inserted", self.tables.iter().map(|t| t.inserted).sum()),
            ("updated", self.tables.iter().map(|t| t.updated).sum()),
            ("unchanged", self.tables.iter().map(|t| t.unchanged).sum()),
            ("stale", self.tables.iter().map(|t| t.stale).sum()),
        ] {
            self.counts.insert(format!("rows_{name}"), value);
        }
    }
}
