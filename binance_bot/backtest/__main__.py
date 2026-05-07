from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path

from .data_downloader import DownloadRequest, download_historical_klines
from .runner import BacktestConfig, run_backtest


def _parse_symbols(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _parse_intervals(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance 선물 백테스트 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    download_parser = subparsers.add_parser("download", help="과거 선물 캔들 데이터를 다운로드합니다.")
    download_parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT", help="쉼표로 구분한 심볼 목록")
    download_parser.add_argument("--intervals", default="1m,3m,5m,15m", help="쉼표로 구분한 인터벌 목록")
    download_parser.add_argument("--start", required=True, help="시작일 YYYY-MM-DD")
    download_parser.add_argument("--end", required=True, help="종료일 YYYY-MM-DD")

    run_parser = subparsers.add_parser("run", help="로컬 캔들 데이터로 백테스트를 실행합니다.")
    run_parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    run_parser.add_argument("--start", required=True)
    run_parser.add_argument("--end", required=True)
    run_parser.add_argument("--initial-balance", default="1000")
    run_parser.add_argument("--leverage", default="7")
    run_parser.add_argument("--margin-per-trade", default="20")
    run_parser.add_argument("--taker-fee", default="0.0004")
    run_parser.add_argument("--maker-fee", default="0.0002")
    run_parser.add_argument("--slippage-bps", default="2")
    run_parser.add_argument("--max-open-positions", default="2")
    run_parser.add_argument("--warmup-candles", default="120")
    run_parser.add_argument("--data-dir", default="data/binance_futures/klines")
    run_parser.add_argument("--output-dir", default="reports/backtests")
    run_parser.add_argument("--debug-decisions", action="store_true")

    replay_parser = subparsers.add_parser("replay", help="특정 심볼 구간을 디버그 리플레이합니다.")
    replay_parser.add_argument("--symbol", required=True)
    replay_parser.add_argument("--start", required=True)
    replay_parser.add_argument("--end", required=True)
    replay_parser.add_argument("--initial-balance", default="1000")
    replay_parser.add_argument("--leverage", default="7")
    replay_parser.add_argument("--margin-per-trade", default="20")
    replay_parser.add_argument("--taker-fee", default="0.0004")
    replay_parser.add_argument("--maker-fee", default="0.0002")
    replay_parser.add_argument("--slippage-bps", default="2")
    replay_parser.add_argument("--max-open-positions", default="1")
    replay_parser.add_argument("--warmup-candles", default="120")
    replay_parser.add_argument("--data-dir", default="data/binance_futures/klines")
    replay_parser.add_argument("--output-dir", default="reports/replay")
    replay_parser.add_argument("--debug-decisions", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "download":
        request = DownloadRequest(
            symbols=_parse_symbols(args.symbols),
            intervals=_parse_intervals(args.intervals),
            start=args.start,
            end=args.end,
        )
        asyncio.run(download_historical_klines(request))
        return

    if args.command in {"run", "replay"}:
        symbols = _parse_symbols(args.symbols) if args.command == "run" else [args.symbol.strip().upper()]
        config = BacktestConfig(
            symbols=symbols,
            start=args.start,
            end=args.end,
            initial_balance=Decimal(args.initial_balance),
            leverage=int(args.leverage),
            margin_per_trade=Decimal(args.margin_per_trade),
            taker_fee=Decimal(args.taker_fee),
            maker_fee=Decimal(args.maker_fee),
            slippage_bps=Decimal(args.slippage_bps),
            max_open_positions=int(args.max_open_positions),
            warmup_candles=int(args.warmup_candles),
            data_dir=Path(args.data_dir),
            output_dir=Path(args.output_dir),
            debug_decisions=bool(args.debug_decisions),
        )
        result = asyncio.run(run_backtest(config))
        print(f"report_dir={result.report_dir}")
        print(f"trade_count={result.metrics.trade_count} final_balance={result.metrics.final_balance} net_pnl={result.metrics.net_pnl}")


if __name__ == "__main__":
    main()