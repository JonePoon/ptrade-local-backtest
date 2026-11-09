mod paths;

use clap::{Parser, Subcommand};
use ptrade_fetch::config::Config;
use ptrade_fetch::error::FetchError;
use ptrade_fetch::lake::atomic_json;
use ptrade_fetch::runner::{self, Report};
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    name = "ptrade-fetch",
    version,
    about = "Native TDX/CNE immutable Parquet downloader"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    #[arg(long, global = true)]
    report: Option<PathBuf>,
    #[arg(long, global = true)]
    progress: Option<PathBuf>,
}
#[derive(Subcommand)]
enum Command {
    Doctor,
    Plan,
    Fetch,
}

fn output_path_safe(path: &std::path::Path, cli: &Cli, cfg: Option<&Config>) -> bool {
    path.extension()
        .and_then(|suffix| suffix.to_str())
        .is_some_and(|suffix| suffix.eq_ignore_ascii_case("json"))
        && !cli
            .config
            .as_ref()
            .is_some_and(|config| paths::same_path(path, config).unwrap_or(true))
        && !cfg.is_some_and(|config| paths::is_within(path, &config.lake).unwrap_or(true))
}

fn outputs_safe(cli: &Cli, cfg: Option<&Config>) -> bool {
    cli.report
        .as_ref()
        .is_none_or(|path| output_path_safe(path, cli, cfg))
        && cli
            .progress
            .as_ref()
            .is_none_or(|path| output_path_safe(path, cli, cfg))
        && !matches!((&cli.report, &cli.progress), (Some(report), Some(progress))
            if paths::same_path(report, progress).unwrap_or(true))
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    let operation = match cli.command {
        Command::Doctor => "doctor",
        Command::Plan => "plan",
        Command::Fetch => "fetch",
    };
    let config = if matches!(cli.command, Command::Doctor) {
        Ok(None)
    } else {
        cli.config
            .as_deref()
            .ok_or_else(|| FetchError::Config("--config is required for plan/fetch".into()))
            .and_then(Config::load)
            .map(Some)
    };
    // Resolve output identities before plan/fetch can publish anything. A JSON
    // report must not destroy its config, the source lake, or its progress file.
    let safe = outputs_safe(&cli, config.as_ref().ok().and_then(Option::as_ref));
    let mut report = if !safe {
        Report::failed(
            operation,
            FetchError::Config(
                "report/progress must be distinct .json outputs outside the lake, never config"
                    .into(),
            ),
        )
    } else {
        match config {
            Ok(Some(cfg)) => match cli.command {
                Command::Plan => {
                    runner::plan(&cfg).unwrap_or_else(|e| Report::failed(operation, e))
                }
                Command::Fetch => runner::fetch(cfg, cli.progress.clone()).await,
                Command::Doctor => runner::doctor(),
            },
            Ok(None) => runner::doctor(),
            Err(error) => Report::failed(operation, error),
        }
    };
    if safe {
        if let Some(path) = &cli.report {
            if let Err(error) = atomic_json(path, &report) {
                report.status = "failed".into();
                report.errors.push(error.detail(None));
            }
        }
    }
    if safe && !matches!(cli.command, Command::Fetch) {
        if let Some(path) = cli.progress {
            if let Err(error) = atomic_json(
                &path,
                &serde_json::json!({"status":report.status,"phase":"finished","completed":report.plan.len(),"total":report.plan.len(),"rows":0,"files":0,"updated_at":report.finished_at}),
            ) {
                report.status = "failed".into();
                report.errors.push(error.detail(None));
            }
        }
    }
    let status = report.exit_code();
    match serde_json::to_string(&report) {
        Ok(text) => println!("{text}"),
        Err(error) => {
            eprintln!("report serialization: {error}");
            std::process::exit(3);
        }
    }
    std::process::exit(status);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> tempfile::TempDir {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("work")
            .join("cli-tests");
        std::fs::create_dir_all(&root).unwrap();
        tempfile::tempdir_in(root).unwrap()
    }

    fn cli() -> Cli {
        Cli {
            command: Command::Plan,
            config: None,
            report: None,
            progress: None,
        }
    }

    #[test]
    fn report_and_progress_cannot_overwrite_input_configuration() {
        let dir = fixture();
        let config = dir.path().join("config.json");
        std::fs::write(&config, b"{\"fixture\":true}").unwrap();
        let mut args = cli();
        args.config = Some(config.clone());
        args.report = Some(config.clone());
        assert!(!outputs_safe(&args, None));
        args.report = None;
        args.progress = Some(config.clone());
        assert!(!outputs_safe(&args, None));
        assert_eq!(std::fs::read(config).unwrap(), b"{\"fixture\":true}");
    }

    #[test]
    fn new_report_progress_aliases_are_rejected_before_directory_creation() {
        let dir = fixture();
        let mut args = cli();
        args.report = Some(dir.path().join("report.json"));
        args.progress = Some(
            dir.path()
                .join("not-created")
                .join("..")
                .join("report.json"),
        );
        assert!(!outputs_safe(&args, None));
        assert!(!dir.path().join("not-created").exists());
    }

    #[test]
    fn output_paths_stay_json_and_outside_the_source_lake() {
        let dir = fixture();
        let lake = dir.path().join("lake");
        std::fs::create_dir(&lake).unwrap();
        let cfg: Config = serde_json::from_value(serde_json::json!({
            "lake": lake, "start":"2026-11-05", "end":"2026-11-06",
            "symbols":["000001.SZ"], "datasets":["daily_bars"],
        }))
        .unwrap();
        let mut args = cli();
        args.report = Some(dir.path().join("output").join("report.json"));
        args.progress = Some(dir.path().join("output").join("progress.JSON"));
        assert!(outputs_safe(&args, Some(&cfg)));
        args.report = Some(dir.path().join("part.parquet"));
        assert!(!outputs_safe(&args, Some(&cfg)));
        args.report = Some(cfg.lake.join(".ptrade-fetch").join("manifest.json"));
        assert!(!outputs_safe(&args, Some(&cfg)));
        assert!(!dir.path().join("output").exists());
    }
}
