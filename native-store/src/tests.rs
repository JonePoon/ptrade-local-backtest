use super::*;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use tempfile::TempDir;
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    dir: TempDir,
    lake: PathBuf,
    db: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("work")
            .join("rust-tests");
        std::fs::create_dir_all(&root).unwrap();
        let dir = tempfile::tempdir_in(root).unwrap();
        let lake = dir.path().join("lake");
        std::fs::create_dir_all(&lake).unwrap();
        let db = dir.path().join("test.duckdb");
        Self { dir, lake, db }
    }
    fn write(&self, dataset: &str, sql: &str) -> PathBuf {
        let directory = self
            .lake
            .join("curated")
            .join(dataset)
            .join("trade_date=2099-12");
        std::fs::create_dir_all(&directory).unwrap();
        let file = directory.join(format!(
            "part-{}.parquet",
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let con = Connection::open_in_memory().unwrap();
        con.execute_batch(&format!(
            "COPY ({sql}) TO {} (FORMAT PARQUET)",
            sql::lit(&file.to_string_lossy().replace('\\', "/"))
        ))
        .unwrap();
        if dataset == "daily_bars" {
            // These fixtures explicitly define an unadjusted synthetic series:
            // factor=1 is a published test fact, never a production fallback.
            self.write("adj_factors",&format!("SELECT DISTINCT symbol,trade_date,'hfq' adjust_type,1.0::DOUBLE factor,'fixture' AS source,'v1' data_version,TIMESTAMPTZ '2000-01-01 00:00:00+00' fetched_at FROM ({sql})"));
        }
        file
    }
    fn config(&self, datasets: &[&str], start: &str, end: &str) -> JobConfig {
        serde_json::from_value(serde_json::json!({"lake":self.lake,"db":self.db,"start":start,"end":end,
            "datasets":datasets,"threads":1,"memory_limit":"256MB","require_volume_v2":true,"extra_field_is_ignored":123})).unwrap()
    }
    fn run(&self, cfg: JobConfig) -> Report {
        execute(
            "ingest",
            Some(cfg),
            &Progress::new(None, "ingest"),
            &Cancellation::default(),
        )
    }
    fn con(&self) -> Connection {
        Connection::open(&self.db).unwrap()
    }
}
fn bars(
    symbol: &str,
    day: &str,
    close: f64,
    pre: Option<f64>,
    version: &str,
    clock: &str,
) -> String {
    let pre = pre.map_or("NULL::DOUBLE".into(), |v| v.to_string());
    format!("SELECT '{}' symbol,DATE '{}' trade_date,{}::DOUBLE open,{}::DOUBLE high,{}::DOUBLE low,{}::DOUBLE AS close,{pre} pre_close,100::BIGINT volume,{}::DOUBLE amount,'fixture' AS source,'{version}' data_version,TIMESTAMPTZ '{clock}' fetched_at",symbol,day,close,close+0.5,close-0.5,close,close*100.0)
}
fn factors(symbol: &str, day: &str, factor: f64, clock: &str) -> String {
    format!("SELECT '{symbol}' symbol,DATE '{day}' trade_date,'hfq' adjust_type,{factor}::DOUBLE factor,'fixture' AS source,'v1' data_version,TIMESTAMPTZ '{clock}' fetched_at")
}
fn status(symbol: &str, day: &str, risk: Option<bool>, state: &str, clock: &str) -> String {
    let risk = risk.map_or("NULL::BOOLEAN".into(), |b| b.to_string());
    format!("SELECT '{symbol}' symbol,DATE '{day}' trade_date,{} is_trading,'{state}' status,{risk} risk_warning,'fixture' AS source,'v1' data_version,TIMESTAMPTZ '{clock}' fetched_at",state=="normal")
}
fn instruments() -> String {
    "SELECT '600000.SH' symbol,'*ST 最新名称' AS name,'SH' exchange,'stock' asset_type,DATE '1990-01-01' list_date,NULL::DATE delist_date,NULL::VARCHAR prev_symbol,'fixture' AS source,'v1' data_version,TIMESTAMPTZ '2025-02-01 00:00:00+00' fetched_at".into()
}
fn assert_success(report: &Report) {
    assert!(
        report.ok,
        "{}",
        serde_json::to_string_pretty(report).unwrap()
    );
    assert!(report.transaction.committed);
    assert!(!report.transaction.rolled_back);
}
fn old_row(con: &Connection, day: &str, close: f64, risk: i32, name: &str) {
    tables::create_core(con).unwrap();
    con.execute_batch(&format!("INSERT INTO ashare_1d_stock VALUES ('600000.SS','{day}',{close},{close},{close},{close},100,{}, {close},1,{close},'{name}',{risk},0,0,0)",close*100.0)).unwrap();
}

#[test]
fn doctor_uses_native_engine_without_config_or_database_files() {
    let report = execute(
        "doctor",
        None,
        &Progress::new(None, "doctor"),
        &Cancellation::default(),
    );
    assert!(report.ok);
    assert_eq!(report.engine_version.as_deref(), Some("v1.5.6"));
    assert!(report.files.is_empty());
    assert!(!report.transaction.committed);
    assert_eq!(serde_json::to_value(report).unwrap()["schema_version"], 1);
}
#[test]
fn path_guard_rejects_database_below_lake_with_missing_parent() {
    let f = Fixture::new();
    let mut cfg = f.config(&["daily_bars"], "20250102", "20250102");
    cfg.db = f.lake.join("not-created").join("target.duckdb");
    assert!(!cfg.db.parent().unwrap().exists());
    let error = cfg
        .validate("ingest")
        .expect_err("a nonexistent output parent must not bypass the source-lake guard");
    assert_eq!(error.exit_code, 2);
    assert!(error.message.contains("outside the source lake"));
    assert!(!cfg.db.parent().unwrap().exists());
}

#[test]
fn real_file_date_overrides_month_directory_and_repeated_ingest_is_idempotent() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let cfg = f.config(&["daily_bars"], "20250102", "20250102");
    let first = f.run(cfg.clone());
    assert_success(&first);
    let second = f.run(cfg);
    assert_success(&second);
    assert_eq!(second.counts["rows_inserted"], 0);
    assert_eq!(second.counts["rows_updated"], 0);
    assert_eq!(second.counts["files_changed"], 0);
    let con = f.con();
    let pair: (String, String) = con
        .query_row("SELECT code,date FROM ashare_1d_stock", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(pair, ("600000.SS".into(), "20250102".into()));
}
#[test]
fn newest_fetched_revision_wins_with_deterministic_duplicate_accounting() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let cfg = f.config(&["daily_bars"], "20250102", "20250102");
    assert_success(&f.run(cfg.clone()));
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            12.0,
            Some(9.0),
            "v2",
            "2025-01-04 00:00:00+00",
        ),
    );
    let report = f.run(cfg);
    assert_success(&report);
    assert_eq!(report.duplicates["daily_bars"], 1);
    assert_eq!(report.counts["rows_updated"], 1);
    assert_eq!(
        f.con()
            .query_row("SELECT close FROM ashare_1d_stock", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        12.0
    );
}
#[test]
fn key_upsert_preserves_unreturned_same_day_rows_other_days_and_extra_columns() {
    let f = Fixture::new();
    let con = f.con();
    old_row(&con, "20250101", 8.0, 0, "旧名");
    con.execute_batch("ALTER TABLE ashare_1d_stock ADD COLUMN user_note VARCHAR DEFAULT '保留';CREATE TABLE untouched(value INTEGER);INSERT INTO untouched VALUES (77);") .unwrap();
    con.execute_batch("INSERT INTO ashare_1d_stock(code,date,open,high,low,close,volume,money,preclose,adj_factor,is_st,is_delisted,change,pct_chg) VALUES ('000001.SZ','20250102',9,9,9,9,100,900,9,1,0,0,0,0)").unwrap();
    drop(con);
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            None,
            "v2",
            "2025-01-04 00:00:00+00",
        ),
    );
    let report = f.run(f.config(&["daily_bars"], "20250102", "20250102"));
    assert_success(&report);
    let con = f.con();
    assert_eq!(
        sql::count(&con, "SELECT count(*) FROM ashare_1d_stock").unwrap(),
        3
    );
    assert_eq!(
        con.query_row(
            "SELECT close FROM ashare_1d_stock WHERE date='20250101'",
            [],
            |r| r.get::<_, f64>(0)
        )
        .unwrap(),
        8.0
    );
    assert_eq!(
        con.query_row(
            "SELECT user_note FROM ashare_1d_stock WHERE date='20250101'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "保留"
    );
    assert_eq!(
        con.query_row("SELECT value FROM untouched", [], |r| r.get::<_, i32>(0))
            .unwrap(),
        77
    );
}
#[test]
fn previous_close_uses_lake_rows_before_requested_window() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &format!(
            "{} UNION ALL {}",
            bars(
                "600000.SH",
                "2025-01-02",
                10.0,
                None,
                "v2",
                "2025-01-05 00:00:00+00"
            ),
            bars(
                "600000.SH",
                "2025-01-03",
                5.0,
                None,
                "v2",
                "2025-01-05 00:00:00+00"
            )
        ),
    );
    f.write(
        "adj_factors",
        &format!(
            "{} UNION ALL {}",
            factors("600000.SH", "2025-01-02", 1.0, "2025-01-05 00:00:00+00"),
            factors("600000.SH", "2025-01-03", 2.0, "2025-01-05 00:00:00+00")
        ),
    );
    assert_success(&f.run(f.config(&["daily_bars", "adj_factors"], "20250103", "20250103")));
    let con = f.con();
    let pair: (f64, f64) = con
        .query_row("SELECT preclose,adj_factor FROM ashare_1d_stock", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(pair, (5.0, 2.0));
    assert_eq!(
        sql::count(&con, "SELECT count(*) FROM ashare_1d_stock").unwrap(),
        1
    );
    assert_eq!(
        con.query_row("SELECT close*adj_factor FROM ashare_1d_stock", [], |r| r
            .get::<_, f64>(0))
            .unwrap(),
        10.0
    );
}
#[test]
fn previous_close_falls_back_to_existing_database_before_window() {
    let f = Fixture::new();
    let con = f.con();
    old_row(&con, "20250102", 10.0, 0, "旧名");
    drop(con);
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-03",
            12.0,
            None,
            "v2",
            "2025-01-05 00:00:00+00",
        ),
    );
    f.write(
        "adj_factors",
        &factors("600000.SH", "2025-01-03", 1.0, "2025-01-05 00:00:00+00"),
    );
    assert_success(&f.run(f.config(&["daily_bars"], "20250103", "20250103")));
    assert_eq!(
        f.con()
            .query_row(
                "SELECT preclose FROM ashare_1d_stock WHERE date='20250103'",
                [],
                |r| r.get::<_, f64>(0)
            )
            .unwrap(),
        10.0
    );
}
#[test]
fn published_preclose_is_preferred_and_unknown_first_close_is_not_fabricated() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(7.5),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    assert_success(&f.run(f.config(&["daily_bars"], "20250102", "20250102")));
    assert_eq!(
        f.con()
            .query_row("SELECT preclose FROM ashare_1d_stock", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        7.5
    );
    let g = Fixture::new();
    g.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            None,
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let report = g.run(g.config(&["daily_bars"], "20250102", "20250102"));
    assert!(!report.ok);
    assert!(report.transaction.rolled_back);
    assert!(!report.transaction.committed);
    assert!(!sql::exists(&g.con(), "ashare_1d_stock").unwrap());
    assert!(report
        .errors
        .iter()
        .any(|e| e.message.contains("required_previous_close_anchor")));
}
#[test]
fn metadata_update_cannot_replay_an_older_bar_over_newer_stored_price() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-05 00:00:00+00",
        ),
    );
    let cfg = f.config(&["daily_bars", "trading_status"], "20250102", "20250102");
    assert_success(&f.run(cfg.clone()));
    let old_files = source::discover(
        &f.con(),
        &cfg,
        &mut Report::new("plan"),
        &Cancellation::default(),
    )
    .unwrap();
    for file in &old_files["daily_bars"].files {
        std::fs::remove_file(&file.path).unwrap();
    }
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            5.0,
            None,
            "v2",
            "2025-01-04 00:00:00+00",
        ),
    );
    f.write(
        "trading_status",
        &status(
            "600000.SH",
            "2025-01-02",
            Some(true),
            "suspended",
            "2025-01-06 00:00:00+00",
        ),
    );
    assert_success(&f.run(cfg));
    let con = f.con();
    let pair: (f64, i32) = con
        .query_row("SELECT close,is_st FROM ashare_1d_stock", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(pair, (10.0, 1));
}
#[test]
fn latest_instrument_name_does_not_reclassify_historical_st_or_name() {
    let f = Fixture::new();
    let con = f.con();
    old_row(&con, "20250102", 10.0, 0, "普通旧名");
    drop(con);
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            11.0,
            None,
            "v2",
            "2025-02-02 00:00:00+00",
        ),
    );
    f.write("instruments", &instruments());
    f.write(
        "trading_status",
        &status(
            "600000.SH",
            "2025-01-02",
            Some(false),
            "normal",
            "2025-02-02 00:00:00+00",
        ),
    );
    assert_success(&f.run(f.config(
        &["daily_bars", "instruments", "trading_status"],
        "20250102",
        "20250102",
    )));
    let con = f.con();
    let pair: (String, i32) = con
        .query_row("SELECT name,is_st FROM ashare_1d_stock", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(pair, ("普通旧名".into(), 0));
    assert_eq!(
        con.query_row("SELECT name FROM ashare_stock_basic", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "*ST 最新名称"
    );
}
#[test]
fn nullable_risk_during_suspension_does_not_invent_a_false_st_state() {
    let f = Fixture::new();
    let con = f.con();
    old_row(&con, "20250102", 10.0, 1, "历史ST");
    drop(con);
    f.write(
        "trading_status",
        &status(
            "600000.SH",
            "2025-01-02",
            None,
            "suspended",
            "2025-01-05 00:00:00+00",
        ),
    );
    assert_success(&f.run(f.config(&["trading_status"], "20250102", "20250102")));
    assert_eq!(
        f.con()
            .query_row("SELECT is_st FROM ashare_1d_stock", [], |r| r
                .get::<_, i32>(0))
            .unwrap(),
        1
    );
}
fn minute_rows(symbol: &str) -> String {
    format!("SELECT '{symbol}' symbol,DATE '2025-01-02' trade_date,t bar_time,'1m' frequency,10::DOUBLE open,10::DOUBLE high,10::DOUBLE low,10::DOUBLE AS close,100::BIGINT volume,1000::DOUBLE amount,'fixture' AS source,'v1' data_version,TIMESTAMPTZ '2025-01-03 00:00:00+00' fetched_at FROM (SELECT TIMESTAMP '2025-01-02 09:31:00'+i*INTERVAL '1 minute' t FROM range(120) x(i) UNION ALL SELECT TIMESTAMP '2025-01-02 13:01:00'+i*INTERVAL '1 minute' t FROM range(120) x(i))")
}
#[test]
fn two_hundred_forty_minutes_stay_native_shares_without_synthetic_auction() {
    let f = Fixture::new();
    f.write("minute_bars", &minute_rows("600000.SH"));
    let report = f.run(f.config(&["minute_bars"], "20250102", "20250102"));
    assert_success(&report);
    let con = f.con();
    assert_eq!(
        sql::count(&con, "SELECT count(*) FROM ashare_1m_stock").unwrap(),
        240
    );
    assert_eq!(
        con.query_row("SELECT min(trade_time) FROM ashare_1m_stock", [], |r| {
            r.get::<_, String>(0)
        })
        .unwrap(),
        "2025-01-02 09:31:00"
    );
    assert_eq!(
        con.query_row("SELECT max(volume) FROM ashare_1m_stock", [], |r| r
            .get::<_, f64>(0))
            .unwrap(),
        100.0
    );
    assert_eq!(
        sql::count(
            &con,
            "SELECT count(*) FROM ashare_1m_stock WHERE trade_time LIKE '%09:30:00'"
        )
        .unwrap(),
        0
    );
}
#[test]
fn index_minutes_and_five_minute_dataset_never_pollute_stock_one_minute_keys() {
    let f = Fixture::new();
    f.write(
        "minute_bars",
        &format!(
            "{} UNION ALL {}",
            minute_rows("600000.SH"),
            minute_rows("000300.SH")
        ),
    );
    f.write(
        "minute_bars_5m",
        &minute_rows("600000.SH").replace("'1m' frequency", "'5m' frequency"),
    );
    assert_success(&f.run(f.config(&["minute_bars", "minute_bars_5m"], "20250102", "20250102")));
    let con = f.con();
    assert_eq!(
        sql::count(&con, "SELECT count(*) FROM ashare_1m_stock").unwrap(),
        240
    );
    assert_eq!(
        sql::count(&con, "SELECT count(*) FROM ashare_1m_index").unwrap(),
        240
    );
}
#[test]
fn mixed_frequency_and_lunch_padding_fail_before_commit() {
    for mutate in ["frequency", "padding"] {
        let f = Fixture::new();
        let query = if mutate == "frequency" {
            minute_rows("600000.SH").replace("'1m' frequency", "'5m' frequency")
        } else {
            minute_rows("600000.SH").replace("09:31:00", "12:01:00")
        };
        f.write("minute_bars", &query);
        let report = f.run(f.config(&["minute_bars"], "20250102", "20250102"));
        assert!(!report.ok);
        assert!(report.transaction.rolled_back);
        assert!(!report.transaction.committed);
        assert!(!sql::exists(&f.con(), "_native_store_files").unwrap());
    }
}
#[test]
fn v1_lot_volume_is_explicitly_gated_and_converted_only_for_known_stock_sources() {
    let f = Fixture::new();
    let query = bars(
        "600000.SH",
        "2025-01-02",
        10.0,
        Some(9.0),
        "v1",
        "2025-01-03 00:00:00+00",
    )
    .replace("100::BIGINT volume", "1::BIGINT volume")
    .replace("'fixture' AS source", "'tdx_protocol' AS source");
    f.write("daily_bars", &query);
    let mut cfg = f.config(&["daily_bars"], "20250102", "20250102");
    let report = f.run(cfg.clone());
    assert!(!report.ok);
    assert!(report.transaction.rolled_back);
    cfg.require_volume_v2 = false;
    assert_success(&f.run(cfg));
    assert_eq!(
        f.con()
            .query_row("SELECT volume FROM ashare_1d_stock", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        100.0
    );
}
#[test]
fn index_quantity_stays_raw_instead_of_unproved_times_one_hundred() {
    let f = Fixture::new();
    f.write(
        "index_bars",
        &bars(
            "000300.SH",
            "2025-01-02",
            3000.0,
            Some(3000.0),
            "v1",
            "2025-01-03 00:00:00+00",
        )
        .replace("100::BIGINT volume", "1::BIGINT volume"),
    );
    let report = f.run(f.config(&["index_bars"], "20250102", "20250102"));
    assert_success(&report);
    assert_eq!(
        f.con()
            .query_row("SELECT volume FROM ashare_1d_index", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        1.0
    );
}
#[test]
fn nonfinite_price_bad_ohlc_and_wrong_units_roll_back_tables_and_checkpoints() {
    for kind in ["nan", "ohlc", "unit"] {
        let f = Fixture::new();
        let good = bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        );
        let bad = match kind {
            "nan" => good.replace("10::DOUBLE AS close", "'NaN'::DOUBLE AS close"),
            "ohlc" => good.replace("10.5::DOUBLE high", "1::DOUBLE high"),
            _ => good.replace("100::BIGINT volume", "1::BIGINT volume"),
        };
        f.write("daily_bars", &bad);
        let report = f.run(f.config(&["daily_bars"], "20250102", "20250102"));
        assert!(!report.ok, "{kind}");
        assert!(report.transaction.rolled_back);
        assert_eq!(report.before, report.after);
        assert!(!sql::exists(&f.con(), "_native_store_files").unwrap());
    }
}
#[test]
fn intentional_sql_failure_after_upsert_rolls_back_existing_price_and_provenance() {
    let f = Fixture::new();
    let con = f.con();
    old_row(&con, "20250102", 9.0, 0, "旧名");
    drop(con);
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            None,
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let hook = |con: &Connection| -> Result<()> {
        con.execute_batch("INSERT INTO deliberately_missing_table VALUES (1)")?;
        Ok(())
    };
    let report = execute_with_hook(
        "ingest",
        Some(f.config(&["daily_bars"], "20250102", "20250102")),
        &Progress::new(None, "ingest"),
        &Cancellation::default(),
        Some(&hook),
    );
    assert!(!report.ok);
    assert!(report.transaction.rolled_back);
    assert_eq!(report.before, report.after);
    assert_eq!(
        f.con()
            .query_row("SELECT close FROM ashare_1d_stock", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        9.0
    );
    assert_eq!(
        sql::count(&f.con(), "SELECT count(*) FROM _native_store_files").unwrap(),
        0
    );
}
#[test]
fn cancellation_before_commit_retains_original_data() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let cancel = Cancellation::default();
    let c = cancel.clone();
    let hook = move |_: &Connection| -> Result<()> {
        c.request();
        Ok(())
    };
    let report = execute_with_hook(
        "ingest",
        Some(f.config(&["daily_bars"], "20250102", "20250102")),
        &Progress::new(None, "ingest"),
        &cancel,
        Some(&hook),
    );
    assert_eq!(report.status, "cancelled");
    assert!(!report.transaction.committed);
    assert!(!sql::exists(&f.con(), "ashare_1d_stock").unwrap());
}
#[test]
fn dry_plan_does_not_create_target_database() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let report = execute(
        "plan",
        Some(f.config(&["daily_bars"], "20250102", "20250102")),
        &Progress::new(None, "plan"),
        &Cancellation::default(),
    );
    assert!(report.ok, "{}", serde_json::to_string(&report).unwrap());
    assert!(!f.db.exists());
    assert!(!report.transaction.committed);
}
#[test]
fn verify_is_readonly_and_detects_duplicate_business_keys() {
    let f = Fixture::new();
    let con = f.con();
    old_row(&con, "20250102", 10.0, 0, "旧名");
    con.execute_batch("INSERT INTO ashare_1d_stock SELECT * FROM ashare_1d_stock")
        .unwrap();
    drop(con);
    let report = execute(
        "verify",
        Some(f.config(&["daily_bars"], "20250102", "20250102")),
        &Progress::new(None, "verify"),
        &Cancellation::default(),
    );
    assert!(!report.ok);
    assert_eq!(
        sql::count(&f.con(), "SELECT count(*) FROM ashare_1d_stock").unwrap(),
        2
    );
    assert!(!report.transaction.committed);
}
#[test]
fn snapshot_membership_preserves_snapshot_semantics_and_does_not_clear_old_intervals() {
    let f = Fixture::new();
    let con = f.con();
    tables::create_core(&con).unwrap();
    con.execute_batch("INSERT INTO ashare_index_weight VALUES ('000300.SS','沪深300','600000.SS','20200101','20250101',1,'ptrade',NULL)").unwrap();
    drop(con);
    f.write("index_constituents","SELECT '000300.SH' index_symbol,'600000.SH' symbol,DATE '2025-01-02' as_of_date,1::DOUBLE weight,'fixture' AS source,'v1' data_version,TIMESTAMPTZ '2025-01-03 00:00:00+00' fetched_at");
    assert_success(&f.run(f.config(&["index_constituents"], "20250102", "20250102")));
    let con = f.con();
    assert_eq!(
        sql::count(&con, "SELECT count(*) FROM ashare_index_weight").unwrap(),
        2
    );
    assert_eq!(sql::count(&con,"SELECT count(*) FROM ashare_index_weight WHERE in_date='20250102' AND snapshot_date='20250102' AND out_date IS NULL").unwrap(),1);
}
#[test]
fn explicit_closed_calendar_key_only_removes_that_key() {
    let f = Fixture::new();
    let con = f.con();
    tables::create_core(&con).unwrap();
    con.execute_batch("INSERT INTO ashare_calendar VALUES ('20250101'),('20250102'),('20250103')")
        .unwrap();
    drop(con);
    f.write("trading_calendar","SELECT DATE '2025-01-02' trade_date,FALSE is_trading,'fixture' AS source,'v1' data_version,TIMESTAMPTZ '2025-01-04 00:00:00+00' fetched_at");
    assert_success(&f.run(f.config(&["trading_calendar"], "20250102", "20250102")));
    assert_eq!(
        sql::texts(&f.con(), "SELECT date FROM ashare_calendar ORDER BY date").unwrap(),
        vec!["20250101", "20250103"]
    );
}
#[test]
fn required_missing_inputs_are_loud_and_strict_unknowns_can_gate_commit() {
    let f = Fixture::new();
    let report = f.run(f.config(&["daily_bars"], "20250102", "20250102"));
    assert!(!report.ok);
    assert_eq!(report.exit_code, 3);
    assert!(report.transaction.rolled_back);
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let mut cfg = f.config(&["daily_bars"], "20250102", "20250102");
    cfg.strict_unknowns = true;
    let report = f.run(cfg);
    assert!(!report.ok);
    assert!(report.transaction.rolled_back);
}

#[test]
fn authoritative_roots_ignore_abandoned_mutable_history_and_empty_mapping_never_falls_back() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            1.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let active = f.dir.path().join("active-generation");
    std::fs::create_dir_all(&active).unwrap();
    let file = active.join("data.parquet");
    let con = Connection::open_in_memory().unwrap();
    con.execute_batch(&format!(
        "COPY ({}) TO {} (FORMAT PARQUET)",
        bars(
            "600000.SH",
            "2025-01-02",
            12.0,
            Some(9.0),
            "v2",
            "2025-01-04 00:00:00+00"
        ),
        sql::lit(&file.to_string_lossy().replace('\\', "/"))
    ))
    .unwrap();
    drop(con);
    let mut cfg = f.config(&["daily_bars"], "20250102", "20250102");
    cfg.dataset_roots.insert("daily_bars".into(), vec![active]);
    assert_success(&f.run(cfg.clone()));
    assert_eq!(
        f.con()
            .query_row("SELECT close FROM ashare_1d_stock", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        12.0
    );
    cfg.dataset_roots.insert("daily_bars".into(), vec![]);
    let report = f.run(cfg);
    assert!(!report.ok);
    assert!(report.transaction.rolled_back);
    assert_eq!(
        f.con()
            .query_row("SELECT close FROM ashare_1d_stock", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        12.0
    );
}
#[test]
fn native_overlay_manifest_sha_is_verified_before_any_rows_commit() {
    let f = Fixture::new();
    let file = f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let mut cfg = f.config(&["daily_bars"], "20250102", "20250102");
    cfg.dataset_roots
        .insert("daily_bars".into(), vec![file.clone()]);
    cfg.native_files.insert(
        "daily_bars".into(),
        vec![config::NativeFile {
            path: file.clone(),
            sha256: "0".repeat(64),
            rows: Some(1),
        }],
    );
    let report = f.run(cfg.clone());
    assert!(!report.ok);
    assert!(report.transaction.rolled_back);
    assert!(!sql::exists(&f.con(), "ashare_1d_stock").unwrap());
    cfg.native_files.get_mut("daily_bars").unwrap()[0].sha256 =
        source::digest_file(&file).unwrap().0;
    assert_success(&f.run(cfg));
}
#[test]
fn missing_factor_for_a_new_day_is_not_written_as_null_or_neutral_one() {
    let f = Fixture::new();
    f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let mut cfg = f.config(&["daily_bars"], "20250102", "20250102");
    cfg.dataset_roots.insert("adj_factors".into(), vec![]);
    let report = f.run(cfg);
    assert!(!report.ok);
    assert!(report.transaction.rolled_back);
    assert!(!report.transaction.committed);
    assert!(report
        .errors
        .iter()
        .any(|e| e.message.contains("required_hfq_anchor")));
    assert!(!sql::exists(&f.con(), "ashare_1d_stock").unwrap());
}

#[test]
fn sdk_single_files_and_native_overlay_can_share_roots_without_mislabeling_sdk() {
    let f = Fixture::new();
    let sdk = f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-02",
            10.0,
            Some(9.0),
            "v2",
            "2025-01-03 00:00:00+00",
        ),
    );
    let native = f.write(
        "daily_bars",
        &bars(
            "600000.SH",
            "2025-01-03",
            11.0,
            Some(10.0),
            "v2",
            "2025-01-04 00:00:00+00",
        ),
    );
    let sdk_factors = f.write(
        "adj_factors",
        &format!(
            "{} UNION ALL {}",
            factors("600000.SH", "2025-01-02", 1.0, "2025-01-05 00:00:00+00"),
            factors("600000.SH", "2025-01-03", 1.0, "2025-01-05 00:00:00+00")
        ),
    );
    let mut cfg = f.config(&["daily_bars"], "20250102", "20250103");
    cfg.dataset_roots
        .insert("daily_bars".into(), vec![sdk.clone(), native.clone()]);
    cfg.dataset_roots
        .insert("adj_factors".into(), vec![sdk_factors.clone()]);
    cfg.native_files.insert(
        "daily_bars".into(),
        vec![config::NativeFile {
            path: native.clone(),
            sha256: source::digest_file(&native).unwrap().0,
            rows: Some(1),
        }],
    );
    // Task-10 returns an empty native list for SDK-only datasets such as the
    // newly derived factors. This must not reject the verified SDK files.
    cfg.native_files.insert("adj_factors".into(), vec![]);
    let report = f.run(cfg.clone());
    assert_success(&report);
    assert_eq!(report.after["ashare_1d_stock"], 2);
    assert!(report
        .files
        .iter()
        .any(|file| file.path == sdk.to_string_lossy().replace('\\', "/")));
    assert!(report
        .files
        .iter()
        .any(|file| file.path == sdk_factors.to_string_lossy().replace('\\', "/")));
    assert_eq!(
        f.con()
            .query_row(
                "SELECT close FROM ashare_1d_stock WHERE date='20250103'",
                [],
                |row| row.get::<_, f64>(0)
            )
            .unwrap(),
        11.0
    );
    cfg.native_files.get_mut("daily_bars").unwrap()[0].sha256 = "0".repeat(64);
    let rejected = f.run(cfg);
    assert!(!rejected.ok);
    assert!(rejected.transaction.rolled_back);
    assert_eq!(rejected.before, rejected.after);
    assert_eq!(
        sql::count(&f.con(), "SELECT count(*) FROM ashare_1d_stock").unwrap(),
        2
    );
}
