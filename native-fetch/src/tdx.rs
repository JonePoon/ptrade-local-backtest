use crate::config::{Config, Host, Symbol};
use crate::data::RawBar;
use crate::error::{FetchError, Result};
use chrono::NaiveDate;
use flate2::bufread::ZlibDecoder;
use std::io::{Cursor, Read};
use std::sync::{
    Arc,
    atomic::{AtomicU64, AtomicUsize, Ordering},
};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::Mutex;
use tokio::time::{Instant, sleep_until, timeout};

pub const HEADER_LEN: usize = 16;
pub const SETUPS: [&[u8]; 3] = [
    &[
        0x0c, 0x02, 0x18, 0x93, 0x00, 0x01, 0x03, 0x00, 0x03, 0x00, 0x0d, 0x00, 0x01,
    ],
    &[
        0x0c, 0x02, 0x18, 0x94, 0x00, 0x01, 0x03, 0x00, 0x03, 0x00, 0x0d, 0x00, 0x02,
    ],
    &[
        0x0c, 0x03, 0x18, 0x99, 0x00, 0x01, 0x20, 0x00, 0x20, 0x00, 0xdb, 0x0f, 0xd5, 0xd0, 0xc9,
        0xcc, 0xd6, 0xa4, 0xa8, 0xaf, 0x00, 0x00, 0x00, 0x8f, 0xc2, 0x25, 0x40, 0x13, 0x00, 0x00,
        0xd5, 0x00, 0xc9, 0xcc, 0xbd, 0xf0, 0xd7, 0xea, 0x00, 0x00, 0x00, 0x02,
    ],
];

#[derive(Default)]
pub struct Metrics {
    pub requests: AtomicU64,
    pub pages: AtomicU64,
    pub retries: AtomicU64,
    pub bytes_received: AtomicU64,
    pub in_flight: AtomicUsize,
    pub peak_in_flight: AtomicUsize,
}
impl Metrics {
    pub fn enter(self: &Arc<Self>) -> FlightGuard {
        let now = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak_in_flight.fetch_max(now, Ordering::SeqCst);
        FlightGuard(self.clone())
    }
}
pub struct FlightGuard(Arc<Metrics>);
impl Drop for FlightGuard {
    fn drop(&mut self) {
        self.0.in_flight.fetch_sub(1, Ordering::SeqCst);
    }
}

pub struct RateLimiter {
    next: Mutex<Instant>,
    spacing: Duration,
}
impl RateLimiter {
    pub fn new(rps: f64) -> Self {
        Self {
            next: Mutex::new(Instant::now()),
            spacing: Duration::from_secs_f64(1.0 / rps),
        }
    }
    async fn send(&self, stream: &mut TcpStream, packet: &[u8], duration: Duration) -> Result<()> {
        let mut next = self.next.lock().await;
        if *next > Instant::now() {
            sleep_until(*next).await;
        }
        timeout(duration, stream.write_all(packet))
            .await
            .map_err(|_| FetchError::Network("send timeout".into()))?
            .map_err(|e| FetchError::Network(format!("send: {e}")))?;
        *next = Instant::now() + self.spacing;
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct WireExchange {
    pub request: Vec<u8>,
    pub response: Vec<u8>,
    pub host: String,
    pub elapsed_ms: u64,
}

pub struct Client {
    stream: TcpStream,
    duration: Duration,
    limiter: Arc<RateLimiter>,
    metrics: Arc<Metrics>,
    pub exchanges: Vec<WireExchange>,
    host: String,
    capture: bool,
}
impl Client {
    pub async fn connect(
        host: &Host,
        cfg: &Config,
        limiter: Arc<RateLimiter>,
        metrics: Arc<Metrics>,
    ) -> Result<Self> {
        let duration = Duration::from_secs_f64(cfg.timeout_secs);
        let address = format!("{}:{}", host.host, host.port);
        let stream = timeout(duration, TcpStream::connect(&address))
            .await
            .map_err(|_| FetchError::Network(format!("connect timeout: {address}")))?
            .map_err(|e| FetchError::Network(format!("connect {address}: {e}")))?;
        stream
            .set_nodelay(true)
            .map_err(|e| FetchError::Network(e.to_string()))?;
        let mut client = Self {
            stream,
            duration,
            limiter,
            metrics,
            exchanges: Vec::new(),
            host: address,
            capture: cfg.capture_wire,
        };
        for packet in SETUPS {
            client.exchange(packet).await?;
        }
        Ok(client)
    }
    pub async fn exchange(&mut self, packet: &[u8]) -> Result<Vec<u8>> {
        let started = Instant::now();
        self.limiter
            .send(&mut self.stream, packet, self.duration)
            .await?;
        self.metrics.requests.fetch_add(1, Ordering::Relaxed);
        let duration = self.duration;
        let result = timeout(duration, async {
            let mut head = [0u8; HEADER_LEN];
            self.stream
                .read_exact(&mut head)
                .await
                .map_err(|e| FetchError::Network(format!("response header incomplete: {e}")))?;
            let size = u16::from_le_bytes([head[12], head[13]]) as usize;
            if size == 0 {
                return Err(FetchError::Protocol("zero response wire length".into()));
            }
            let mut body = vec![0u8; size];
            self.stream
                .read_exact(&mut body)
                .await
                .map_err(|e| FetchError::Network(format!("response body incomplete: {e}")))?;
            let mut wire = head.to_vec();
            wire.extend_from_slice(&body);
            Ok(wire)
        })
        .await
        .map_err(|_| FetchError::Network("response timeout".into()))??;
        self.metrics
            .bytes_received
            .fetch_add(result.len() as u64, Ordering::Relaxed);
        if self.capture {
            self.exchanges.push(WireExchange {
                request: packet.to_vec(),
                response: result.clone(),
                host: self.host.clone(),
                elapsed_ms: started.elapsed().as_millis() as u64,
            });
        }
        decode_frame(&result)
    }
    pub async fn bars(
        &mut self,
        symbol: &Symbol,
        category: u16,
        start: u16,
        count: u16,
        index: bool,
    ) -> Result<Vec<RawBar>> {
        self.metrics.pages.fetch_add(1, Ordering::Relaxed);
        let packet = request(symbol.market, &symbol.code, category, start, count)?;
        let payload = self.exchange(&packet).await?;
        // Daily index frames add two breadth counters per record. Some hosts
        // omit them on intraday categories; both known layouts must consume
        // the entire payload, not silently ignore a suffix.
        if index && category != 9 {
            match decode_bars(&payload, category, false) {
                Ok(rows) => Ok(rows),
                Err(_) => decode_bars(&payload, category, true),
            }
        } else {
            decode_bars(&payload, category, index)
        }
    }
}

pub fn request(market: u16, code: &str, category: u16, start: u16, count: u16) -> Result<Vec<u8>> {
    if market > 1
        || code.len() != 6
        || !code.bytes().all(|b| b.is_ascii_digit())
        || count == 0
        || count > 800
        || !matches!(category, 0 | 8 | 9)
    {
        return Err(FetchError::Config("invalid TDX bars request".into()));
    }
    let mut out = Vec::with_capacity(38);
    out.extend_from_slice(&0x010cu16.to_le_bytes());
    out.extend_from_slice(&0x01016408u32.to_le_bytes());
    for value in [0x1cu16, 0x1c, 0x052d, market] {
        out.extend_from_slice(&value.to_le_bytes());
    }
    out.extend_from_slice(code.as_bytes());
    for value in [category, 1, start, count] {
        out.extend_from_slice(&value.to_le_bytes());
    }
    out.extend_from_slice(&[0u8; 10]);
    Ok(out)
}

pub fn decode_frame(wire: &[u8]) -> Result<Vec<u8>> {
    if wire.len() < HEADER_LEN {
        return Err(FetchError::Protocol(
            "response shorter than 16-byte header".into(),
        ));
    }
    let zip = u16::from_le_bytes([wire[12], wire[13]]) as usize;
    let plain = u16::from_le_bytes([wire[14], wire[15]]) as usize;
    if zip == 0 || plain == 0 || wire.len() != HEADER_LEN + zip {
        return Err(FetchError::Protocol(
            "response frame length mismatch".into(),
        ));
    }
    let bytes = &wire[HEADER_LEN..];
    if zip == plain {
        return Ok(bytes.to_vec());
    }
    let mut decoder = ZlibDecoder::new(Cursor::new(bytes));
    let mut out = Vec::with_capacity(plain);
    decoder
        .by_ref()
        .take(plain as u64 + 1)
        .read_to_end(&mut out)
        .map_err(|e| FetchError::Protocol(format!("zlib checksum/stream: {e}")))?;
    if out.len() != plain || decoder.total_in() != zip as u64 {
        return Err(FetchError::Protocol(
            "zlib output/consumed length mismatch".into(),
        ));
    }
    Ok(out)
}

struct Reader<'a> {
    bytes: &'a [u8],
    pos: usize,
}
impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        let end = self
            .pos
            .checked_add(n)
            .ok_or_else(|| FetchError::Protocol("cursor overflow".into()))?;
        let bytes = self
            .bytes
            .get(self.pos..end)
            .ok_or_else(|| FetchError::Protocol("truncated bar record".into()))?;
        self.pos = end;
        Ok(bytes)
    }
    fn u16(&mut self) -> Result<u16> {
        let b = self.take(2)?;
        Ok(u16::from_le_bytes([b[0], b[1]]))
    }
    fn u32(&mut self) -> Result<u32> {
        let b = self.take(4)?;
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }
    fn price(&mut self) -> Result<i64> {
        let first = self.take(1)?[0];
        let sign = first & 0x40 != 0;
        let mut value = (first & 0x3f) as u64;
        let mut byte = first;
        let mut shift = 6;
        while byte & 0x80 != 0 {
            if shift >= 63 {
                return Err(FetchError::Protocol("price varint overflow".into()));
            }
            byte = self.take(1)?[0];
            if (byte & 0x7f) as u64 > (u64::MAX >> shift) {
                return Err(FetchError::Protocol(
                    "price varint high bits overflow".into(),
                ));
            }
            value = value
                .checked_add(
                    ((byte & 0x7f) as u64)
                        .checked_shl(shift)
                        .ok_or_else(|| FetchError::Protocol("price shift overflow".into()))?,
                )
                .ok_or_else(|| FetchError::Protocol("price overflow".into()))?;
            shift += 7;
        }
        if value > i64::MAX as u64 {
            return Err(FetchError::Protocol("price exceeds i64".into()));
        }
        Ok(if sign { -(value as i64) } else { value as i64 })
    }
}

pub fn quantity(raw: u32) -> Result<f64> {
    if raw == 0 {
        return Ok(0.0);
    }
    let logpoint = (raw >> 24) as i32;
    let high = ((raw >> 16) & 255) as i32;
    let middle = ((raw >> 8) & 255) as f64;
    let low = (raw & 255) as f64;
    let exponent = logpoint * 2;
    let head = if high > 128 {
        2f64.powi(exponent - 134) * 128.0 + (high & 127) as f64 * 2f64.powi(exponent - 133)
    } else {
        2f64.powi(exponent - 134) * high as f64
    };
    let scale = if high & 128 != 0 { 2.0 } else { 1.0 };
    let value = 2f64.powi(exponent - 127)
        + head
        + scale * (2f64.powi(exponent - 142) * middle + 2f64.powi(exponent - 150) * low);
    if !value.is_finite() || raw & 0x80000000 != 0 {
        return Err(FetchError::Protocol("invalid wire quantity".into()));
    }
    Ok(if value.abs() < 1e-6 { 0.0 } else { value })
}

pub fn decode_bars(bytes: &[u8], category: u16, index: bool) -> Result<Vec<RawBar>> {
    let mut reader = Reader { bytes, pos: 0 };
    let count = reader.u16()? as usize;
    if count > 800 {
        return Err(FetchError::Protocol(
            "response count exceeds TDX page limit".into(),
        ));
    }
    let mut rows = Vec::with_capacity(count);
    let mut previous = 0i64;
    for _ in 0..count {
        let (year, month, day, hour, minute) = if category < 4 || category == 7 || category == 8 {
            let day = reader.u16()?;
            let clock = reader.u16()?;
            (
                ((day >> 11) + 2004) as i32,
                ((day % 2048) / 100) as u32,
                ((day % 2048) % 100) as u32,
                (clock / 60) as u32,
                (clock % 60) as u32,
            )
        } else {
            let date = reader.u32()?;
            (
                (date / 10000) as i32,
                (date % 10000 / 100),
                (date % 100),
                15,
                0,
            )
        };
        let stamp = NaiveDate::from_ymd_opt(year, month, day)
            .and_then(|d| d.and_hms_opt(hour, minute, 0))
            .ok_or_else(|| FetchError::Protocol("invalid bar datetime".into()))?;
        let open_delta = reader.price()?;
        let close_delta = reader.price()?;
        let high_delta = reader.price()?;
        let low_delta = reader.price()?;
        let volume = quantity(reader.u32()?)?;
        let amount = quantity(reader.u32()?)?;
        if index {
            reader.u16()?;
            reader.u16()?;
        }
        let open = previous
            .checked_add(open_delta)
            .ok_or_else(|| FetchError::Protocol("open delta overflow".into()))?;
        let close = open
            .checked_add(close_delta)
            .ok_or_else(|| FetchError::Protocol("close delta overflow".into()))?;
        let high = open
            .checked_add(high_delta)
            .ok_or_else(|| FetchError::Protocol("high delta overflow".into()))?;
        let low = open
            .checked_add(low_delta)
            .ok_or_else(|| FetchError::Protocol("low delta overflow".into()))?;
        previous = close;
        rows.push(RawBar {
            stamp,
            open: open as f64 / 1000.0,
            high: high as f64 / 1000.0,
            low: low as f64 / 1000.0,
            close: close as f64 / 1000.0,
            volume,
            amount,
        });
    }
    if reader.pos != bytes.len() {
        return Err(FetchError::Protocol(format!(
            "{} unconsumed bar bytes",
            bytes.len() - reader.pos
        )));
    }
    Ok(rows)
}
