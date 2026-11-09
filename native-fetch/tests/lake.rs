use arrow_array::{Int64Array, StringArray, TimestampMicrosecondArray};
use arrow_schema::{DataType, TimeUnit};
use chrono::{NaiveDate, Utc};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use ptrade_fetch::config::Dataset;
use ptrade_fetch::data::Bar;
use ptrade_fetch::lake::{Lake, atomic_json, load_manifest, scope_id, verify_scope};
use std::fs::File;
use tempfile::tempdir;

fn row(date: NaiveDate) -> Bar {
    Bar {
        symbol: "000001.SZ".into(),
        stamp: date.and_hms_opt(9, 31, 0).unwrap(),
        open: 10.,
        high: 11.,
        low: 9.,
        close: 10.5,
        volume: 14,
        amount: 147.,
    }
}
#[test]
fn immutable_month_shards_have_real_date_units_provenance_and_digest_resume() {
    let temp = tempdir().unwrap();
    let start = NaiveDate::from_ymd_opt(2026, 10, 31).unwrap();
    let end = NaiveDate::from_ymd_opt(2026, 11, 2).unwrap();
    let mut lake = Lake::open(temp.path()).unwrap();
    let rows = vec![row(start), row(end)];
    let fetched = Utc::now();
    let files = lake
        .publish(
            Dataset::Minute,
            "000001.SZ",
            start,
            end,
            &rows,
            fetched,
            serde_json::json!({"partial":false}),
        )
        .unwrap();
    assert_eq!(files.len(), 2);
    let scope = scope_id(Dataset::Minute, "000001.SZ", start, end);
    assert!(verify_scope(&lake.root, &lake.manifest, &scope).unwrap());
    for info in &files {
        let mut reader = ParquetRecordBatchReaderBuilder::try_new(File::open(&info.path).unwrap())
            .unwrap()
            .build()
            .unwrap();
        let batch = reader.next().unwrap().unwrap();
        assert_eq!(
            batch
                .schema()
                .field_with_name("trade_date")
                .unwrap()
                .data_type(),
            &DataType::Date32
        );
        assert_eq!(
            batch
                .schema()
                .field_with_name("bar_time")
                .unwrap()
                .data_type(),
            &DataType::Timestamp(TimeUnit::Microsecond, None)
        );
        assert_eq!(
            batch
                .schema()
                .field_with_name("fetched_at")
                .unwrap()
                .data_type(),
            &DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into()))
        );
        assert_eq!(
            batch
                .column_by_name("data_version")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap()
                .value(0),
            "v1"
        );
        assert_eq!(
            batch
                .column_by_name("volume")
                .unwrap()
                .as_any()
                .downcast_ref::<Int64Array>()
                .unwrap()
                .value(0),
            14
        );
        assert_eq!(
            batch
                .column_by_name("fetched_at")
                .unwrap()
                .as_any()
                .downcast_ref::<TimestampMicrosecondArray>()
                .unwrap()
                .value(0),
            fetched.timestamp_micros()
        );
    }
    let old = std::fs::read(&files[0].path).unwrap();
    let revision = lake
        .publish(
            Dataset::Minute,
            "000001.SZ",
            start,
            end,
            &rows,
            Utc::now(),
            serde_json::json!({"partial":false}),
        )
        .unwrap();
    assert_ne!(files[0].path, revision[0].path);
    assert_eq!(std::fs::read(&files[0].path).unwrap(), old);
    assert_eq!(lake.manifest.files.len(), 4);
}
#[test]
fn corrupted_digest_and_traversal_never_count_as_completed_history() {
    let temp = tempdir().unwrap();
    let date = NaiveDate::from_ymd_opt(2026, 11, 6).unwrap();
    let mut lake = Lake::open(temp.path()).unwrap();
    let files = lake
        .publish(
            Dataset::Daily,
            "000001.SZ",
            date,
            date,
            &[row(date)],
            Utc::now(),
            serde_json::json!({"partial":false}),
        )
        .unwrap();
    std::fs::write(&files[0].path, b"corrupt").unwrap();
    assert!(
        verify_scope(
            &lake.root,
            &lake.manifest,
            &scope_id(Dataset::Daily, "000001.SZ", date, date)
        )
        .is_err()
    );
    let mut manifest = lake.manifest.clone();
    manifest.files[0].path = "../foreign.parquet".into();
    atomic_json(&lake.root.join(".ptrade-fetch/manifest.json"), &manifest).unwrap();
    assert!(load_manifest(&lake.root).is_err());
}
#[test]
fn empty_publish_and_second_writer_never_advance_state() {
    let temp = tempdir().unwrap();
    let date = NaiveDate::from_ymd_opt(2026, 11, 6).unwrap();
    let mut lake = Lake::open(temp.path()).unwrap();
    assert!(Lake::open(temp.path()).is_err());
    assert!(
        lake.publish(
            Dataset::Daily,
            "000001.SZ",
            date,
            date,
            &[],
            Utc::now(),
            serde_json::json!({})
        )
        .is_err()
    );
    assert!(!lake.root.join(".ptrade-fetch/manifest.json").exists());
    assert!(!lake.root.join(".ptrade-fetch/watermarks.json").exists());
}
#[test]
fn atomic_progress_replaces_a_single_valid_json_snapshot() {
    let temp = tempdir().unwrap();
    let path = temp.path().join("progress.json");
    atomic_json(&path, &serde_json::json!({"completed":1})).unwrap();
    atomic_json(&path, &serde_json::json!({"completed":2})).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert_eq!(value["completed"], 2);
}

#[test]
fn failed_manifest_publication_rolls_back_only_new_uncommitted_shards() {
    let temp = tempdir().unwrap();
    let date = NaiveDate::from_ymd_opt(2026, 11, 6).unwrap();
    let mut lake = Lake::open(temp.path()).unwrap();
    let legacy = lake.root.join("curated/daily_bars/trade_date=2026-11");
    std::fs::create_dir_all(&legacy).unwrap();
    let prior = legacy.join("prior.parquet");
    std::fs::write(&prior, b"prior-owned-by-other-writer").unwrap();
    std::fs::create_dir(lake.root.join(".ptrade-fetch/manifest.json")).unwrap();
    assert!(
        lake.publish(
            Dataset::Daily,
            "000001.SZ",
            date,
            date,
            &[row(date)],
            Utc::now(),
            serde_json::json!({"partial":false})
        )
        .is_err()
    );
    assert_eq!(
        std::fs::read(&prior).unwrap(),
        b"prior-owned-by-other-writer"
    );
    assert_eq!(std::fs::read_dir(&legacy).unwrap().count(), 1);
    assert!(!lake.root.join(".ptrade-fetch/watermarks.json").exists());
}
#[test]
fn a_partial_scope_is_not_publishable_as_complete_history() {
    let temp = tempdir().unwrap();
    let date = NaiveDate::from_ymd_opt(2026, 11, 6).unwrap();
    let mut lake = Lake::open(temp.path()).unwrap();
    assert!(
        lake.publish(
            Dataset::Daily,
            "000001.SZ",
            date,
            date,
            &[row(date)],
            Utc::now(),
            serde_json::json!({"partial":true})
        )
        .is_err()
    );
    assert!(lake.manifest.files.is_empty());
    assert!(lake.manifest.completed.is_empty());
}
#[test]
fn reference_discovery_accepts_large_utf8_without_inventing_names_or_dates() {
    use arrow_array::{Array, LargeStringArray, RecordBatch};
    use arrow_schema::{Field, Schema};
    use parquet::arrow::ArrowWriter;
    use std::sync::Arc;
    let temp = tempdir().unwrap();
    let folder = temp
        .path()
        .join("curated/instruments/as_of_date=2026-11-06");
    std::fs::create_dir_all(&folder).unwrap();
    let schema = Arc::new(Schema::new(vec![Field::new(
        "symbol",
        DataType::LargeUtf8,
        true,
    )]));
    let values = Arc::new(LargeStringArray::from(vec![
        Some("600000.SH"),
        Some("000001.SZ"),
        Some("920001.BJ"),
        None,
    ])) as Arc<dyn Array>;
    let batch = RecordBatch::try_new(schema.clone(), vec![values]).unwrap();
    let file = File::create(folder.join("refs.parquet")).unwrap();
    let mut writer = ArrowWriter::try_new(file, schema, None).unwrap();
    writer.write(&batch).unwrap();
    writer.close().unwrap();
    let found = ptrade_fetch::lake::discover_symbols(temp.path()).unwrap();
    assert_eq!(
        found.iter().map(|s| s.symbol.as_str()).collect::<Vec<_>>(),
        vec!["000001.SZ", "600000.SH"]
    );
}

fn complete_scope(root: &std::path::Path) -> (Lake, String) {
    let date = NaiveDate::from_ymd_opt(2026, 11, 6).unwrap();
    let mut lake = Lake::open(root).unwrap();
    lake.publish(
        Dataset::Daily,
        "000001.SZ",
        date,
        date,
        &[row(date)],
        Utc::now(),
        serde_json::json!({"partial": false}),
    )
    .unwrap();
    let id = scope_id(Dataset::Daily, "000001.SZ", date, date);
    assert!(verify_scope(&lake.root, &lake.manifest, &id).unwrap());
    (lake, id)
}

#[test]
fn resume_guard_rejects_empty_completed_shards() {
    let temp = tempdir().unwrap();
    let (mut lake, id) = complete_scope(temp.path());
    lake.manifest.completed.get_mut(&id).unwrap().files.clear();
    assert!(verify_scope(&lake.root, &lake.manifest, &id).is_err());
    atomic_json(
        &lake.root.join(".ptrade-fetch/manifest.json"),
        &lake.manifest,
    )
    .unwrap();
    assert!(load_manifest(&lake.root).is_err());
}

#[test]
fn resume_guard_rejects_partial_or_missing_coverage() {
    let temp = tempdir().unwrap();
    let (lake, id) = complete_scope(temp.path());
    for coverage in [
        serde_json::json!({"partial": true}),
        serde_json::json!({}),
        serde_json::Value::Null,
    ] {
        let mut manifest = lake.manifest.clone();
        manifest.completed.get_mut(&id).unwrap().coverage = coverage;
        assert!(verify_scope(&lake.root, &manifest, &id).is_err());
        atomic_json(&lake.root.join(".ptrade-fetch/manifest.json"), &manifest).unwrap();
        assert!(load_manifest(&lake.root).is_err());
    }
}

#[test]
fn resume_guard_binds_scope_key_and_unique_shard_references() {
    let temp = tempdir().unwrap();
    let (lake, id) = complete_scope(temp.path());
    let mut wrong_window = lake.manifest.clone();
    wrong_window.completed.get_mut(&id).unwrap().start =
        NaiveDate::from_ymd_opt(2026, 11, 5).unwrap();
    assert!(verify_scope(&lake.root, &wrong_window, &id).is_err());
    atomic_json(
        &lake.root.join(".ptrade-fetch/manifest.json"),
        &wrong_window,
    )
    .unwrap();
    assert!(load_manifest(&lake.root).is_err());

    let mut repeated = lake.manifest.clone();
    let commit = repeated.completed.get_mut(&id).unwrap();
    commit.files.push(commit.files[0].clone());
    assert!(verify_scope(&lake.root, &repeated, &id).is_err());
    atomic_json(&lake.root.join(".ptrade-fetch/manifest.json"), &repeated).unwrap();
    assert!(load_manifest(&lake.root).is_err());
}
