from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .data_loader import DEFAULT_DATA_DIR, load_candle_dataset
from .execution import EquityPoint, ExecutionFill, FuturesExecutionSimulator, PendingOrder, TradeRecord
from .metrics import BacktestMetrics, calculate_metrics
from .replay import CandleReplayEngine, HistoricalMarketDataProvider
from .reporting import write_backtest_report
from .strategy_adapter import BacktestStrategyAdapter, DecisionLog, StrategyAction


@dataclass(slots=True)
class BacktestConfig:
    symbols: list[str]
    start: str
    end: str
    initial_balance: Decimal = Decimal("1000")
    leverage: int = 7
    margin_per_trade: Decimal = Decimal("20")
    taker_fee: Decimal = Decimal("0.0004")
    maker_fee: Decimal = Decimal("0.0002")
    slippage_bps: Decimal = Decimal("2")
    max_open_positions: int = 2
    warmup_candles: int = 120
    data_dir: Path = DEFAULT_DATA_DIR
    output_dir: Path = Path("reports/backtests")
    debug_decisions: bool = False


@dataclass(slots=True)
class BacktestResult:
    metrics: BacktestMetrics
    trades: list[TradeRecord]
    equity_curve: list[EquityPoint]
    decision_logs: list[DecisionLog]
    report_dir: Path


async def run_backtest(config: BacktestConfig) -> BacktestResult:
    intervals = ["1m", "3m", "5m", "15m"]
    dataset = load_candle_dataset(config.symbols, intervals, config.start, config.end, data_dir=config.data_dir)
    provider = HistoricalMarketDataProvider(dataset)
    replay_engine = CandleReplayEngine(dataset, warmup_candles=config.warmup_candles)
    strategy = BacktestStrategyAdapter(provider, config.symbols, config.leverage, config.margin_per_trade)
    simulator = FuturesExecutionSimulator(
        initial_balance=config.initial_balance,
        leverage=config.leverage,
        margin_per_trade=config.margin_per_trade,
        taker_fee=config.taker_fee,
        maker_fee=config.maker_fee,
        slippage_bps=config.slippage_bps,
        max_open_positions=config.max_open_positions,
    )
    decision_logs: list[DecisionLog] = []

    for step in replay_engine.iter_steps(config.symbols):
        open_fills = simulator.execute_pending_orders(step.candles)
        _apply_fills_to_strategy(strategy, simulator, open_fills)
        intrabar_fills = simulator.process_intrabar_triggers(step.candles)
        _apply_fills_to_strategy(strategy, simulator, intrabar_fills)

        provider.set_current_time(next(iter(step.candles.values())).close_time)
        actions, decisions = await strategy.evaluate(simulator.available_balance(), simulator.balance, step.timestamp)
        if config.debug_decisions:
            decision_logs.extend(decisions)
        for action in actions:
            simulator.queue_order(
                PendingOrder(
                    kind=action.kind,
                    symbol=action.symbol,
                    side=action.side,
                    quantity=action.quantity,
                    margin_usdt=action.margin_usdt,
                    created_at=step.timestamp,
                    reason=action.reason,
                    action=action,
                )
            )
        simulator.record_equity(next(iter(step.candles.values())).close_time, step.candles)
        if simulator.equity_curve and len(simulator.equity_curve) % 250 == 0:
            print(f"progress timestamp={step.timestamp} trades={len(simulator.trades)} balance={simulator.balance}")

    metrics = calculate_metrics(config.initial_balance, simulator.balance, simulator.trades, simulator.equity_curve)
    report_dir = write_backtest_report(
        output_root=config.output_dir,
        config={
            key: str(value) if isinstance(value, (Decimal, Path)) else value
            for key, value in asdict(config).items()
        },
        metrics=metrics,
        trades=simulator.trades,
        equity_curve=simulator.equity_curve,
        daily_pnl=metrics.daily_pnl,
        decisions=decision_logs if config.debug_decisions else None,
    )
    return BacktestResult(metrics=metrics, trades=simulator.trades, equity_curve=simulator.equity_curve, decision_logs=decision_logs, report_dir=report_dir)


def _apply_fills_to_strategy(strategy: BacktestStrategyAdapter, simulator: FuturesExecutionSimulator, fills: list[ExecutionFill]) -> None:
    for fill in fills:
        action = fill.action if isinstance(fill.action, StrategyAction) else None
        if fill.kind == "enter":
            if action is None:
                continue
            position = strategy.on_entry_fill(action, fill.fill_price, fill.quantity, fill.timestamp)
            simulator.update_exit_lines(position.symbol, position.take_profit_price_decimal, position.stop_loss_price_decimal)
        elif fill.kind == "scale_in":
            if action is None:
                continue
            position = strategy.on_scale_in_fill(action, fill.fill_price, fill.quantity, fill.timestamp)
            simulator.update_exit_lines(position.symbol, position.take_profit_price_decimal, position.stop_loss_price_decimal)
        elif fill.kind == "exit":
            strategy.on_exit_fill(fill.symbol)


def run_backtest_sync(config: BacktestConfig) -> BacktestResult:
    return asyncio.run(run_backtest(config))