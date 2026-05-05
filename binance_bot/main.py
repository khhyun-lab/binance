from __future__ import annotations

import argparse
import asyncio
import logging

from binance_bot.app import BinanceFuturesApp
from binance_bot.config import load_settings
from upbit_bot.logging_utils import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance 선물 자동매매 봇")
    parser.add_argument("--once", action="store_true", help="전략 사이클을 1회만 실행합니다.")
    parser.add_argument("--preflight", action="store_true", help="잔고 조회, 레버리지 설정, 롱/숏 테스트 주문을 점검합니다.")
    return parser.parse_args()


async def _run(run_once: bool, preflight: bool) -> None:
    settings = load_settings()
    setup_logging(settings.log_dir, settings.log_level, log_filename="binance_bot.log")
    logger = logging.getLogger("binance.main")
    if not settings.api_key or not settings.api_secret:
        logger.warning("Binance API 키가 비어 있습니다. dry-run 공개 시세 점검만 가능합니다.")
    app = BinanceFuturesApp(settings)
    if preflight:
        await app.run_preflight()
        return
    await app.run(run_once=run_once)


def main() -> None:
    args = parse_args()
    asyncio.run(_run(args.once, args.preflight))


if __name__ == "__main__":
    main()