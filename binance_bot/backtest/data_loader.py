from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


INTERVAL_TO_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
}
DEFAULT_DATA_DIR = Path("data/binance_futures/klines")


@dataclass(frozen=True)
class Candle:
    symbol: str
    interval: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_asset_volume: float = 0.0
    number_of_trades: int = 0
    taker_buy_base_volume: float = 0.0
    taker_buy_quote_volume: float = 0.0


@dataclass(frozen=True)
class CandleSeries:
    symbol: str
    interval: str
    candles: tuple[Candle, ...]
    gaps: tuple[tuple[int, int], ...]


class DataValidationError(ValueError):
    pass


def load_candle_dataset(
    symbols: list[str],
    intervals: list[str],
    start: str,
    end: str,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, dict[str, CandleSeries]]:
    dataset: dict[str, dict[str, CandleSeries]] = {}
    for symbol in symbols:
        dataset[symbol] = {}
        for interval in intervals:
            dataset[symbol][interval] = load_candle_series(symbol, interval, start, end, data_dir=data_dir)
    return dataset


def load_candle_series(symbol: str, interval: str, start: str, end: str, data_dir: Path = DEFAULT_DATA_DIR) -> CandleSeries:
    if interval not in INTERVAL_TO_MS:
        raise DataValidationError(f"지원하지 않는 인터벌입니다: {interval}")

    start_ms = _parse_start_date(start)
    end_ms = _parse_end_date(end)
    months = _month_range(start_ms, end_ms)
    candles: list[Candle] = []
    seen_timestamps: set[int] = set()
    loaded_files = 0

    for month_key in months:
        file_path = data_dir / symbol / interval / f"{month_key}.jsonl"
        if not file_path.exists():
            continue
        loaded_files += 1
        with file_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DataValidationError(f"JSONL 파싱 실패 file={file_path} line={line_number}: {exc}") from exc
                candle = _parse_candle(symbol, interval, payload, file_path, line_number)
                if candle.open_time < start_ms or candle.open_time >= end_ms:
                    continue
                if candle.open_time in seen_timestamps:
                    raise DataValidationError(f"중복 캔들 timestamp 감지 symbol={symbol} interval={interval} open_time={candle.open_time}")
                seen_timestamps.add(candle.open_time)
                candles.append(candle)

    if loaded_files == 0:
        raise FileNotFoundError(
            f"로컬 캔들 데이터가 없습니다 symbol={symbol} interval={interval} data_dir={data_dir} start={start} end={end}"
        )
    if not candles:
        raise DataValidationError(f"요청한 범위에 캔들이 없습니다 symbol={symbol} interval={interval} start={start} end={end}")

    candles.sort(key=lambda candle: candle.open_time)
    gaps = _validate_interval_consistency(symbol, interval, candles)
    return CandleSeries(symbol=symbol, interval=interval, candles=tuple(candles), gaps=tuple(gaps))


def _parse_candle(symbol: str, interval: str, payload: dict[str, object], file_path: Path, line_number: int) -> Candle:
    required_fields = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
    ]
    for field in required_fields:
        if field not in payload:
            raise DataValidationError(f"필수 필드 누락 file={file_path} line={line_number} field={field}")
    try:
        return Candle(
            symbol=symbol,
            interval=interval,
            open_time=int(payload["open_time"]),
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
            volume=float(payload["volume"]),
            close_time=int(payload["close_time"]),
            quote_asset_volume=float(payload.get("quote_asset_volume", 0.0)),
            number_of_trades=int(payload.get("number_of_trades", 0)),
            taker_buy_base_volume=float(payload.get("taker_buy_base_volume", 0.0)),
            taker_buy_quote_volume=float(payload.get("taker_buy_quote_volume", 0.0)),
        )
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"캔들 필드 변환 실패 file={file_path} line={line_number}: {exc}") from exc


def _validate_interval_consistency(symbol: str, interval: str, candles: list[Candle]) -> list[tuple[int, int]]:
    interval_ms = INTERVAL_TO_MS[interval]
    gaps: list[tuple[int, int]] = []
    previous = candles[0]
    for candle in candles[1:]:
        delta = candle.open_time - previous.open_time
        if delta <= 0:
            raise DataValidationError(
                f"캔들 정렬이 올바르지 않습니다 symbol={symbol} interval={interval} previous={previous.open_time} current={candle.open_time}"
            )
        if delta % interval_ms != 0:
            raise DataValidationError(
                f"캔들 간격이 인터벌과 맞지 않습니다 symbol={symbol} interval={interval} previous={previous.open_time} current={candle.open_time} delta_ms={delta}"
            )
        if delta > interval_ms:
            gaps.append((previous.open_time, candle.open_time))
        previous = candle
    return gaps


def _parse_start_date(value: str) -> int:
    dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _parse_end_date(value: str) -> int:
    dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _month_range(start_ms: int, end_ms: int) -> list[str]:
    current = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_dt = datetime.fromtimestamp((end_ms - 1) / 1000, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months: list[str] = []
    while current <= end_dt:
        months.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months