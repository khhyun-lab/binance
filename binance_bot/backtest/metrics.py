from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal

from .execution import EquityPoint, TradeRecord


@dataclass(slots=True)
class BacktestMetrics:
    initial_balance: Decimal
    final_balance: Decimal
    net_pnl: Decimal
    total_return_pct: Decimal
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal
    average_win: Decimal
    average_loss: Decimal
    profit_factor: Decimal
    expectancy: Decimal
    max_drawdown_pct: Decimal
    max_consecutive_wins: int
    max_consecutive_losses: int
    average_holding_time: Decimal
    best_trade: Decimal
    worst_trade: Decimal
    fees_paid: Decimal
    estimated_slippage_cost: Decimal
    by_symbol: dict[str, dict[str, float]]
    daily_pnl: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, Decimal):
                data[key] = float(value)
        return data


def calculate_metrics(initial_balance: Decimal, final_balance: Decimal, trades: list[TradeRecord], equity_curve: list[EquityPoint]) -> BacktestMetrics:
    win_trades = [trade for trade in trades if trade.net_pnl > 0]
    loss_trades = [trade for trade in trades if trade.net_pnl < 0]
    gross_profit = sum((trade.net_pnl for trade in win_trades), Decimal("0"))
    gross_loss = sum((trade.net_pnl for trade in loss_trades), Decimal("0"))
    fees_paid = sum((trade.fees for trade in trades), Decimal("0"))
    slippage_cost = sum((trade.slippage_cost for trade in trades), Decimal("0"))
    max_drawdown_pct = max((point.drawdown_pct for point in equity_curve), default=Decimal("0"))
    by_symbol_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_symbol_trades: dict[str, int] = defaultdict(int)
    daily_pnl: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for trade in trades:
        by_symbol_totals[trade.symbol] += trade.net_pnl
        by_symbol_trades[trade.symbol] += 1
        daily_pnl[_timestamp_to_day(trade.exit_time)] += trade.net_pnl

    return BacktestMetrics(
        initial_balance=initial_balance,
        final_balance=final_balance,
        net_pnl=final_balance - initial_balance,
        total_return_pct=Decimal("0") if initial_balance <= 0 else ((final_balance - initial_balance) / initial_balance) * Decimal("100"),
        trade_count=len(trades),
        win_count=len(win_trades),
        loss_count=len(loss_trades),
        win_rate=Decimal("0") if not trades else (Decimal(len(win_trades)) / Decimal(len(trades))) * Decimal("100"),
        average_win=Decimal("0") if not win_trades else gross_profit / Decimal(len(win_trades)),
        average_loss=Decimal("0") if not loss_trades else sum((trade.net_pnl for trade in loss_trades), Decimal("0")) / Decimal(len(loss_trades)),
        profit_factor=Decimal("0") if gross_loss == 0 else gross_profit / abs(gross_loss),
        expectancy=Decimal("0") if not trades else sum((trade.net_pnl for trade in trades), Decimal("0")) / Decimal(len(trades)),
        max_drawdown_pct=max_drawdown_pct,
        max_consecutive_wins=_max_consecutive(trades, wins=True),
        max_consecutive_losses=_max_consecutive(trades, wins=False),
        average_holding_time=Decimal("0") if not trades else sum((Decimal(trade.holding_seconds) for trade in trades), Decimal("0")) / Decimal(len(trades)),
        best_trade=max((trade.net_pnl for trade in trades), default=Decimal("0")),
        worst_trade=min((trade.net_pnl for trade in trades), default=Decimal("0")),
        fees_paid=fees_paid,
        estimated_slippage_cost=slippage_cost,
        by_symbol={symbol: {"net_pnl": float(by_symbol_totals[symbol]), "trade_count": by_symbol_trades[symbol]} for symbol in sorted(by_symbol_totals)},
        daily_pnl={day: float(value) for day, value in sorted(daily_pnl.items())},
    )


def _max_consecutive(trades: list[TradeRecord], wins: bool) -> int:
    best = 0
    current = 0
    for trade in trades:
        hit = trade.net_pnl > 0 if wins else trade.net_pnl < 0
        if hit:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _timestamp_to_day(timestamp_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")