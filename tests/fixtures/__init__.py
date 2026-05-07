from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def write_fixture_dataset(root: Path, symbol: str = "BTCUSDT", start: str = "2025-01-01", minutes: int = 1600) -> None:
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    candles_1m = []
    price = 100.0
    for index in range(minutes):
        open_time = int((start_dt + timedelta(minutes=index)).timestamp() * 1000)
        close_time = open_time + 59_999
        drift = 0.05 if index % 40 < 20 else -0.03
        open_price = price
        close_price = max(1.0, price + drift)
        high = max(open_price, close_price) + 0.1
        low = min(open_price, close_price) - 0.1
        price = close_price
        candles_1m.append(
            {
                "open_time": open_time,
                "open": f"{open_price:.4f}",
                "high": f"{high:.4f}",
                "low": f"{low:.4f}",
                "close": f"{close_price:.4f}",
                "volume": "10.0",
                "close_time": close_time,
                "quote_asset_volume": "1000.0",
                "number_of_trades": 100,
                "taker_buy_base_volume": "5.0",
                "taker_buy_quote_volume": "500.0",
            }
        )
    _write_month_jsonl(root / symbol / "1m" / f"{start_dt.strftime('%Y-%m')}.jsonl", candles_1m)
    for interval, chunk in (("3m", 3), ("5m", 5), ("15m", 15)):
        aggregated = _aggregate(candles_1m, chunk)
        _write_month_jsonl(root / symbol / interval / f"{start_dt.strftime('%Y-%m')}.jsonl", aggregated)


def _aggregate(rows: list[dict[str, object]], size: int) -> list[dict[str, object]]:
    aggregated: list[dict[str, object]] = []
    for start_index in range(0, len(rows), size):
        bucket = rows[start_index : start_index + size]
        if len(bucket) < size:
            break
        aggregated.append(
            {
                "open_time": bucket[0]["open_time"],
                "open": bucket[0]["open"],
                "high": max(float(item["high"]) for item in bucket),
                "low": min(float(item["low"]) for item in bucket),
                "close": bucket[-1]["close"],
                "volume": sum(float(item["volume"]) for item in bucket),
                "close_time": bucket[-1]["close_time"],
                "quote_asset_volume": sum(float(item["quote_asset_volume"]) for item in bucket),
                "number_of_trades": sum(int(item["number_of_trades"]) for item in bucket),
                "taker_buy_base_volume": sum(float(item["taker_buy_base_volume"]) for item in bucket),
                "taker_buy_quote_volume": sum(float(item["taker_buy_quote_volume"]) for item in bucket),
            }
        )
    return aggregated


def _write_month_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")