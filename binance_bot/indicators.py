from __future__ import annotations

from decimal import Decimal


def sma(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period:
        raise ValueError("값 개수가 부족합니다.")
    return sum(values[-period:], Decimal("0")) / Decimal(period)


def ema(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period:
        raise ValueError("EMA 계산을 위한 데이터가 부족합니다.")

    multiplier = Decimal("2") / (Decimal(period) + Decimal("1"))
    current = sma(values[:period], period)
    for value in values[period:]:
        current = ((value - current) * multiplier) + current
    return current


def rsi(values: list[Decimal], period: int = 14) -> Decimal:
    if len(values) <= period:
        raise ValueError("RSI 계산을 위한 데이터가 부족합니다.")

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, current in zip(values[:-1], values[1:]):
        diff = current - previous
        if diff >= 0:
            gains.append(diff)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(-diff)

    average_gain = sum(gains[-period:], Decimal("0")) / Decimal(period)
    average_loss = sum(losses[-period:], Decimal("0")) / Decimal(period)
    if average_loss == 0:
        return Decimal("100")
    rs = average_gain / average_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def atr(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int = 14) -> Decimal:
    if len(highs) <= period or len(lows) <= period or len(closes) <= period:
        raise ValueError("ATR 계산을 위한 데이터가 부족합니다.")

    true_ranges: list[Decimal] = []
    for index in range(1, len(closes)):
        current_high = highs[index]
        current_low = lows[index]
        previous_close = closes[index - 1]
        tr = max(
            current_high - current_low,
            abs(current_high - previous_close),
            abs(current_low - previous_close),
        )
        true_ranges.append(tr)

    return sum(true_ranges[-period:], Decimal("0")) / Decimal(period)