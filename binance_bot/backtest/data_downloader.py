from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp


BASE_URL = "https://fapi.binance.com"
KLINES_PATH = "/fapi/v1/klines"
DEFAULT_DATA_ROOT = Path("data/binance_futures/klines")
DEFAULT_INTERVALS = ("1m", "3m", "5m", "15m")
MAX_LIMIT = 1500
INTERVAL_TO_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
}


@dataclass(slots=True)
class DownloadRequest:
    symbols: list[str]
    intervals: list[str]
    start: str
    end: str
    data_root: Path = DEFAULT_DATA_ROOT


@dataclass(slots=True)
class DownloadSummary:
    symbol: str
    interval: str
    downloaded_candles: int
    first_timestamp: int | None
    last_timestamp: int | None
    gap_count: int


class HistoricalDataDownloader:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self._last_request_at = 0.0
        self._throttle_seconds = 0.25

    async def download(self, request: DownloadRequest) -> list[DownloadSummary]:
        start_ms = self._parse_start_date(request.start)
        end_ms = self._parse_end_date(request.end)
        self._validate_request(request, start_ms, end_ms)

        summaries: list[DownloadSummary] = []
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for symbol in request.symbols:
                for interval in request.intervals:
                    records = await self._download_symbol_interval(session, symbol, interval, start_ms, end_ms)
                    summary = self._merge_and_save(symbol, interval, records)
                    summaries.append(summary)
                    self._print_summary(summary)

        return summaries

    def _validate_request(self, request: DownloadRequest, start_ms: int, end_ms: int) -> None:
        if not request.symbols:
            raise ValueError("최소 한 개 이상의 심볼이 필요합니다.")
        if not request.intervals:
            raise ValueError("최소 한 개 이상의 인터벌이 필요합니다.")
        invalid_intervals = [interval for interval in request.intervals if interval not in INTERVAL_TO_MS]
        if invalid_intervals:
            raise ValueError(f"지원하지 않는 인터벌입니다: {','.join(invalid_intervals)}")
        if end_ms <= start_ms:
            raise ValueError("종료일은 시작일보다 뒤여야 합니다.")

    def _parse_start_date(self, value: str) -> int:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def _parse_end_date(self, value: str) -> int:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        return int(dt.timestamp() * 1000)

    async def _download_symbol_interval(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor = start_ms
        interval_ms = INTERVAL_TO_MS[interval]

        while cursor < end_ms:
            payload = await self._fetch_klines(session, symbol, interval, cursor, end_ms)
            if not payload:
                break

            batch = [self._normalize_row(row) for row in payload]
            records.extend(batch)
            last_open_time = int(batch[-1]["open_time"])
            next_cursor = last_open_time + interval_ms
            if next_cursor <= cursor:
                raise RuntimeError(f"캔들 다운로드 커서가 전진하지 않았습니다: {symbol} {interval}")
            cursor = next_cursor

            if len(payload) < MAX_LIMIT:
                break

        filtered = [record for record in records if start_ms <= int(record["open_time"]) < end_ms]
        return self._deduplicate_and_sort(filtered)

    async def _fetch_klines(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[list[Any]]:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": MAX_LIMIT,
        }
        backoff_seconds = 1.0
        last_error: Exception | None = None
        for _ in range(5):
            try:
                await self._throttle()
                async with session.get(f"{BASE_URL}{KLINES_PATH}", params=params) as response:
                    if response.status >= 400:
                        body = await response.text()
                        raise RuntimeError(f"Binance 공개 API 오류 status={response.status} body={body}")
                    payload = await response.json(content_type=None)
                    if not isinstance(payload, list):
                        raise RuntimeError(f"Binance 응답 형식이 예상과 다릅니다: {payload}")
                    return payload
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(backoff_seconds)
                backoff_seconds *= 2
        raise RuntimeError(f"Binance 캔들 다운로드 실패 symbol={symbol} interval={interval} start={start_ms} end={end_ms}: {last_error}")

    async def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_at
        if elapsed < self._throttle_seconds:
            await asyncio.sleep(self._throttle_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def _normalize_row(self, row: list[Any]) -> dict[str, Any]:
        return {
            "open_time": int(row[0]),
            "open": str(row[1]),
            "high": str(row[2]),
            "low": str(row[3]),
            "close": str(row[4]),
            "volume": str(row[5]),
            "close_time": int(row[6]),
            "quote_asset_volume": str(row[7]),
            "number_of_trades": int(row[8]),
            "taker_buy_base_volume": str(row[9]),
            "taker_buy_quote_volume": str(row[10]),
        }

    def _deduplicate_and_sort(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_open_time = {int(record["open_time"]): record for record in records}
        return [by_open_time[key] for key in sorted(by_open_time)]

    def _merge_and_save(self, symbol: str, interval: str, records: list[dict[str, Any]]) -> DownloadSummary:
        month_buckets: dict[str, dict[int, dict[str, Any]]] = {}
        for record in records:
            month_key = self._month_key(int(record["open_time"]))
            month_buckets.setdefault(month_key, {})[int(record["open_time"])] = record

        total_ordered: list[dict[str, Any]] = []
        for month_key, month_records in sorted(month_buckets.items()):
            file_path = self._month_file_path(symbol, interval, month_key)
            existing = self._load_existing_records(file_path)
            existing.update(month_records)
            ordered = [existing[key] for key in sorted(existing)]
            self._validate_chronological_order(ordered, interval, symbol, file_path)
            self._write_records(file_path, ordered)
            total_ordered.extend(ordered)

        total_ordered = self._deduplicate_and_sort(total_ordered)
        gap_count = self._count_gaps(total_ordered, interval)
        first_timestamp = int(total_ordered[0]["open_time"]) if total_ordered else None
        last_timestamp = int(total_ordered[-1]["open_time"]) if total_ordered else None
        return DownloadSummary(
            symbol=symbol,
            interval=interval,
            downloaded_candles=len(records),
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            gap_count=gap_count,
        )

    def _load_existing_records(self, file_path: Path) -> dict[int, dict[str, Any]]:
        if not file_path.exists():
            return {}
        records: dict[int, dict[str, Any]] = {}
        with file_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                records[int(record["open_time"])] = record
        return records

    def _write_records(self, file_path: Path, records: list[dict[str, Any]]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False))
                handle.write("\n")
        tmp_path.replace(file_path)

    def _validate_chronological_order(self, records: list[dict[str, Any]], interval: str, symbol: str, file_path: Path) -> None:
        if not records:
            return
        interval_ms = INTERVAL_TO_MS[interval]
        previous_open_time = int(records[0]["open_time"])
        for record in records[1:]:
            current_open_time = int(record["open_time"])
            if current_open_time <= previous_open_time:
                raise RuntimeError(f"캔들 시간 순서가 올바르지 않습니다: {symbol} {interval} {file_path}")
            if (current_open_time - previous_open_time) % interval_ms != 0:
                raise RuntimeError(f"캔들 간격이 잘못되었습니다: {symbol} {interval} {file_path}")
            previous_open_time = current_open_time

    def _count_gaps(self, records: list[dict[str, Any]], interval: str) -> int:
        if len(records) < 2:
            return 0
        interval_ms = INTERVAL_TO_MS[interval]
        gap_count = 0
        previous_open_time = int(records[0]["open_time"])
        for record in records[1:]:
            current_open_time = int(record["open_time"])
            if current_open_time - previous_open_time > interval_ms:
                gap_count += 1
            previous_open_time = current_open_time
        return gap_count

    def _month_key(self, timestamp_ms: int) -> str:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m")

    def _month_file_path(self, symbol: str, interval: str, month_key: str) -> Path:
        return self.data_root / symbol / interval / f"{month_key}.jsonl"

    def _print_summary(self, summary: DownloadSummary) -> None:
        first_value = self._format_timestamp(summary.first_timestamp)
        last_value = self._format_timestamp(summary.last_timestamp)
        print(
            f"symbol={summary.symbol} interval={summary.interval} candles_downloaded={summary.downloaded_candles} first={first_value} last={last_value} gaps_found={summary.gap_count}"
        )

    def _format_timestamp(self, timestamp_ms: int | None) -> str:
        if timestamp_ms is None:
            return "none"
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


async def download_historical_klines(request: DownloadRequest) -> list[DownloadSummary]:
    downloader = HistoricalDataDownloader(request.data_root)
    return await downloader.download(request)