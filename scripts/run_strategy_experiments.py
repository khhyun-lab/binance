from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from binance_bot.backtest.runner import BacktestConfig, run_backtest_sync


VARIANT_ENV: dict[str, dict[str, str]] = {
    "baseline": {},
    "pullback_conservative": {
        "BINANCE_FUTURES_PULLBACK_REACCEL_ENABLED": "true",
        "BINANCE_FUTURES_PULLBACK_REACCEL_MIN_SCORE": "6",
        "BINANCE_FUTURES_PULLBACK_REACCEL_VOLUME_RATIO": "1.05",
        "BINANCE_FUTURES_PULLBACK_MIN_RR": "1.70",
    },
    "pullback_balanced": {
        "BINANCE_FUTURES_PULLBACK_REACCEL_ENABLED": "true",
        "BINANCE_FUTURES_PULLBACK_REACCEL_MIN_SCORE": "5",
        "BINANCE_FUTURES_PULLBACK_REACCEL_VOLUME_RATIO": "0.95",
        "BINANCE_FUTURES_PULLBACK_MIN_RR": "1.50",
    },
    "pullback_aggressive": {
        "BINANCE_FUTURES_PULLBACK_REACCEL_ENABLED": "true",
        "BINANCE_FUTURES_PULLBACK_REACCEL_MIN_SCORE": "4",
        "BINANCE_FUTURES_PULLBACK_REACCEL_VOLUME_RATIO": "0.85",
        "BINANCE_FUTURES_PULLBACK_MIN_RR": "1.30",
    },
}


@contextmanager
def temporary_env(overrides: dict[str, str]):
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_variant(variant: str, base_config: BacktestConfig) -> Path:
    output_dir = base_config.output_dir / variant
    with temporary_env(VARIANT_ENV[variant]):
        result = run_backtest_sync(
            BacktestConfig(
                symbols=base_config.symbols,
                start=base_config.start,
                end=base_config.end,
                initial_balance=base_config.initial_balance,
                leverage=base_config.leverage,
                margin_per_trade=base_config.margin_per_trade,
                taker_fee=base_config.taker_fee,
                maker_fee=base_config.maker_fee,
                slippage_bps=base_config.slippage_bps,
                max_open_positions=base_config.max_open_positions,
                warmup_candles=base_config.warmup_candles,
                data_dir=base_config.data_dir,
                output_dir=output_dir,
                debug_decisions=True,
            )
        )
    return result.report_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Run grouped strategy experiment variants")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data/market"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/strategy_experiments"))
    parser.add_argument("--variants", nargs="+", default=list(VARIANT_ENV))
    parser.add_argument("--warmup-candles", type=int, default=1300)
    args = parser.parse_args()

    config = BacktestConfig(
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        warmup_candles=args.warmup_candles,
    )
    for variant in args.variants:
        report_dir = run_variant(variant, config)
        print(f"{variant}\t{report_dir}")


if __name__ == "__main__":
    main()