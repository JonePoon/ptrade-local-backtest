use chrono::NaiveDate;
use flate2::{Compression, write::ZlibEncoder};
use ptrade_fetch::config::{Dataset, Symbol};
use ptrade_fetch::data::{RawBar, in_session, normalize};
use ptrade_fetch::tdx::{SETUPS, decode_bars, decode_frame, quantity, request};
use std::io::Write;

fn frame(payload: &[u8], compressed: bool) -> Vec<u8> {
    let bytes = if compressed {
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(payload).unwrap();
        encoder.finish().unwrap()
    } else {
        payload.to_vec()
    };
    let mut out = vec![0; 16];
    out[12..14].copy_from_slice(&(bytes.len() as u16).to_le_bytes());
    out[14..16].copy_from_slice(&(payload.len() as u16).to_le_bytes());
    out.extend(bytes);
    out
}
fn varint(value: i64) -> Vec<u8> {
    let sign = value < 0;
    let mut value = value.unsigned_abs();
    let mut first = (value & 63) as u8;
    if sign {
        first |= 64;
    }
    value >>= 6;
    if value > 0 {
        first |= 128;
    }
    let mut out = vec![first];
    while value > 0 {
        let mut byte = (value & 127) as u8;
        value >>= 7;
        if value > 0 {
            byte |= 128;
        }
        out.push(byte);
    }
    out
}
fn daily_body() -> Vec<u8> {
    let mut out = 2u16.to_le_bytes().to_vec();
    let mut prior = 0;
    for (date, open, close, high, low, vol, amount) in [
        (20261105u32, 10000i64, 10020, 10040, 9990, 14f32, 1000f32),
        (20261106, 10030, 10025, 10050, 10000, 80., 800.),
    ] {
        out.extend(date.to_le_bytes());
        for value in [open - prior, close - open, high - open, low - open] {
            out.extend(varint(value));
        }
        out.extend(vol.to_bits().to_le_bytes());
        out.extend(amount.to_bits().to_le_bytes());
        prior = close;
    }
    out
}
#[test]
fn request_and_handshake_have_the_known_wire_shape() {
    let packet = request(0, "000001", 8, 800, 800).unwrap();
    assert_eq!(packet.len(), 38);
    assert_eq!(
        &packet[..14],
        &[0x0c, 1, 8, 0x64, 1, 1, 0x1c, 0, 0x1c, 0, 0x2d, 5, 0, 0]
    );
    assert_eq!(&packet[14..20], b"000001");
    assert_eq!(u16::from_le_bytes([packet[24], packet[25]]), 800);
    assert_eq!(SETUPS[0].len(), 13);
    assert_eq!(SETUPS[1].len(), 13);
    assert_eq!(SETUPS[2].len(), 42);
    assert!(request(2, "000001", 8, 0, 800).is_err());
    assert!(request(0, "bad", 8, 0, 800).is_err());
    assert!(request(0, "000001", 8, 0, 801).is_err());
}
#[test]
fn independent_raw_and_zlib_frames_decode_identically() {
    let body = daily_body();
    let raw = frame(&body, false);
    let compressed = frame(&body, true);
    assert_eq!(decode_frame(&raw).unwrap(), body);
    assert_eq!(decode_frame(&compressed).unwrap(), body);
    let bars = decode_bars(&body, 9, false).unwrap();
    assert_eq!(bars.len(), 2);
    assert_eq!(bars[0].open, 10.0);
    assert_eq!(bars[0].close, 10.02);
    assert_eq!(bars[1].open, 10.03);
    assert_eq!(bars[1].low, 10.0);
    assert_eq!(bars[0].volume, 14.0);
}
#[test]
fn quantity_negative_exponent_and_zero_are_not_inflated() {
    assert_eq!(quantity(0).unwrap(), 0.0);
    assert_eq!(quantity(0x41600000).unwrap(), 14.0);
    assert_eq!(quantity(0x42c80000).unwrap(), 100.0);
    for value in [1f32, 2., 14., 63., 80., 100., 59700., 88977784.] {
        assert_eq!(quantity(value.to_bits()).unwrap(), value as f64);
    }
    assert!(quantity((-1f32).to_bits()).is_err());
}
#[test]
fn wrong_length_truncation_checksum_count_and_tail_fail_closed() {
    assert!(decode_frame(&[0; 15]).is_err());
    let mut raw = frame(&daily_body(), false);
    raw.pop();
    assert!(decode_frame(&raw).is_err());
    let mut compressed = frame(&daily_body(), true);
    *compressed.last_mut().unwrap() ^= 1;
    assert!(decode_frame(&compressed).is_err());
    let mut output_length = frame(&daily_body(), true);
    output_length[14..16].copy_from_slice(&1u16.to_le_bytes());
    assert!(decode_frame(&output_length).is_err());
    assert!(decode_bars(&801u16.to_le_bytes(), 9, false).is_err());
    let mut body = daily_body();
    body.push(255);
    assert!(decode_bars(&body, 9, false).is_err());
    let mut body = daily_body();
    body.truncate(body.len() - 3);
    assert!(decode_bars(&body, 9, false).is_err());
}
#[test]
fn stock_daily_lots_and_minute_shares_use_different_boundaries() {
    let stamp = NaiveDate::from_ymd_opt(2026, 11, 6)
        .unwrap()
        .and_hms_opt(9, 35, 0)
        .unwrap();
    let row = RawBar {
        stamp,
        open: 10.,
        high: 10.1,
        low: 9.9,
        close: 10.,
        volume: 14.,
        amount: 14000.,
    };
    let date = stamp.date();
    assert_eq!(
        normalize(row.clone(), "000001.SZ", Dataset::Daily, date, date)
            .unwrap()
            .unwrap()
            .volume,
        1400
    );
    assert_eq!(
        normalize(row.clone(), "000001.SZ", Dataset::Minute, date, date)
            .unwrap()
            .unwrap()
            .volume,
        14
    );
    assert_eq!(
        normalize(row, "000001.SH", Dataset::Index, date, date)
            .unwrap()
            .unwrap()
            .volume,
        14
    );
}
#[test]
fn closing_session_excludes_lunch_padding_and_no_auction_bar_is_created() {
    let date = NaiveDate::from_ymd_opt(2026, 11, 6).unwrap();
    for (h, m, ok) in [
        (9, 30, false),
        (9, 31, true),
        (11, 30, true),
        (12, 0, false),
        (13, 0, false),
        (13, 1, true),
        (15, 0, true),
        (15, 1, false),
    ] {
        assert_eq!(
            in_session(date.and_hms_opt(h, m, 0).unwrap(), Dataset::Minute),
            ok
        );
    }
    assert!(!in_session(
        date.and_hms_opt(9, 31, 0).unwrap(),
        Dataset::Minute5
    ));
    assert!(in_session(
        date.and_hms_opt(9, 35, 0).unwrap(),
        Dataset::Minute5
    ));
    let mut row = RawBar {
        stamp: date.and_hms_opt(13, 0, 0).unwrap(),
        open: 10.,
        high: 10.,
        low: 10.,
        close: 10.,
        volume: 0.,
        amount: 0.,
    };
    assert!(
        normalize(row.clone(), "000001.SZ", Dataset::Minute, date, date)
            .unwrap()
            .is_none()
    );
    row.stamp = date.and_hms_opt(13, 1, 0).unwrap();
    assert_eq!(
        normalize(row, "000001.SZ", Dataset::Minute, date, date)
            .unwrap()
            .unwrap()
            .volume,
        0
    );
}
#[test]
fn symbol_exchange_is_authoritative_and_unknown_routes_fail() {
    assert_eq!(Symbol::parse("600000.SS").unwrap().symbol, "600000.SH");
    assert!(Symbol::parse("000001.SH").unwrap().is_index);
    assert!(!Symbol::parse("000001.SZ").unwrap().is_index);
    assert!(Symbol::parse("920001.BJ").is_err());
}

#[test]
fn index_extra_fields_and_packed_minute_date_are_consumed_exactly() {
    let mut indexed = 1u16.to_le_bytes().to_vec();
    indexed.extend(20261106u32.to_le_bytes());
    for delta in [10000i64, 20, 40, -10] {
        indexed.extend(varint(delta));
    }
    indexed.extend(14f32.to_bits().to_le_bytes());
    indexed.extend(1000f32.to_bits().to_le_bytes());
    indexed.extend([1, 0, 2, 0]);
    assert_eq!(decode_bars(&indexed, 9, true).unwrap()[0].close, 10.02);
    assert!(
        decode_bars(&indexed, 9, false).is_err(),
        "index breadth bytes must not be silently discarded"
    );
    let mut minute = 1u16.to_le_bytes().to_vec();
    minute.extend((((2026u16 - 2004) << 11) + 1106).to_le_bytes());
    minute.extend(571u16.to_le_bytes());
    for delta in [10000i64, 20, 40, -10] {
        minute.extend(varint(delta));
    }
    minute.extend(14f32.to_bits().to_le_bytes());
    minute.extend(1000f32.to_bits().to_le_bytes());
    let rows = decode_bars(&minute, 8, false).unwrap();
    assert_eq!(rows[0].stamp.to_string(), "2026-11-06 09:31:00");
}
#[test]
fn a_price_varint_must_not_discard_shifted_high_bits() {
    let mut payload = 1u16.to_le_bytes().to_vec();
    payload.extend(20261106u32.to_le_bytes());
    payload.extend([0x80; 9]);
    payload.push(4);
    payload.extend([0, 0, 0]);
    payload.extend([0; 8]);
    assert!(
        decode_bars(&payload, 9, false).is_err(),
        "high bits beyond u64 must fail, not become a zero-price padding row"
    );
}
