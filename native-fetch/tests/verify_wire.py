"""Offline SDK parser holdout against native captured frames; no network operations."""
import argparse
import hashlib
import json
import struct
import zlib
from datetime import date
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from cnequity.adapters.tdx_protocol._wire.parser.std.get_security_bars import GetSecurityBarsCmd
from cnequity.adapters.tdx_protocol.bars import _parse_bar_rows
from cnequity.adapters.tdx_protocol.minute_bars import _rows_to_dicts


def verify(path):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    file_index = {(f["dataset"], f["symbol"]): f for f in report["files"]}
    request_index = {Path(item["path"]).name: item for item in report["wire"] if item["direction"] == "request"}
    matched = []
    for response in report["wire"]:
        if response["direction"] != "response":
            continue
        request_name = Path(response["path"]).name.replace("-response.bin", "-request.bin")
        request_record = request_index[request_name]
        request = Path(request_record["path"]).read_bytes()
        if len(request) != 38:
            continue
        wire = Path(response["path"]).read_bytes()
        assert hashlib.sha256(wire).hexdigest() == response["sha256"]
        zipped, plain = struct.unpack_from("<HH", wire, 12)
        assert len(wire) == 16 + zipped
        payload = zlib.decompress(wire[16:]) if zipped != plain else wire[16:]
        assert len(payload) == plain
        market = struct.unpack_from("<H", request, 12)[0]
        code = request[14:20].decode("ascii")
        category = struct.unpack_from("<H", request, 20)[0]
        symbol = code + (".SH" if market == 1 else ".SZ")
        parser = GetSecurityBarsCmd(None)
        parser.category = category
        decoded = parser.parseResponse(payload)
        if category == 9:
            dataset = "daily_bars"
            item = file_index[(dataset, symbol)]
            rows = _parse_bar_rows(pl.DataFrame(decoded), symbol, date.fromisoformat(item["start"]), date.fromisoformat(item["end"]))
            key = lambda row: row["trade_date"]
        else:
            dataset = "minute_bars" if category == 8 else "minute_bars_5m"
            item = file_index[(dataset, symbol)]
            rows, excluded = _rows_to_dicts(decoded, symbol, "1m" if category == 8 else "5m", date.fromisoformat(item["start"]), date.fromisoformat(item["end"]))
            assert excluded == 0
            key = lambda row: row["bar_time"]
        native = pq.read_table(Path(item["path"])).to_pylist()
        sdk = {key(row): row for row in rows}
        assert len(native) == len(sdk) == item["rows"]
        for row in native:
            expected = sdk[key(row)]
            for column in ("open", "high", "low", "close", "volume", "amount"):
                assert row[column] == expected[column], (symbol, dataset, key(row), column, row[column], expected[column])
        matched.append({"dataset": dataset, "symbol": symbol, "native_rows": len(native), "source_page_rows": len(decoded), "sha256": response["sha256"], "exact_columns": ["open", "high", "low", "close", "volume", "amount"]})
    assert len(matched) == len(report["files"])
    return {"status": "succeeded", "source": "offline public CNE SDK parser", "network_operations": 0, "rows": sum(item["native_rows"] for item in matched), "matches": matched}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = verify(args.report)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
