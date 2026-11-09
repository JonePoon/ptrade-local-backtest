use clap::{Parser, Subcommand};
use ptrade_store::{
    config::JobConfig,
    model::{Cancellation, Report, StoreError},
    progress::{write_json, Progress},
};
use std::path::{Path, PathBuf};

#[derive(Parser)]
#[command(
    name = "ptrade-store",
    version,
    about = "Native DuckDB 1.5.6 CNE Parquet incremental ingestion"
)]
struct Args {
    #[command(subcommand)]
    operation: Operation,
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    #[arg(long, global = true)]
    report: Option<PathBuf>,
    #[arg(long, global = true)]
    progress: Option<PathBuf>,
}
#[derive(Subcommand)]
enum Operation {
    Doctor,
    Plan,
    Ingest,
    Verify,
}
impl Operation {
    fn name(&self) -> &'static str {
        match self {
            Self::Doctor => "doctor",
            Self::Plan => "plan",
            Self::Ingest => "ingest",
            Self::Verify => "verify",
        }
    }
}
fn same_file(a: &Path, b: &Path) -> bool {
    // New targets have no final inode yet. Resolve existing prefixes and keep
    // missing suffixes lexical; an unreadable identity must fail closed.
    ptrade_store::paths::same_path(a, b).unwrap_or(true)
}
fn output_safe(path: &Path, args: &Args, cfg: Option<&JobConfig>) -> bool {
    path.extension()
        .and_then(|s| s.to_str())
        .is_some_and(|s| s.eq_ignore_ascii_case("json"))
        && !args.config.as_ref().is_some_and(|p| same_file(path, p))
        && !cfg.is_some_and(|c| same_file(path, &c.db))
}
fn main() {
    let args = match Args::try_parse() {
        Ok(a) => a,
        Err(error) => {
            if error.use_stderr() {
                let mut report = Report::new("unknown");
                report.finish(Some(StoreError::config(error.to_string())));
                println!("{}", serde_json::to_string(&report).unwrap());
                std::process::exit(2);
            }
            let _ = error.print();
            return;
        }
    };
    let operation = args.operation.name();
    let config: std::result::Result<Option<JobConfig>, StoreError> = match &args.config {
        Some(path) => std::fs::read(path)
            .map_err(StoreError::from)
            .and_then(|s| serde_json::from_slice(&s).map_err(StoreError::from))
            .map(Some),
        None => Ok(None),
    };
    let safe =
        args.report
            .as_ref()
            .is_none_or(|p| output_safe(p, &args, config.as_ref().ok().and_then(Option::as_ref)))
            && args.progress.as_ref().is_none_or(|p| {
                output_safe(p, &args, config.as_ref().ok().and_then(Option::as_ref))
            })
            && !matches!((&args.report,&args.progress),(Some(a),Some(b)) if same_file(a,b));
    let cancel = Cancellation::default();
    let signal = cancel.clone();
    let signal_result = ctrlc::set_handler(move || signal.request());
    let progress = Progress::new(if safe { args.progress.clone() } else { None }, operation);
    let mut report = if !safe {
        let mut r = Report::new(operation);
        r.finish(Some(StoreError::config(
            "report/progress must be distinct .json outputs, never config or db",
        )));
        r
    } else {
        match config {
            Ok(cfg) => ptrade_store::execute(operation, cfg, &progress, &cancel),
            Err(error) => {
                let mut r = Report::new(operation);
                r.finish(Some(error));
                r
            }
        }
    };
    if let Err(error) = signal_result {
        report.warnings.push(ptrade_store::model::Finding::new(
            "signal_handler_unavailable",
            error.to_string(),
            None,
            None,
            None,
        ));
    }
    if safe {
        if let Some(path) = &args.report {
            if let Err(error) = write_json(path, &report) {
                report.errors.push(error);
                report.status = "failed".into();
                report.ok = false;
                report.exit_code = 1;
            }
        }
    }
    println!("{}", serde_json::to_string(&report).unwrap());
    std::process::exit(report.exit_code);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_guard_rejects_missing_database_alias_as_report_output() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("work")
            .join("cli-path-tests");
        std::fs::create_dir_all(&root).unwrap();
        let dir = tempfile::tempdir_in(root).unwrap();
        let report = dir.path().join("target.json");
        let db = dir
            .path()
            .join("not-created")
            .join("..")
            .join("target.json");
        let cfg: JobConfig = serde_json::from_value(serde_json::json!({
            "lake": dir.path(),
            "db": db,
        }))
        .unwrap();
        let args = Args {
            operation: Operation::Ingest,
            config: None,
            report: Some(report.clone()),
            progress: None,
        };
        assert!(!report.exists());
        assert!(same_file(&report, &cfg.db));
        assert!(!output_safe(&report, &args, Some(&cfg)));
        assert!(!dir.path().join("not-created").exists());
    }
}
