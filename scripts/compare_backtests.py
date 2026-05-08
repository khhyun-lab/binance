from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _load_summary(report_dir: Path) -> dict[str, Any]:
    with (report_dir / "summary.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_trades(report_dir: Path) -> list[dict[str, str]]:
    trades_path = report_dir / "trades.csv"
    if not trades_path.exists():
        return []
    with trades_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def summarize_report_dir(report_dir: Path) -> dict[str, Any]:
    summary = _load_summary(report_dir)
    trades = _load_trades(report_dir)
    entry_type_counts = Counter((trade.get("entry_reason") or "unknown") for trade in trades)
    exit_reason_counts = Counter((trade.get("exit_reason") or "unknown") for trade in trades)
    return {
        "report_dir": str(report_dir),
        "trade_count": int(summary.get("trade_count", len(trades))),
        "net_pnl": float(summary.get("net_pnl", 0)),
        "max_drawdown_pct": float(summary.get("max_drawdown_pct", 0)),
        "win_rate": float(summary.get("win_rate", 0)),
        "profit_factor": float(summary.get("profit_factor", 0)),
        "entry_type_counts": dict(entry_type_counts),
        "exit_reason_counts": dict(exit_reason_counts),
    }


def compare_report_dirs(report_dirs: list[Path]) -> list[dict[str, Any]]:
    return [summarize_report_dir(report_dir) for report_dir in report_dirs]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare backtest report directories")
    parser.add_argument("report_dirs", nargs="+", type=Path)
    args = parser.parse_args()
    rows = compare_report_dirs(args.report_dirs)
    for row in rows:
        print(
            "\t".join(
                [
                    row["report_dir"],
                    str(row["trade_count"]),
                    f"{row['net_pnl']:.4f}",
                    f"{row['max_drawdown_pct']:.4f}",
                    f"{row['win_rate']:.4f}",
                    f"{row['profit_factor']:.4f}",
                    json.dumps(row["entry_type_counts"], ensure_ascii=False),
                    json.dumps(row["exit_reason_counts"], ensure_ascii=False),
                ]
            )
        )


if __name__ == "__main__":
    main()