from __future__ import annotations

from decimal import Decimal


def build_market_scores(
    mark_price: Decimal,
    breakout_high: Decimal,
    breakout_low: Decimal,
    ema_fast_1m: Decimal,
    ema_slow_1m: Decimal,
    latest_close_1m: Decimal,
    previous_close_1m: Decimal,
    rsi_1m: Decimal,
    volume_ratio: Decimal,
    trend_long_ok: bool,
    trend_short_ok: bool,
) -> tuple[int, int, list[str], list[str]]:
    long_score = 0
    short_score = 0
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    if trend_long_ok:
        long_score += 2
        long_reasons.append("5분/15분하모니상승+2")
    if trend_short_ok:
        short_score += 2
        short_reasons.append("5분/15분하모니하락+2")
    if mark_price >= breakout_high:
        long_score += 2
        long_reasons.append("30봉상단돌파+2")
    if mark_price <= breakout_low:
        short_score += 2
        short_reasons.append("30봉하단이탈+2")
    if ema_fast_1m > ema_slow_1m and latest_close_1m > ema_fast_1m:
        long_score += 1
        long_reasons.append("1분추세복원+1")
    if ema_fast_1m < ema_slow_1m and latest_close_1m < ema_fast_1m:
        short_score += 1
        short_reasons.append("1분추세약세+1")
    if Decimal("50") <= rsi_1m <= Decimal("67"):
        long_score += 1
        long_reasons.append("RSI롱영역+1")
    if Decimal("33") <= rsi_1m <= Decimal("50"):
        short_score += 1
        short_reasons.append("RSI숏영역+1")
    if volume_ratio >= Decimal("1.15"):
        long_score += 1
        short_score += 1
        long_reasons.append("거래량확대+1")
        short_reasons.append("거래량확대+1")
    if latest_close_1m > previous_close_1m:
        long_score += 1
        long_reasons.append("직전봉상승+1")
    if latest_close_1m < previous_close_1m:
        short_score += 1
        short_reasons.append("직전봉하락+1")

    return long_score, short_score, long_reasons, short_reasons