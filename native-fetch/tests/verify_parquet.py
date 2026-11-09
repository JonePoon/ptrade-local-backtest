"""Read-only cross-reader verification. This file does not download or run a strategy."""
import argparse
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def verify(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert report["status"] == "succeeded", report["errors"]
    totals = {}
    records = []
    for item in report["files"]:
        path = Path(item["path"])
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        table = pq.read_table(path)
        assert table.num_rows == item["rows"]
        assert table.schema.field("trade_date").type == pa.date32()
        assert table.schema.field("volume").type == pa.int64()
        assert table.schema.field("fetched_at").type == pa.timestamp("us", tz="UTC")
        data = table.to_pydict()
        dataset = item["dataset"]
        assert set(data["data_version"]) == ({"v2"} if dataset == "daily_bars" else {"v1"})
        assert set(data["symbol"]) == {item["symbol"]}
        assert set(data["source"]) == {"tdx_protocol"}
        for date in data["trade_date"]:
            assert item["start"] <= date.isoformat() <= item["end"]
        if dataset.startswith("minute_bars"):
            assert table.schema.field("bar_time").type == pa.timestamp("us")
            assert all(stamp.date() == date for stamp, date in zip(data["bar_time"], data["trade_date"]))
            assert all((9, 31) <= (s.hour, s.minute) <= (11, 30) or (13, 1) <= (s.hour, s.minute) <= (15, 0) for s in data["bar_time"])
            assert (9, 30) not in {(s.hour, s.minute) for s in data["bar_time"]}
            assert (13, 0) not in {(s.hour, s.minute) for s in data["bar_time"]}
        else:
            assert all(v is None for v in data["pre_close"])
        for op, hi, lo, cl in zip(data["open"], data["high"], data["low"], data["close"]):
            assert 0 < lo <= min(op, cl) <= max(op, cl) <= hi
        total_volume = sum(data["volume"])
        total_amount = sum(data["amount"])
        totals[(item["symbol"], dataset)] = (total_volume, total_amount)
        records.append({"dataset": dataset, "symbol": item["symbol"], "rows": table.num_rows, "volume": total_volume, "amount": total_amount, "sha256": item["sha256"]})
    reconciliation = []
    for symbol in sorted({s for s, _ in totals}):
        if (symbol, "daily_bars") in totals:
            daily = totals[(symbol, "daily_bars")]
            for dataset in ("minute_bars", "minute_bars_5m"):
                if (symbol, dataset) in totals:
                    minute = totals[(symbol, dataset)]
                    reconciliation.append({"symbol": symbol, "dataset": dataset, "daily_volume": daily[0], "minute_volume": minute[0], "volume_ratio": minute[0] / daily[0] if daily[0] else None, "daily_amount": daily[1], "minute_amount": minute[1]})
    return {"reader": "pyarrow", "native_downloader": "ptrade-fetch", "status": "succeeded", "rows": sum(r["rows"] for r in records), "files": records, "reconciliation": reconciliation}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = verify(args.report)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
