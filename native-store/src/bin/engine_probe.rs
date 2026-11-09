use clap::Parser;
use duckdb::{params, AccessMode, Config, Connection};
use serde_json::json;
use std::path::PathBuf;

#[derive(Parser)]
struct Args {
    #[arg(long)]
    db: Option<PathBuf>,
    #[arg(long)]
    write_probe: bool,
    #[arg(long)]
    parquet: Option<PathBuf>,
}

fn run(args: Args) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let memory = Connection::open_in_memory()?;
    let version: String = memory.query_row("SELECT version()", [], |row| row.get(0))?;
    if version != "v1.5.6" {
        return Err(format!("refuse engine {version}; expected v1.5.6").into());
    }
    let mut count = None;
    if let Some(path) = args.db {
        let cfg = Config::default().access_mode(if args.write_probe {
            AccessMode::ReadWrite
        } else {
            AccessMode::ReadOnly
        })?;
        let connection = Connection::open_with_flags(&path, cfg)?;
        if args.write_probe {
            connection.execute_batch(
                "BEGIN TRANSACTION;
                 CREATE TABLE IF NOT EXISTS __native_store_probe(
                     id INTEGER PRIMARY KEY, origin VARCHAR, close DOUBLE);
                 INSERT INTO __native_store_probe VALUES (1, 'rust-1.5.6', 12.5)
                     ON CONFLICT(id) DO UPDATE SET origin=excluded.origin, close=excluded.close;
                 COMMIT;",
            )?;
        }
        count = Some(connection.query_row(
            "SELECT COUNT(*) FROM __native_store_probe WHERE id=? AND close=?",
            params![1, 12.5],
            |row| row.get::<_, i64>(0),
        )?);
    }
    let parquet_rows = if let Some(path) = args.parquet {
        let path = path
            .to_string_lossy()
            .replace('\\', "/")
            .replace('\'', "''");
        Some(memory.query_row(
            &format!("SELECT count(*) FROM read_parquet('{path}', hive_partitioning=false)"),
            [],
            |row| row.get::<_, i64>(0),
        )?)
    } else {
        None
    };
    Ok(json!({
        "protocol_version": 1,
        "backend": "rust-duckdb-c-api",
        "engine_version": version,
        "probe_rows": count,
        "parquet_rows": parquet_rows,
        "ok": true,
    }))
}

fn main() {
    match run(Args::parse()) {
        Ok(report) => println!("{report}"),
        Err(error) => {
            println!(
                "{}",
                json!({"protocol_version":1,"ok":false,"error":error.to_string()})
            );
            std::process::exit(6);
        }
    }
}
