use crate::error::{FetchError, Result};
use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Host {
    pub host: String,
    pub port: u16,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum HostInput {
    Pair((String, u16)),
    Object(Host),
    Text(String),
}

fn hosts<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Vec<Host>, D::Error> {
    Vec::<HostInput>::deserialize(deserializer)?
        .into_iter()
        .map(|value| match value {
            HostInput::Pair((host, port)) => Ok(Host { host, port }),
            HostInput::Object(host) => Ok(host),
            HostInput::Text(text) => {
                let (host, port) = text
                    .rsplit_once(':')
                    .ok_or_else(|| serde::de::Error::custom("host must be host:port"))?;
                let port = port.parse().map_err(serde::de::Error::custom)?;
                Ok(Host {
                    host: host.into(),
                    port,
                })
            }
        })
        .collect()
}
fn default_hosts() -> Vec<Host> {
    [
        ("120.76.1.198", 7709),
        ("1.202.143.37", 7709),
        ("111.203.134.118", 7709),
        ("117.133.128.226", 7709),
    ]
    .into_iter()
    .map(|(host, port)| Host {
        host: host.into(),
        port,
    })
    .collect()
}
fn provider() -> String {
    "tdx".into()
}
fn concurrency() -> usize {
    4
}
fn timeout() -> f64 {
    10.0
}
fn retries() -> usize {
    2
}
fn rps() -> f64 {
    4.0
}
fn max_pages() -> usize {
    64
}
fn page_size() -> usize {
    800
}
fn yes() -> bool {
    true
}
fn backoff() -> u64 {
    100
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Config {
    pub lake: PathBuf,
    pub start: NaiveDate,
    pub end: NaiveDate,
    #[serde(default)]
    pub symbols: Vec<String>,
    pub datasets: Vec<String>,
    #[serde(default = "provider")]
    pub provider: String,
    #[serde(default = "default_hosts", deserialize_with = "hosts")]
    pub tdx_hosts: Vec<Host>,
    #[serde(default = "concurrency")]
    pub concurrency: usize,
    #[serde(default = "timeout")]
    pub timeout_secs: f64,
    #[serde(default = "retries")]
    pub retries: usize,
    #[serde(default = "rps")]
    pub requests_per_second: f64,
    #[serde(default = "max_pages")]
    pub max_pages: usize,
    #[serde(default = "page_size")]
    pub page_size: usize,
    #[serde(default = "yes")]
    pub resume: bool,
    #[serde(default)]
    pub refresh: bool,
    #[serde(default)]
    pub capture_wire: bool,
    #[serde(default = "backoff")]
    pub retry_backoff_ms: u64,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, Eq, PartialEq, Ord, PartialOrd)]
pub enum Dataset {
    #[serde(rename = "daily_bars")]
    Daily,
    #[serde(rename = "index_bars")]
    Index,
    #[serde(rename = "minute_bars")]
    Minute,
    #[serde(rename = "minute_bars_5m")]
    Minute5,
}
impl Dataset {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "daily_bars" => Ok(Self::Daily),
            "index_bars" => Ok(Self::Index),
            "minute_bars" => Ok(Self::Minute),
            "minute_bars_5m" => Ok(Self::Minute5),
            _ => Err(FetchError::Config(format!(
                "unsupported dataset {value}; reference/adjustment datasets belong to the coordinator"
            ))),
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::Daily => "daily_bars",
            Self::Index => "index_bars",
            Self::Minute => "minute_bars",
            Self::Minute5 => "minute_bars_5m",
        }
    }
    pub fn frequency(self) -> &'static str {
        match self {
            Self::Daily | Self::Index => "1d",
            Self::Minute => "1m",
            Self::Minute5 => "5m",
        }
    }
    pub fn category(self) -> u16 {
        match self {
            Self::Daily | Self::Index => 9,
            Self::Minute => 8,
            Self::Minute5 => 0,
        }
    }
    pub fn intraday(self) -> bool {
        matches!(self, Self::Minute | Self::Minute5)
    }
    pub fn version(self) -> &'static str {
        if self == Self::Daily { "v2" } else { "v1" }
    }
    pub fn unit(self) -> &'static str {
        if self == Self::Index {
            "tdx_index_unverified"
        } else {
            "share"
        }
    }
    pub fn expected_bars(self) -> usize {
        match self {
            Self::Minute => 240,
            Self::Minute5 => 48,
            _ => 1,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
pub struct Symbol {
    pub symbol: String,
    pub code: String,
    pub market: u16,
    pub is_index: bool,
}
impl Symbol {
    pub fn parse(value: &str) -> Result<Self> {
        let upper = value.trim().to_ascii_uppercase();
        let (code, exchange) = upper.split_once('.').ok_or_else(|| {
            FetchError::Config(format!("symbol requires code.SH/code.SZ: {value}"))
        })?;
        if code.len() != 6 || !code.bytes().all(|c| c.is_ascii_digit()) {
            return Err(FetchError::Config(format!(
                "invalid six-digit symbol {value}"
            )));
        }
        let exchange = if exchange == "SS" { "SH" } else { exchange };
        let market = match exchange {
            "SH" => 1,
            "SZ" => 0,
            _ => {
                return Err(FetchError::Config(format!(
                    "TDX main-market route unavailable: {value}"
                )));
            }
        };
        let is_index = if market == 1 {
            code.starts_with("00") || code.starts_with("88") || code.starts_with("99")
        } else {
            code.starts_with("39")
        };
        Ok(Self {
            symbol: format!("{code}.{exchange}"),
            code: code.into(),
            market,
            is_index,
        })
    }
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let bytes = std::fs::read(path)
            .map_err(|e| FetchError::Config(format!("read config {}: {e}", path.display())))?;
        let mut cfg: Self = serde_json::from_slice(&bytes)
            .map_err(|e| FetchError::Config(format!("invalid JSON config: {e}")))?;
        if cfg.lake.is_relative() {
            cfg.lake = std::env::current_dir()
                .map_err(|e| FetchError::Io(e.to_string()))?
                .join(path.parent().unwrap_or(Path::new(".")))
                .join(&cfg.lake);
        }
        cfg.validate()?;
        Ok(cfg)
    }
    pub fn validate(&mut self) -> Result<()> {
        if self.start > self.end {
            return Err(FetchError::Config("start must not exceed end".into()));
        }
        if self.datasets.is_empty() {
            return Err(FetchError::Config("datasets must not be empty".into()));
        }
        if !matches!(self.provider.as_str(), "tdx" | "tdx_protocol") {
            return Err(FetchError::Config(
                "provider must be tdx or tdx_protocol".into(),
            ));
        }
        if !(1..=1024).contains(&self.concurrency)
            || !self.timeout_secs.is_finite()
            || !(0.001..=3600.0).contains(&self.timeout_secs)
            || !self.requests_per_second.is_finite()
            || !(0.001..=100_000.0).contains(&self.requests_per_second)
        {
            return Err(FetchError::Config(
                "invalid concurrency/timeout/requests_per_second".into(),
            ));
        }
        if self.retries > 32
            || self.retry_backoff_ms > 60_000
            || !(1..=800).contains(&self.page_size)
            || self.max_pages == 0
            || self
                .max_pages
                .saturating_sub(1)
                .saturating_mul(self.page_size)
                > u16::MAX as usize
        {
            return Err(FetchError::Config(
                "invalid retry/page budget; TDX start is u16, page count <=800".into(),
            ));
        }
        if self.tdx_hosts.is_empty()
            || self
                .tdx_hosts
                .iter()
                .any(|h| h.host.trim().is_empty() || h.port == 0)
        {
            return Err(FetchError::Config(
                "tdx_hosts must contain valid host/port".into(),
            ));
        }
        let mut datasets = Vec::new();
        for name in &self.datasets {
            let d = Dataset::parse(name)?;
            if !datasets.contains(&d.name().to_string()) {
                datasets.push(d.name().into());
            }
        }
        self.datasets = datasets;
        let mut symbols = std::collections::BTreeSet::new();
        for value in &self.symbols {
            symbols.insert(Symbol::parse(value)?.symbol);
        }
        self.symbols = symbols.into_iter().collect();
        Ok(())
    }
}
