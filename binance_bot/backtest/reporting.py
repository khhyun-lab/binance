from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .execution import EquityPoint, TradeRecord
from .metrics import BacktestMetrics
from .strategy_adapter import DecisionLog


def write_backtest_report(
    output_root: Path,
    config: dict[str, Any],
    metrics: BacktestMetrics,
    trades: list[TradeRecord],
    equity_curve: list[EquityPoint],
    daily_pnl: dict[str, float],
    decisions: list[DecisionLog] | None = None,
) -> Path:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = output_root / timestamp
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_trades_csv(report_dir / "trades.csv", trades)
    _write_equity_curve_csv(report_dir / "equity_curve.csv", equity_curve)
    _write_daily_pnl_csv(report_dir / "daily_pnl.csv", daily_pnl)
    if decisions:
        with (report_dir / "decision_log.jsonl").open("w", encoding="utf-8") as handle:
            for decision in decisions:
                handle.write(json.dumps(asdict(decision), ensure_ascii=False))
                handle.write("\n")
    return report_dir


def _write_trades_csv(path: Path, trades: list[TradeRecord]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["entry_time", "exit_time", "symbol", "side", "quantity", "entry_price", "exit_price", "gross_pnl", "fees", "slippage_cost", "net_pnl", "return_on_margin", "entry_reason", "exit_reason", "holding_seconds"])
        for trade in trades:
            writer.writerow([
                trade.entry_time,
                trade.exit_time,
                trade.symbol,
                trade.side,
                str(trade.quantity),
                str(trade.entry_price),
                str(trade.exit_price),
                str(trade.gross_pnl),
                str(trade.fees),
                str(trade.slippage_cost),
                str(trade.net_pnl),
                str(trade.return_on_margin),
                trade.entry_reason,
                trade.exit_reason,
                trade.holding_seconds,
            ])


def _write_equity_curve_csv(path: Path, equity_curve: list[EquityPoint]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "balance", "equity", "drawdown_pct"])
        for point in equity_curve:
            writer.writerow([point.timestamp, str(point.balance), str(point.equity), str(point.drawdown_pct)])


def _write_daily_pnl_csv(path: Path, daily_pnl: dict[str, float]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "net_pnl"])
        for day, pnl in sorted(daily_pnl.items()):
            writer.writerow([day, pnl])