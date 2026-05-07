from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from binance_bot.market_data import MarketDataProvider
from binance_bot.services.binance_futures_service import AccountSnapshot

from .indicators import atr, ema, rsi, sma
from .scoring import build_market_scores


@dataclass(slots=True)
class MarketSnapshot:
    symbol: str
    mark_price: Decimal
    ask_price: Decimal
    bid_price: Decimal
    ema_fast_1m: Decimal
    ema_slow_1m: Decimal
    long_score: int
    short_score: int
    long_reasons: list[str]
    short_reasons: list[str]
    volume_ratio: Decimal
    rsi_1m: Decimal
    rsi_3m: Decimal
    atr_3m: Decimal
    atr_15m: Decimal
    recent_high: Decimal
    recent_low: Decimal
    breakout_high: Decimal
    breakout_low: Decimal
    trend_long_ok: bool
    trend_short_ok: bool

    @property
    def preferred_side(self) -> str | None:
        if self.long_score > self.short_score and self.long_score > 0:
            return "LONG"
        if self.short_score > self.long_score and self.short_score > 0:
            return "SHORT"
        return None


def build_market_snapshot(
    symbol: str,
    klines_1m: list[dict[str, Decimal]],
    klines_3m: list[dict[str, Decimal]],
    klines_5m: list[dict[str, Decimal]],
    klines_15m: list[dict[str, Decimal]],
    mark_price: Decimal,
    ask_price: Decimal,
    bid_price: Decimal,
) -> MarketSnapshot:
    closes_1m = [row["close"] for row in klines_1m]
    highs_1m = [row["high"] for row in klines_1m]
    lows_1m = [row["low"] for row in klines_1m]
    volumes_1m = [row["volume"] for row in klines_1m]
    closes_3m = [row["close"] for row in klines_3m]
    closes_5m = [row["close"] for row in klines_5m]
    closes_15m = [row["close"] for row in klines_15m]

    ema9_1m = ema(closes_1m, 9)
    ema21_1m = ema(closes_1m, 21)
    ema9_5m = ema(closes_5m, 9)
    ema21_5m = ema(closes_5m, 21)
    ema50_5m = ema(closes_5m, 50)
    ema21_15m = ema(closes_15m, 21)
    ema50_15m = ema(closes_15m, 50)
    rsi_1m = rsi(closes_1m, 14)
    rsi_3m = rsi(closes_3m, 14)
    atr_3m = atr(
        [row["high"] for row in klines_3m],
        [row["low"] for row in klines_3m],
        closes_3m,
        14,
    )
    atr_5m = atr(
        [row["high"] for row in klines_5m],
        [row["low"] for row in klines_5m],
        closes_5m,
        14,
    )
    recent_high = max(highs_1m[-15:])
    recent_low = min(lows_1m[-15:])
    breakout_high = max(highs_1m[-31:-1])
    breakout_low = min(lows_1m[-31:-1])
    average_volume = sma(volumes_1m[-31:-1], 20)
    volume_ratio = Decimal("0") if average_volume <= 0 else volumes_1m[-1] / average_volume

    trend_long_ok = ema9_5m > ema21_5m > ema50_5m and closes_15m[-1] > ema21_15m > ema50_15m
    trend_short_ok = ema9_5m < ema21_5m < ema50_5m and closes_15m[-1] < ema21_15m < ema50_15m
    long_score, short_score, long_reasons, short_reasons = build_market_scores(
        mark_price=mark_price,
        breakout_high=breakout_high,
        breakout_low=breakout_low,
        ema_fast_1m=ema9_1m,
        ema_slow_1m=ema21_1m,
        latest_close_1m=closes_1m[-1],
        previous_close_1m=closes_1m[-2],
        rsi_1m=rsi_1m,
        volume_ratio=volume_ratio,
        trend_long_ok=trend_long_ok,
        trend_short_ok=trend_short_ok,
    )

    return MarketSnapshot(
        symbol=symbol,
        mark_price=mark_price,
        ask_price=ask_price,
        bid_price=bid_price,
        ema_fast_1m=ema9_1m,
        ema_slow_1m=ema21_1m,
        long_score=long_score,
        short_score=short_score,
        long_reasons=long_reasons,
        short_reasons=short_reasons,
        volume_ratio=volume_ratio,
        rsi_1m=rsi_1m,
        rsi_3m=rsi_3m,
        atr_3m=atr_3m,
        atr_15m=atr_5m,
        recent_high=recent_high,
        recent_low=recent_low,
        breakout_high=breakout_high,
        breakout_low=breakout_low,
        trend_long_ok=trend_long_ok,
        trend_short_ok=trend_short_ok,
    )


class SnapshotMixin:
    async def _get_account_snapshot(self) -> AccountSnapshot:
        if not self.settings.api_key or not self.settings.api_secret:
            simulated_balance = self.settings.margin_per_trade_usdt * Decimal(self.settings.max_open_positions)
            return AccountSnapshot(available_balance=simulated_balance, wallet_balance=simulated_balance)
        return await self.service.get_account_snapshot()

    async def _build_snapshot(self, symbol: str) -> MarketSnapshot:
        klines_1m = await self.service.get_klines(symbol, self.settings.entry_interval, self.settings.candle_count)
        klines_3m = await self.service.get_klines(symbol, "3m", self.settings.candle_count)
        klines_5m = await self.service.get_klines(symbol, self.settings.trend_interval, self.settings.candle_count)
        klines_15m = await self.service.get_klines(symbol, self.settings.context_interval, 80)
        ask_price, bid_price = await self.service.get_book_ticker(symbol)
        mark_price = await self.service.get_mark_price(symbol)
        snapshot = build_market_snapshot(
            symbol=symbol,
            klines_1m=klines_1m,
            klines_3m=klines_3m,
            klines_5m=klines_5m,
            klines_15m=klines_15m,
            mark_price=mark_price,
            ask_price=ask_price,
            bid_price=bid_price,
        )

        self.logger.info(
            "Binance 시장상태 symbol=%s mark_price=%s long_score=%s short_score=%s rsi_1m=%s rsi_3m=%s volume_ratio=%s trend_long_ok=%s trend_short_ok=%s breakout_high=%s breakout_low=%s",
            symbol,
            self._format_decimal(snapshot.mark_price, "0.0000"),
            snapshot.long_score,
            snapshot.short_score,
            self._format_decimal(snapshot.rsi_1m, "0.00"),
            self._format_decimal(snapshot.rsi_3m, "0.00"),
            self._format_decimal(snapshot.volume_ratio, "0.00"),
            snapshot.trend_long_ok,
            snapshot.trend_short_ok,
            self._format_decimal(snapshot.breakout_high, "0.0000"),
            self._format_decimal(snapshot.breakout_low, "0.0000"),
        )
        return snapshot