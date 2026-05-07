from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from binance_bot.services.binance_futures_service import BinanceFuturesService


class MarketDataProvider(Protocol):
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        ...

    async def get_mark_price(self, symbol: str) -> Decimal:
        ...

    async def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        ...

    async def get_order_book(self, symbol: str, limit: int = 5) -> dict[str, list[tuple[Decimal, Decimal]]]:
        ...


@dataclass(slots=True)
class LiveBinanceMarketDataProvider:
    service: BinanceFuturesService

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        return await self.service.get_klines(symbol, interval, limit)

    async def get_mark_price(self, symbol: str) -> Decimal:
        return await self.service.get_mark_price(symbol)

    async def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        return await self.service.get_book_ticker(symbol)

    async def get_order_book(self, symbol: str, limit: int = 5) -> dict[str, list[tuple[Decimal, Decimal]]]:
        ask_price, bid_price = await self.service.get_book_ticker(symbol)
        return {
            "asks": [(ask_price, Decimal("1"))],
            "bids": [(bid_price, Decimal("1"))],
        }

    def __getattr__(self, name: str) -> Any:
        return getattr(self.service, name)