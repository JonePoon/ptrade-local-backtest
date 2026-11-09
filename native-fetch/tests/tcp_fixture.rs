use chrono::{NaiveDate, Timelike};
use flate2::{Compression, write::ZlibEncoder};
use ptrade_fetch::config::{Config, Host};
use ptrade_fetch::data::RawBar;
use ptrade_fetch::runner::fetch;
use ptrade_fetch::tdx::SETUPS;
use serde_json::json;
use std::io::Write;
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::time::Duration;
use tempfile::tempdir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{Mutex, oneshot};
use tokio::task::{JoinHandle, JoinSet};
use tokio::time::Instant;

#[derive(Clone, Copy)]
enum Mode {
    Daily,
    Minute,
    DisconnectOnce,
    BadChecksum,
    Repeat,
    Unordered,
}
#[derive(Default)]
struct Stats {
    active: AtomicUsize,
    peak: AtomicUsize,
    requests: AtomicUsize,
    failed: AtomicBool,
    offsets: Mutex<Vec<u16>>,
    times: Mutex<Vec<Instant>>,
}
struct Active(Arc<Stats>);
impl Drop for Active {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}
struct Fixture {
    host: Host,
    stats: Arc<Stats>,
    stop: oneshot::Sender<()>,
    task: JoinHandle<()>,
}
impl Fixture {
    async fn new(mode: Mode) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let stats = Arc::new(Stats::default());
        let state = stats.clone();
        let (stop, mut stopped) = oneshot::channel();
        let task = tokio::spawn(async move {
            let mut clients = JoinSet::new();
            loop {
                tokio::select! {result=listener.accept()=>{let(stream,_)=result.unwrap();let state=state.clone();clients.spawn(async move{serve(stream,state,mode).await;});},_= &mut stopped=>break}
            }
            clients.abort_all();
            while clients.join_next().await.is_some() {}
        });
        Self {
            host: Host {
                host: address.ip().to_string(),
                port: address.port(),
            },
            stats,
            stop,
            task,
        }
    }
    async fn finish(self) {
        let _ = self.stop.send(());
        self.task.await.unwrap();
    }
}
async fn read_packet(stream: &mut TcpStream) -> Option<Vec<u8>> {
    let mut header = [0u8; 10];
    stream.read_exact(&mut header).await.ok()?;
    let length = u16::from_le_bytes([header[6], header[7]]) as usize;
    let mut body = vec![0; length];
    stream.read_exact(&mut body).await.ok()?;
    let mut out = header.to_vec();
    out.extend(body);
    Some(out)
}
async fn counted_packet(stream: &mut TcpStream, stats: &Stats) -> Option<Vec<u8>> {
    let out = read_packet(stream).await?;
    stats.requests.fetch_add(1, Ordering::SeqCst);
    stats.times.lock().await.push(Instant::now());
    Some(out)
}
fn frame(payload: &[u8]) -> Vec<u8> {
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(payload).unwrap();
    let bytes = encoder.finish().unwrap();
    let mut out = vec![0; 16];
    out[12..14].copy_from_slice(&(bytes.len() as u16).to_le_bytes());
    out[14..16].copy_from_slice(&(payload.len() as u16).to_le_bytes());
    out.extend(bytes);
    out
}
async fn fragmented(stream: &mut TcpStream, bytes: &[u8]) {
    for chunk in bytes.chunks(3) {
        if stream.write_all(chunk).await.is_err() {
            return;
        }
        tokio::task::yield_now().await;
    }
}
fn varint(value: i64) -> Vec<u8> {
    let mut raw = value.unsigned_abs();
    let mut byte = (raw & 63) as u8;
    if value < 0 {
        byte |= 64;
    }
    raw >>= 6;
    if raw != 0 {
        byte |= 128;
    }
    let mut out = vec![byte];
    while raw != 0 {
        let mut b = (raw & 127) as u8;
        raw >>= 7;
        if raw != 0 {
            b |= 128;
        }
        out.push(b);
    }
    out
}
fn body(rows: &[RawBar], category: u16, index: bool) -> Vec<u8> {
    let mut out = (rows.len() as u16).to_le_bytes().to_vec();
    let mut previous = 0;
    for row in rows {
        let date = row.stamp.date();
        if category == 9 {
            out.extend(
                row.stamp
                    .format("%Y%m%d")
                    .to_string()
                    .parse::<u32>()
                    .unwrap()
                    .to_le_bytes(),
            );
        } else {
            let year = row.stamp.format("%Y").to_string().parse::<u16>().unwrap();
            let month = row.stamp.format("%m").to_string().parse::<u16>().unwrap();
            let day = row.stamp.format("%d").to_string().parse::<u16>().unwrap();
            let packed = ((year - 2004) << 11) + month * 100 + day;
            out.extend(packed.to_le_bytes());
            out.extend(((row.stamp.hour() * 60 + row.stamp.minute()) as u16).to_le_bytes());
        }
        let open = (row.open * 1000.).round() as i64;
        let close = (row.close * 1000.).round() as i64;
        let high = (row.high * 1000.).round() as i64;
        let low = (row.low * 1000.).round() as i64;
        for value in [open - previous, close - open, high - open, low - open] {
            out.extend(varint(value));
        }
        previous = close;
        out.extend((row.volume as f32).to_bits().to_le_bytes());
        out.extend((row.amount as f32).to_bits().to_le_bytes());
        if index {
            out.extend([1, 0, 2, 0]);
        }
        let _ = date;
    }
    out
}
fn bar(date: NaiveDate, h: u32, m: u32) -> RawBar {
    RawBar {
        stamp: date.and_hms_opt(h, m, 0).unwrap(),
        open: 10.,
        high: 10.1,
        low: 9.9,
        close: 10.05,
        volume: 14.,
        amount: 140.,
    }
}
fn daily() -> Vec<RawBar> {
    vec![
        bar(NaiveDate::from_ymd_opt(2026, 11, 5).unwrap(), 15, 0),
        bar(NaiveDate::from_ymd_opt(2026, 11, 6).unwrap(), 15, 0),
    ]
}
fn minute(category: u16) -> Vec<RawBar> {
    let old = NaiveDate::from_ymd_opt(2026, 11, 5).unwrap();
    let date = NaiveDate::from_ymd_opt(2026, 11, 6).unwrap();
    let mut rows = vec![bar(old, 15, 0)];
    let step = if category == 0 { 5 } else { 1 };
    for minute in (571..=690).chain(781..=900) {
        if minute % step == 0 {
            rows.push(bar(date, minute / 60, minute % 60));
        }
    }
    rows.push(bar(date, 13, 0));
    rows
}
async fn serve(mut stream: TcpStream, stats: Arc<Stats>, mode: Mode) {
    let now = stats.active.fetch_add(1, Ordering::SeqCst) + 1;
    stats.peak.fetch_max(now, Ordering::SeqCst);
    let _active = Active(stats.clone());
    for expected in SETUPS {
        let Some(packet) = counted_packet(&mut stream, &stats).await else {
            return;
        };
        assert_eq!(packet, expected);
        fragmented(&mut stream, &frame(&[0, 0])).await;
    }
    while let Some(packet) = counted_packet(&mut stream, &stats).await {
        assert_eq!(packet.len(), 38);
        let category = u16::from_le_bytes([packet[20], packet[21]]);
        let offset = u16::from_le_bytes([packet[24], packet[25]]);
        stats.offsets.lock().await.push(offset);
        if matches!(mode, Mode::DisconnectOnce) && !stats.failed.swap(true, Ordering::SeqCst) {
            let mut broken = vec![0; 16];
            broken[12..14].copy_from_slice(&20u16.to_le_bytes());
            broken[14..16].copy_from_slice(&20u16.to_le_bytes());
            broken.push(0);
            fragmented(&mut stream, &broken).await;
            return;
        }
        let mut rows = if category != 9 {
            minute(category)
        } else if offset == 0 || matches!(mode, Mode::Repeat) {
            daily()
        } else {
            Vec::new()
        };
        if matches!(mode, Mode::Unordered) {
            rows.reverse();
        }
        let index = packet[12] == 1 && &packet[14..16] == b"00";
        let mut response = frame(&body(&rows, category, index));
        if matches!(mode, Mode::BadChecksum) {
            *response.last_mut().unwrap() ^= 1;
        }
        fragmented(&mut stream, &response).await;
    }
}
fn config(path: &std::path::Path, host: Host, datasets: &[&str]) -> Config {
    let mut cfg:Config=serde_json::from_value(json!({"lake":path,"start":"2026-11-05","end":"2026-11-06","symbols":["000001.SZ","000002.SZ"],"datasets":datasets,"tdx_hosts":[host],"page_size":2,"max_pages":4,"concurrency":2,"requests_per_second":200,"timeout_secs":2,"retries":1,"retry_backoff_ms":0,"capture_wire":true})).unwrap();
    cfg.validate().unwrap();
    cfg
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn real_native_tcp_fragmentation_concurrency_rate_and_verified_resume() {
    let fixture = Fixture::new(Mode::Daily).await;
    let temp = tempdir().unwrap();
    let mut cfg = config(temp.path(), fixture.host.clone(), &["daily_bars"]);
    cfg.requests_per_second = 25.;
    let progress = temp.path().join("progress.json");
    let report = fetch(cfg.clone(), Some(progress.clone())).await;
    assert_eq!(report.status, "succeeded", "{:?}", report.errors);
    assert_eq!(report.counts["rows"], 4);
    assert_eq!(report.counts["peak_in_flight"], 2);
    assert_eq!(report.counts["requests"], 8);
    assert_eq!(fixture.stats.requests.load(Ordering::SeqCst), 8);
    assert_eq!(fixture.stats.peak.load(Ordering::SeqCst), 2);
    assert_eq!(report.files.len(), 2);
    assert_eq!(report.wire.len(), 16);
    let times = fixture.stats.times.lock().await;
    for pair in times.windows(2) {
        assert!(
            pair[1].duration_since(pair[0]) >= Duration::from_millis(30),
            "actual requests bypassed global pacing"
        );
    }
    drop(times);
    let progress: serde_json::Value =
        serde_json::from_slice(&std::fs::read(progress).unwrap()).unwrap();
    assert_eq!(progress["status"], "succeeded");
    assert_eq!(progress["completed"], 2);
    let requests = fixture.stats.requests.load(Ordering::SeqCst);
    let again = fetch(cfg.clone(), None).await;
    assert_eq!(again.counts["jobs_skipped"], 2);
    assert_eq!(fixture.stats.requests.load(Ordering::SeqCst), requests);
    cfg.refresh = true;
    let revision = fetch(cfg, None).await;
    assert_eq!(revision.status, "succeeded");
    assert_ne!(report.files[0].path, revision.files[0].path);
    fixture.finish().await;
}
#[tokio::test]
async fn dropped_body_reconnects_and_retries_the_same_page() {
    let fixture = Fixture::new(Mode::DisconnectOnce).await;
    let temp = tempdir().unwrap();
    let mut cfg = config(temp.path(), fixture.host.clone(), &["daily_bars"]);
    cfg.symbols.truncate(1);
    let report = fetch(cfg, None).await;
    assert_eq!(report.status, "succeeded", "{:?}", report.errors);
    assert_eq!(report.counts["retries"], 1);
    assert_eq!(*fixture.stats.offsets.lock().await, vec![0, 0]);
    fixture.finish().await;
}
#[tokio::test]
async fn page_cap_is_partial_and_never_publishes_or_advances_success() {
    let fixture = Fixture::new(Mode::Daily).await;
    let temp = tempdir().unwrap();
    let mut cfg = config(temp.path(), fixture.host.clone(), &["daily_bars"]);
    cfg.symbols.truncate(1);
    cfg.start = NaiveDate::from_ymd_opt(2026, 11, 4).unwrap();
    cfg.max_pages = 1;
    let report = fetch(cfg, None).await;
    assert_eq!(report.status, "partial");
    assert_eq!(report.exit_code(), 4);
    assert!(report.files.is_empty());
    assert!(!temp.path().join(".ptrade-fetch/manifest.json").exists());
    let watermarks: serde_json::Value = serde_json::from_slice(
        &std::fs::read(temp.path().join(".ptrade-fetch/watermarks.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(watermarks["latest"], json!({}));
    fixture.finish().await;
}
#[tokio::test]
async fn checksum_failure_and_repeated_pages_are_not_valid_prefixes() {
    for mode in [Mode::BadChecksum, Mode::Repeat] {
        let fixture = Fixture::new(mode).await;
        let temp = tempdir().unwrap();
        let mut cfg = config(temp.path(), fixture.host.clone(), &["daily_bars"]);
        cfg.symbols.truncate(1);
        cfg.start = NaiveDate::from_ymd_opt(2026, 11, 4).unwrap();
        let report = fetch(cfg, None).await;
        assert_eq!(report.status, "failed");
        assert_eq!(report.errors[0].code, "protocol_error");
        assert!(report.files.is_empty());
        assert!(!temp.path().join(".ptrade-fetch/manifest.json").exists());
        fixture.finish().await;
    }
}
#[tokio::test]
async fn minute_and_five_minute_native_share_units_and_session_counts() {
    let fixture = Fixture::new(Mode::Minute).await;
    let temp = tempdir().unwrap();
    let mut cfg = config(
        temp.path(),
        fixture.host.clone(),
        &["minute_bars", "minute_bars_5m"],
    );
    cfg.symbols.truncate(1);
    cfg.start = cfg.end;
    cfg.page_size = 800;
    let report = fetch(cfg, None).await;
    assert_eq!(report.status, "succeeded", "{:?}", report.errors);
    assert_eq!(report.counts["rows"], 288);
    assert_eq!(report.counts["off_session_rows"], 2);
    assert_eq!(report.files.iter().map(|f| f.rows).sum::<u64>(), 288);
    assert!(
        report
            .files
            .iter()
            .all(|f| f.volume_unit == "share" && f.data_version == "v1")
    );
    fixture.finish().await;
}

#[tokio::test]
async fn unobserved_requested_end_date_does_not_advance_the_watermark() {
    let fixture = Fixture::new(Mode::Daily).await;
    let temp = tempdir().unwrap();
    let mut cfg = config(temp.path(), fixture.host.clone(), &["daily_bars"]);
    cfg.symbols.truncate(1);
    cfg.end = NaiveDate::from_ymd_opt(2026, 11, 9).unwrap();
    let report = fetch(cfg, None).await;
    assert_eq!(report.status, "partial");
    assert!(report.files.is_empty());
    assert_eq!(report.coverage[0]["end_boundary_observed"], false);
    assert_eq!(report.coverage[0]["reason"], "calendar_boundary_unverified");
    assert!(!temp.path().join(".ptrade-fetch/manifest.json").exists());
    fixture.finish().await;
}

#[tokio::test]
async fn unordered_source_rows_are_sorted_by_real_timestamp_not_packet_position() {
    use arrow_array::Date32Array;
    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
    use std::fs::File;
    let fixture = Fixture::new(Mode::Unordered).await;
    let temp = tempdir().unwrap();
    let mut cfg = config(temp.path(), fixture.host.clone(), &["daily_bars"]);
    cfg.symbols.truncate(1);
    let report = fetch(cfg, None).await;
    assert_eq!(report.status, "succeeded", "{:?}", report.errors);
    let mut reader =
        ParquetRecordBatchReaderBuilder::try_new(File::open(&report.files[0].path).unwrap())
            .unwrap()
            .build()
            .unwrap();
    let batch = reader.next().unwrap().unwrap();
    let dates = batch
        .column_by_name("trade_date")
        .unwrap()
        .as_any()
        .downcast_ref::<Date32Array>()
        .unwrap();
    assert!(dates.value(0) < dates.value(1));
    fixture.finish().await;
}
