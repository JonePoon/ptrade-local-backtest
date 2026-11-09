pub mod apply;
pub mod config;
pub mod model;
pub mod paths;
pub mod progress;
pub mod source;
pub mod sql;
pub mod stage;
pub mod tables;
pub mod validate;

use config::JobConfig;
use duckdb::{AccessMode, Config, Connection};
use model::{Cancellation, Report, Result, StoreError};
use progress::Progress;
use std::time::Instant;

pub type CommitHook<'a> = Option<&'a dyn Fn(&Connection) -> Result<()>>;
pub fn execute(
    operation: &str,
    config: Option<JobConfig>,
    progress: &Progress,
    cancel: &Cancellation,
) -> Report {
    execute_with_hook(operation, config, progress, cancel, None)
}
pub fn execute_with_hook(
    operation: &str,
    config: Option<JobConfig>,
    progress: &Progress,
    cancel: &Cancellation,
    hook: CommitHook<'_>,
) -> Report {
    let mut report = Report::new(operation);
    report.capabilities = source::capabilities();
    let result = run(operation, config, progress, cancel, hook, &mut report);
    let error = result.err().map(|e| {
        if cancel.is_requested() {
            StoreError::cancelled()
        } else {
            e
        }
    });
    report.finish(error);
    report
}
fn run(
    operation: &str,
    config: Option<JobConfig>,
    progress: &Progress,
    cancel: &Cancellation,
    hook: CommitHook<'_>,
    report: &mut Report,
) -> Result<()> {
    cancel.check()?;
    let memory = Connection::open_in_memory()?;
    let version: String = memory.query_row("SELECT version()", [], |row| row.get(0))?;
    report.engine_version = Some(version.clone());
    if version != "v1.5.6" {
        return Err(StoreError::dependency(format!(
            "refuse {version}; writer requires v1.5.6"
        )));
    }
    drop(memory);
    if operation == "doctor" {
        progress.emit(
            "complete",
            1,
            1,
            None,
            None,
            "native C API v1.5.6 available",
        )?;
        return Ok(());
    }
    let mut cfg =
        config.ok_or_else(|| StoreError::config("--config is required for plan/ingest/verify"))?;
    cfg.validate(operation)?;
    if operation == "verify" && !cfg.db.is_file() {
        return Err(StoreError::data("target database does not exist"));
    }
    let con = if operation == "plan" && !cfg.db.is_file() {
        Connection::open_in_memory()?
    } else {
        if operation == "ingest" {
            if let Some(parent) = cfg.db.parent().filter(|p| !p.as_os_str().is_empty()) {
                std::fs::create_dir_all(parent)?;
            }
        }
        Connection::open_with_flags(
            &cfg.db,
            Config::default().access_mode(if operation == "ingest" {
                AccessMode::ReadWrite
            } else {
                AccessMode::ReadOnly
            })?,
        )?
    };
    cancel.bind(&con);
    con.execute_batch(&format!(
        "SET threads={};SET memory_limit={}",
        cfg.threads,
        sql::lit(&cfg.memory_limit.value())
    ))?;
    for table in tables::TABLES {
        let rows = if sql::exists(&con, table.name)? {
            sql::count(
                &con,
                &format!("SELECT count(*) FROM {}", sql::ident(table.name)),
            )?
        } else {
            0
        };
        report.before.insert(table.name.into(), rows);
        report.after.insert(table.name.into(), rows);
    }
    if operation == "verify" {
        progress.emit(
            "validate",
            0,
            1,
            None,
            None,
            "read-only physical contract verification",
        )?;
        validate::all_physical(&con, &cfg, report, true)?;
        validate::coverage(&con, &cfg, report)?;
        progress.emit(
            "complete",
            1,
            1,
            None,
            None,
            "read-only verification complete",
        )?;
        return Ok(());
    }
    if !["plan", "ingest"].contains(&operation) {
        return Err(StoreError::config("unsupported operation"));
    }
    let transaction = operation == "ingest";
    if transaction {
        con.execute_batch("BEGIN TRANSACTION")?;
    }
    let outcome = (|| -> Result<()> {
        if transaction {
            tables::create_core(&con)?;
        }
        // Existing data is verified before accepting corrections; no deletion
        // or rebuild is used to make a corrupt historical target look healthy.
        validate::all_physical(&con, &cfg, report, false)?;
        tables::old_views(&con)?;
        progress.emit(
            "discover",
            0,
            cfg.datasets.len(),
            None,
            None,
            "scan immutable Parquet files and fingerprints",
        )?;
        let started = Instant::now();
        let mut inputs = source::discover(&con, &cfg, report, cancel)?;
        report
            .timings
            .insert("discover_ms".into(), started.elapsed().as_millis() as u64);
        let started = Instant::now();
        source::materialize(&con, &cfg, &mut inputs, report, cancel)?;
        let targets = stage::prepare(&con, &cfg, &inputs, report)?;
        validate::sources(&con, &cfg, &inputs, report)?;
        for (i, name) in targets.iter().enumerate() {
            cancel.check()?;
            progress.emit(
                "validate",
                i,
                targets.len(),
                None,
                Some(name),
                "validate complete staged rows",
            )?;
            validate::table(&con, &cfg, report, name, false)?;
        }
        if cfg.strict_unknowns && !report.unknowns.is_empty() {
            return Err(StoreError::data(
                "strict_unknowns: unresolved source/adjustment/state facts",
            ));
        }
        report.timings.insert(
            "stage_validate_ms".into(),
            started.elapsed().as_millis() as u64,
        );
        if !transaction {
            progress.emit(
                "complete",
                1,
                1,
                None,
                None,
                "plan validated without target writes",
            )?;
            return Ok(());
        }
        let started = Instant::now();
        for (i, name) in targets.iter().enumerate() {
            cancel.check()?;
            progress.emit(
                "upsert",
                i,
                targets.len(),
                None,
                Some(name),
                "key-only update/insert in one writer transaction",
            )?;
            apply::apply(&con, report, name)?;
        }
        validate::all_physical(&con, &cfg, report, true)?;
        validate::coverage(&con, &cfg, report)?;
        if let Some(hook) = hook {
            hook(&con)?;
        }
        cancel.check()?;
        source::ensure_stable(&inputs, cancel)?;
        source::commit_files(&con, &cfg, &inputs)?;
        // All output data and provenance checkpoints commit together. Do not
        // advance a file watermark after a failed stage, SQL call or interrupt.
        con.execute_batch("COMMIT")?;
        report.transaction.committed = true;
        report.timings.insert(
            "apply_commit_ms".into(),
            started.elapsed().as_millis() as u64,
        );
        Ok(())
    })();
    if outcome.is_err() && transaction {
        match con.execute_batch("ROLLBACK") {
            Ok(()) => report.transaction.rolled_back = true,
            Err(e) => report.warnings.push(model::Finding::new(
                "rollback_error",
                e.to_string(),
                None,
                None,
                None,
            )),
        }
        report.after = report.before.clone();
        for table in &mut report.tables {
            table.after = table.before;
            table.inserted = 0;
            table.updated = 0;
            table.deleted_keys = 0;
        }
    }
    if outcome.is_ok() {
        progress.emit(
            "complete",
            1,
            1,
            None,
            None,
            if transaction {
                "data and checkpoints committed"
            } else {
                "plan complete"
            },
        )?;
    }
    outcome
}

#[cfg(test)]
mod tests;
