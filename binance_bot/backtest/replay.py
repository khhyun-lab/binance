from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any

from binance_bot.market_data import MarketDataProvider
from binance_bot.services.binance_futures_service import AccountSnapshot, OrderExecution, SymbolRules

from .data_loader import Candle, CandleSeries


@dataclass(frozen=True)
class ReplayStep:
    timestamp: int
    candles: dict[str, Candle]


class HistoricalMarketDataProvider:
    def __init__(self, dataset: dict[str, dict[str, CandleSeries]]) -> None:
        self.dataset = dataset
        self.current_time_ms = 0
        self._rules: dict[str, SymbolRules] = {
            "BTCUSDT": SymbolRules(symbol="BTCUSDT", tick_size=Decimal("0.1"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional=Decimal("5")),
            "ETHUSDT": SymbolRules(symbol="ETHUSDT", tick_size=Decimal("0.01"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional=Decimal("5")),
            "SOLUSDT": SymbolRules(symbol="SOLUSDT", tick_size=Decimal("0.001"), step_size=Decimal("0.1"), min_qty=Decimal("0.1"), min_notional=Decimal("5")),
        }

    def set_current_time(self, timestamp: int) -> None:
        self.current_time_ms = timestamp

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        series = self._get_series(symbol, interval)
        available = [candle for candle in series.candles if candle.close_time <= self.current_time_ms]
        if len(available) < limit:
            raise ValueError(
                f"리플레이 warmup 부족 symbol={symbol} interval={interval} available={len(available)} required={limit} current_time={self.current_time_ms}"
            )
        selected = available[-limit:]
        return [
            {
                "open_time": candle.open_time,
                "open": Decimal(str(candle.open)),
                "high": Decimal(str(candle.high)),
                "low": Decimal(str(candle.low)),
                "close": Decimal(str(candle.close)),
                "volume": Decimal(str(candle.volume)),
            }
            for candle in selected
        ]

    async def get_mark_price(self, symbol: str) -> Decimal:
        candle = self._current_candle(symbol)
        return Decimal(str(candle.close))

    async def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        candle = self._current_candle(symbol)
        price = Decimal(str(candle.close))
        return price, price

    async def get_order_book(self, symbol: str, limit: int = 5) -> dict[str, list[tuple[Decimal, Decimal]]]:
        ask_price, bid_price = await self.get_book_ticker(symbol)
        return {
            "asks": [(ask_price, Decimal("1"))],
            "bids": [(bid_price, Decimal("1"))],
        }

    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        if symbol in self._rules:
            return self._rules[symbol]
        return SymbolRules(symbol=symbol, tick_size=Decimal("0.01"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional=Decimal("5"))

    async def get_position_risks(self, symbols: list[str]) -> list[Any]:
        return []

    async def get_user_trades(self, symbol: str, start_time_ms: int | None = None, limit: int = 200) -> list[Any]:
        return []

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        return None

    def normalize_price(self, rules: SymbolRules, price: Decimal, side: str) -> Decimal:
        step = rules.tick_size
        if step <= 0:
            return price
        units = price / step
        rounding = ROUND_CEILING if side == "BUY" else ROUND_DOWN
        return (units.quantize(Decimal("1"), rounding=rounding) * step).quantize(step)

    def normalize_quantity(self, rules: SymbolRules, quantity: Decimal) -> Decimal:
        step = rules.step_size
        if step <= 0:
            return quantity
        units = quantity / step
        return (units.quantize(Decimal("1"), rounding=ROUND_DOWN) * step).quantize(step)

    async def create_aggressive_limit_order(self, *args: Any, **kwargs: Any) -> OrderExecution | None:
        raise RuntimeError("백테스트 provider에서는 실주문 메서드를 호출할 수 없습니다.")

    async def create_market_order(self, *args: Any, **kwargs: Any) -> OrderExecution | None:
        raise RuntimeError("백테스트 provider에서는 실주문 메서드를 호출할 수 없습니다.")

    async def get_account_snapshot(self) -> AccountSnapshot:
        raise RuntimeError("백테스트 provider의 계좌 스냅샷은 runner가 직접 제공합니다.")

    def _current_candle(self, symbol: str) -> Candle:
        series = self._get_series(symbol, "1m")
        for candle in reversed(series.candles):
            if candle.close_time <= self.current_time_ms:
                return candle
        raise ValueError(f"현재 시점 이전 1m 캔들이 없습니다 symbol={symbol} current_time={self.current_time_ms}")

    def _get_series(self, symbol: str, interval: str) -> CandleSeries:
        if symbol not in self.dataset or interval not in self.dataset[symbol]:
            raise KeyError(f"리플레이 데이터가 없습니다 symbol={symbol} interval={interval}")
        return self.dataset[symbol][interval]


class CandleReplayEngine:
    def __init__(self, dataset: dict[str, dict[str, CandleSeries]], warmup_candles: int = 120) -> None:
        self.dataset = dataset
        self.warmup_candles = warmup_candles

    def iter_steps(self, symbols: list[str]) -> list[ReplayStep]:
        timestamps = sorted({candle.open_time for symbol in symbols for candle in self.dataset[symbol]["1m"].candles})
        steps: list[ReplayStep] = []
        for index, timestamp in enumerate(timestamps):
            if index < self.warmup_candles:
                continue
            candles: dict[str, Candle] = {}
            for symbol in symbols:
                series = self.dataset[symbol]["1m"].candles
                candle = next((item for item in series if item.open_time == timestamp), None)
                if candle is not None:
                    candles[symbol] = candle
            if candles:
                steps.append(ReplayStep(timestamp=timestamp, candles=candles))
        return steps