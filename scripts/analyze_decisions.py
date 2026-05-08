from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_decisions(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def summarize_decisions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reason_counts = Counter(str(row.get("entry_reason", "unknown")) for row in rows)
    candidate_counts = Counter(str(row.get("entry_type_candidate", "none")) for row in rows)
    blocker_counts: Counter[str] = Counter()
    for row in rows:
        for blocker in row.get("entry_blockers", []):
            blocker_counts[str(blocker)] += 1
    return {
        "rows": len(rows),
        "entry_reason_counts": dict(reason_counts),
        "entry_type_candidate_counts": dict(candidate_counts),
        "preferred_side_counts": dict(Counter(str(row.get("preferred_side", "NONE")) for row in rows)),
        "blocker_counts": dict(blocker_counts),
        "breakout_candidate_rows": sum(1 for row in rows if bool(row.get("breakout_chase_candidate", False))),
        "pullback_candidate_rows": sum(1 for row in rows if bool(row.get("pullback_reaccel_candidate", False))),
        "pullback_valid_rows": sum(1 for row in rows if bool(row.get("pullback_valid", False))),
        "reaccel_valid_rows": sum(1 for row in rows if bool(row.get("reaccel_valid", False))),
        "quantity_ok_rows": sum(1 for row in rows if bool(row.get("quantity_ok", False))),
    }


def write_summary_csv(path: Path, summary: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for key, value in summary.items():
            writer.writerow([key, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value])


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze backtest decision_log.jsonl")
    parser.add_argument("report_dir", type=Path)
    parser.add_argument("--csv", dest="csv_path", type=Path, default=None)
    args = parser.parse_args()

    decision_path = args.report_dir / "decision_log.jsonl"
    rows = load_decisions(decision_path)
    summary = summarize_decisions(rows)
    for key, value in summary.items():
        print(f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
    csv_path = args.csv_path or (args.report_dir / "decision_summary.csv")
    write_summary_csv(csv_path, summary)


if __name__ == "__main__":
    main()