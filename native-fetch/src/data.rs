use crate::config::Dataset;
use crate::error::{FetchError, Result};
use chrono::{NaiveDate, NaiveDateTime, Timelike};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct RawBar {
    pub stamp: NaiveDateTime,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub amount: f64,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Bar {
    pub symbol: String,
    pub stamp: NaiveDateTime,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: i64,
    pub amount: f64,
}

pub fn in_session(stamp: NaiveDateTime, dataset: Dataset) -> bool {
    let minute = stamp.hour() * 60 + stamp.minute();
    let session = (571..=690).contains(&minute) || (781..=900).contains(&minute);
    session && (dataset != Dataset::Minute5 || minute % 5 == 0) && stamp.second() == 0
}

pub fn normalize(
    raw: RawBar,
    symbol: &str,
    dataset: Dataset,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Option<Bar>> {
    if raw.stamp.date() < start || raw.stamp.date() > end {
        return Ok(None);
    }
    if dataset.intraday() && !in_session(raw.stamp, dataset) {
        return Ok(None);
    }
    if ![
        raw.open, raw.high, raw.low, raw.close, raw.volume, raw.amount,
    ]
    .into_iter()
    .all(f64::is_finite)
    {
        return Err(FetchError::Protocol("nonfinite OHLC/quantity".into()));
    }
    if [raw.open, raw.high, raw.low, raw.close]
        .into_iter()
        .any(|v| v <= 0.0)
    {
        return Ok(None);
    }
    if raw.volume < 0.0
        || raw.amount < 0.0
        || raw.low > raw.high
        || raw.open < raw.low - 1e-8
        || raw.open > raw.high + 1e-8
        || raw.close < raw.low - 1e-8
        || raw.close > raw.high + 1e-8
    {
        return Err(FetchError::Protocol("invalid OHLC/quantity domain".into()));
    }
    let number = if raw.volume.abs() < 1e-6 {
        0.0
    } else {
        raw.volume
    };
    let limit = if dataset == Dataset::Daily {
        (i64::MAX / 100) as f64
    } else {
        i64::MAX as f64
    };
    if number >= limit || number.fract() != 0.0 {
        return Err(FetchError::Protocol(
            "volume is not an exact nonnegative int64".into(),
        ));
    }
    let mut volume = number as i64;
    if dataset == Dataset::Daily {
        if volume == 0 && raw.amount > 0.0 {
            return Err(FetchError::Protocol(
                "daily lot-rounded zero with positive turnover is not a suspension".into(),
            ));
        }
        volume = volume
            .checked_mul(100)
            .ok_or_else(|| FetchError::Protocol("daily volume overflow".into()))?;
    }
    Ok(Some(Bar {
        symbol: symbol.into(),
        stamp: raw.stamp,
        open: raw.open,
        high: raw.high,
        low: raw.low,
        close: raw.close,
        volume,
        amount: if raw.amount.abs() < 1e-6 {
            0.0
        } else {
            raw.amount
        },
    }))
}
