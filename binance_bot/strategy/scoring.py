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
        long_reasons.append("15분/5분상승정렬+2")
    if trend_short_ok:
        short_score += 2
        short_reasons.append("15분/5분하락정렬+2")
    if trend_long_ok and mark_price >= breakout_high:
        long_score += 2
        long_reasons.append("1분상단돌파+2")
    if trend_short_ok and mark_price <= breakout_low:
        short_score += 2
        short_reasons.append("1분하단돌파+2")
    if ema_fast_1m > ema_slow_1m and latest_close_1m > ema_fast_1m:
        long_score += 1
        long_reasons.append("1분추세유지+1")
    if ema_fast_1m < ema_slow_1m and latest_close_1m < ema_fast_1m:
        short_score += 1
        short_reasons.append("1분추세약세+1")
    if Decimal("52") <= rsi_1m <= Decimal("68"):
        long_score += 1
        long_reasons.append("1분RSI확장+1")
    if Decimal("36") <= rsi_1m <= Decimal("48"):
        short_score += 1
        short_reasons.append("1분RSI하락확인+1")
    if volume_ratio >= Decimal("1.00"):
        long_score += 1
        short_score += 1
        long_reasons.append("돌파거래량확인+1")
        short_reasons.append("돌파거래량확인+1")
    if latest_close_1m > previous_close_1m:
        long_score += 1
        long_reasons.append("직전봉상승+1")
    if latest_close_1m < previous_close_1m:
        short_score += 1
        short_reasons.append("직전봉하락+1")

    if not trend_long_ok and long_score > 0:
        long_score = max(0, long_score - 1)
        long_reasons.append("상위추세미정렬-1")
    if not trend_short_ok and short_score > 0:
        short_score = max(0, short_score - 2)
        short_reasons.append("상위추세미정렬-2")
    if rsi_1m < Decimal("30") and short_score > 0:
        short_score = max(0, short_score - 2)
        short_reasons.append("과매도숏억제-2")

    return long_score, short_score, long_reasons, short_reasons