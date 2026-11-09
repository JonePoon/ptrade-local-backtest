use crate::model::Result;
use serde::Serialize;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;
static NEXT: AtomicU64 = AtomicU64::new(0);
pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let temp = parent.join(format!(
        ".ptrade-store-{}-{}-{}.json.tmp",
        std::process::id(),
        chrono::Utc::now().timestamp_micros(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)?;
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temp, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}
pub struct Progress {
    path: Option<PathBuf>,
    operation: String,
    start: Instant,
}
impl Progress {
    pub fn new(path: Option<PathBuf>, operation: &str) -> Self {
        Self {
            path,
            operation: operation.into(),
            start: Instant::now(),
        }
    }
    pub fn emit(
        &self,
        phase: &str,
        completed: usize,
        total: usize,
        dataset: Option<&str>,
        table: Option<&str>,
        message: &str,
    ) -> Result<()> {
        if let Some(path) = &self.path {
            write_json(
                path,
                &serde_json::json!({"schema_version":1,"operation":self.operation,
            "phase":phase,"completed":completed,"total":total,"dataset":dataset,"table":table,
            "message":message,"elapsed_ms":self.start.elapsed().as_millis() as u64}),
            )?;
        }
        Ok(())
    }
}
