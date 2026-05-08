from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from binance_bot.backtest.data_loader import load_candle_dataset
from binance_bot.backtest.replay import HistoricalMarketDataProvider
from binance_bot.backtest.strategy_adapter import BacktestStrategyAdapter
from binance_bot.services.binance_futures_service import AccountSnapshot
from binance_bot.strategy.plans import PlanDecision
from binance_bot.strategy.snapshot import MarketSnapshot
from scripts.compare_backtests import compare_report_dirs
from tests.fixtures import write_fixture_dataset


def _build_pullback_snapshot(side: str = "LONG") -> MarketSnapshot:
    if side == "LONG":
        return MarketSnapshot(
            symbol="BTCUSDT",
            mark_price=Decimal("101.40"),
            ask_price=Decimal("101.40"),
            bid_price=Decimal("101.40"),
            latest_close_1m=Decimal("101.40"),
            previous_close_1m=Decimal("100.90"),
            previous_high_1m=Decimal("101.10"),
            previous_low_1m=Decimal("100.70"),
            recent_three_highs_1m=(Decimal("101.50"), Decimal("101.10"), Decimal("101.45")),
            recent_three_lows_1m=(Decimal("100.60"), Decimal("100.70"), Decimal("100.95")),
            ema_fast_1m=Decimal("101.00"),
            ema_slow_1m=Decimal("100.70"),
            long_score=6,
            short_score=3,
            long_reasons=["trend"],
            short_reasons=["counter"],
            volume_ratio=Decimal("1.10"),
            rsi_1m=Decimal("58"),
            rsi_3m=Decimal("56"),
            atr_3m=Decimal("1.50"),
            atr_15m=Decimal("1.80"),
            recent_high=Decimal("101.50"),
            recent_low=Decimal("100.60"),
            breakout_high=Decimal("101.00"),
            breakout_low=Decimal("99.50"),
            trend_long_ok=True,
            trend_short_ok=False,
            latest_close_time_ms=1_700_000_000_000,
            recent_closes_1m=(Decimal("99.80"), Decimal("100.30"), Decimal("100.70"), Decimal("101.30"), Decimal("100.80"), Decimal("100.90"), Decimal("101.40")),
            recent_highs_window_1m=(Decimal("100.00"), Decimal("100.50"), Decimal("100.90"), Decimal("101.50"), Decimal("101.00"), Decimal("101.10"), Decimal("101.45")),
            recent_lows_window_1m=(Decimal("99.60"), Decimal("100.10"), Decimal("100.50"), Decimal("100.90"), Decimal("100.60"), Decimal("100.70"), Decimal("100.95")),
            recent_volumes_1m=(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1"), Decimal("1.4")),
        )
    return MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("98.60"),
        ask_price=Decimal("98.60"),
        bid_price=Decimal("98.60"),
        latest_close_1m=Decimal("98.60"),
        previous_close_1m=Decimal("99.10"),
        previous_high_1m=Decimal("99.30"),
        previous_low_1m=Decimal("98.90"),
        recent_three_highs_1m=(Decimal("99.40"), Decimal("99.30"), Decimal("99.05")),
        recent_three_lows_1m=(Decimal("98.50"), Decimal("98.90"), Decimal("98.55")),
        ema_fast_1m=Decimal("99.00"),
        ema_slow_1m=Decimal("99.30"),
        long_score=2,
        short_score=6,
        long_reasons=["counter"],
        short_reasons=["trend"],
        volume_ratio=Decimal("1.12"),
        rsi_1m=Decimal("42"),
        rsi_3m=Decimal("45"),
        atr_3m=Decimal("1.50"),
        atr_15m=Decimal("1.80"),
        recent_high=Decimal("99.40"),
        recent_low=Decimal("98.50"),
        breakout_high=Decimal("100.20"),
        breakout_low=Decimal("99.00"),
        trend_long_ok=False,
        trend_short_ok=True,
        latest_close_time_ms=1_700_000_000_000,
        recent_closes_1m=(Decimal("100.20"), Decimal("99.80"), Decimal("99.30"), Decimal("98.70"), Decimal("99.20"), Decimal("99.10"), Decimal("98.60")),
        recent_highs_window_1m=(Decimal("100.40"), Decimal("100.00"), Decimal("99.50"), Decimal("99.10"), Decimal("99.40"), Decimal("99.30"), Decimal("99.05")),
        recent_lows_window_1m=(Decimal("100.00"), Decimal("99.60"), Decimal("99.10"), Decimal("98.50"), Decimal("98.95"), Decimal("98.90"), Decimal("98.55")),
        recent_volumes_1m=(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1"), Decimal("1.3")),
    )


def _build_breakout_snapshot(side: str = "LONG") -> MarketSnapshot:
    if side == "LONG":
        return MarketSnapshot(
            symbol="BTCUSDT",
            mark_price=Decimal("101.10"),
            ask_price=Decimal("101.10"),
            bid_price=Decimal("101.10"),
            latest_close_1m=Decimal("101.10"),
            previous_close_1m=Decimal("101.00"),
            previous_high_1m=Decimal("101.10"),
            previous_low_1m=Decimal("100.80"),
            recent_three_highs_1m=(Decimal("100.90"), Decimal("101.00"), Decimal("101.20")),
            recent_three_lows_1m=(Decimal("100.60"), Decimal("100.80"), Decimal("100.90")),
            ema_fast_1m=Decimal("100.95"),
            ema_slow_1m=Decimal("100.70"),
            long_score=7,
            short_score=0,
            long_reasons=["trend"],
            short_reasons=[],
            volume_ratio=Decimal("1.80"),
            rsi_1m=Decimal("63"),
            rsi_3m=Decimal("60"),
            atr_3m=Decimal("1.00"),
            atr_15m=Decimal("1.20"),
            recent_high=Decimal("101.20"),
            recent_low=Decimal("100.60"),
            breakout_high=Decimal("101.00"),
            breakout_low=Decimal("99.80"),
            trend_long_ok=True,
            trend_short_ok=False,
            latest_close_time_ms=1_700_000_000_000,
            recent_closes_1m=(Decimal("100.20"), Decimal("100.40"), Decimal("100.60"), Decimal("100.80"), Decimal("101.00"), Decimal("101.00"), Decimal("101.10")),
            recent_highs_window_1m=(Decimal("100.30"), Decimal("100.50"), Decimal("100.70"), Decimal("100.90"), Decimal("101.00"), Decimal("101.10"), Decimal("101.20")),
            recent_lows_window_1m=(Decimal("100.00"), Decimal("100.20"), Decimal("100.40"), Decimal("100.50"), Decimal("100.70"), Decimal("100.80"), Decimal("100.90")),
            recent_volumes_1m=(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("1.3"), Decimal("1.4"), Decimal("1.8")),
        )
    return MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("98.90"),
        ask_price=Decimal("98.90"),
        bid_price=Decimal("98.90"),
        latest_close_1m=Decimal("98.90"),
        previous_close_1m=Decimal("99.00"),
        previous_high_1m=Decimal("99.20"),
        previous_low_1m=Decimal("98.90"),
        recent_three_highs_1m=(Decimal("99.30"), Decimal("99.10"), Decimal("99.00")),
        recent_three_lows_1m=(Decimal("99.10"), Decimal("99.00"), Decimal("98.80")),
        ema_fast_1m=Decimal("98.95"),
        ema_slow_1m=Decimal("99.15"),
        long_score=0,
        short_score=7,
        long_reasons=[],
        short_reasons=["trend"],
        volume_ratio=Decimal("1.70"),
        rsi_1m=Decimal("41"),
        rsi_3m=Decimal("40"),
        atr_3m=Decimal("1.00"),
        atr_15m=Decimal("1.20"),
        recent_high=Decimal("99.30"),
        recent_low=Decimal("98.80"),
        breakout_high=Decimal("100.20"),
        breakout_low=Decimal("99.00"),
        trend_long_ok=False,
        trend_short_ok=True,
        latest_close_time_ms=1_700_000_000_000,
        recent_closes_1m=(Decimal("99.80"), Decimal("99.60"), Decimal("99.40"), Decimal("99.20"), Decimal("99.00"), Decimal("99.00"), Decimal("98.90")),
        recent_highs_window_1m=(Decimal("99.90"), Decimal("99.70"), Decimal("99.50"), Decimal("99.30"), Decimal("99.20"), Decimal("99.10"), Decimal("99.00")),
        recent_lows_window_1m=(Decimal("99.60"), Decimal("99.40"), Decimal("99.20"), Decimal("99.10"), Decimal("99.00"), Decimal("98.90"), Decimal("98.80")),
        recent_volumes_1m=(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("1.3"), Decimal("1.4"), Decimal("1.7")),
    )


def test_pullback_reaccel_long_candidate_detected() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    candidate = adapter._evaluate_pullback_reaccel_candidate(_build_pullback_snapshot("LONG"), "LONG")
    assert candidate.ready is True
    assert candidate.entry_type == "pullback_reaccel_long"


def test_pullback_reaccel_short_candidate_detected() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    candidate = adapter._evaluate_pullback_reaccel_candidate(_build_pullback_snapshot("SHORT"), "SHORT")
    assert candidate.ready is True
    assert candidate.entry_type == "pullback_reaccel_short"


def test_pullback_reaccel_does_not_use_future_candles(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    dataset = load_candle_dataset(["BTCUSDT"], ["1m", "3m", "5m", "15m"], "2025-01-01", "2025-01-01", data_dir=tmp_path)
    provider = HistoricalMarketDataProvider(dataset)
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    visible_candle = dataset["BTCUSDT"]["1m"].candles[1300]
    future_candle = dataset["BTCUSDT"]["1m"].candles[1301]
    provider.set_current_time(visible_candle.close_time)
    snapshot = asyncio.run(adapter._build_snapshot("BTCUSDT"))
    assert snapshot.recent_closes_1m[-1] == Decimal(str(visible_candle.close))
    assert snapshot.recent_closes_1m[-1] != Decimal(str(future_candle.close))


def test_pullback_reaccel_rejects_too_deep_pullback() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = _build_pullback_snapshot("LONG")
    snapshot = replace(snapshot, recent_lows_window_1m=snapshot.recent_lows_window_1m[:-3] + (Decimal("99.00"),) + snapshot.recent_lows_window_1m[-2:])
    candidate = adapter._evaluate_pullback_reaccel_candidate(snapshot, "LONG")
    assert candidate.ready is False
    assert "pullback_invalid" in candidate.blockers


def test_pullback_reaccel_rejects_chasing_entry() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = replace(_build_pullback_snapshot("LONG"), mark_price=Decimal("101.90"))
    candidate = adapter._evaluate_pullback_reaccel_candidate(snapshot, "LONG")
    assert candidate.ready is False
    assert "chasing_breakout" in candidate.blockers


def test_pullback_reaccel_requires_reacceleration() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = replace(_build_pullback_snapshot("LONG"), latest_close_1m=Decimal("100.95"), mark_price=Decimal("100.95"))
    candidate = adapter._evaluate_pullback_reaccel_candidate(snapshot, "LONG")
    assert candidate.ready is False
    assert "reaccel_missing" in candidate.blockers


def test_pullback_reaccel_uses_structural_stop() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = _build_pullback_snapshot("LONG")
    candidate = adapter._evaluate_pullback_reaccel_candidate(snapshot, "LONG")
    _, stop_loss_price, _, _ = adapter._calculate_exit_lines(snapshot, "LONG", snapshot.mark_price, adapter.settings.leverage, entry_metadata=candidate.to_metadata("LONG"))
    assert stop_loss_price <= candidate.pullback_low - (snapshot.atr_3m * adapter.settings.pullback_stop_buffer_atr)


def test_pullback_reaccel_min_rr_guard() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    adapter.settings.pullback_min_rr = Decimal("2.80")
    snapshot = _build_pullback_snapshot("LONG")
    account_snapshot = AccountSnapshot(available_balance=Decimal("1000"), wallet_balance=Decimal("1000"), max_withdraw_amount=Decimal("1000"))
    decision = asyncio.run(adapter._plan_entry(snapshot, account_snapshot))
    assert decision.allowed is False
    assert decision.reason == "pullback_min_rr_blocked"


def test_breakout_short_is_blocked_by_default() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = _build_breakout_snapshot("SHORT")
    account_snapshot = AccountSnapshot(available_balance=Decimal("1000"), wallet_balance=Decimal("1000"), max_withdraw_amount=Decimal("1000"))
    decision = asyncio.run(adapter._plan_entry(snapshot, account_snapshot))
    assert decision.allowed is False
    assert decision.reason == "breakout_side_disabled"


def test_breakout_long_blocks_overheated_rsi() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = replace(_build_breakout_snapshot("LONG"), rsi_3m=Decimal("66"))
    account_snapshot = AccountSnapshot(available_balance=Decimal("1000"), wallet_balance=Decimal("1000"), max_withdraw_amount=Decimal("1000"))
    decision = asyncio.run(adapter._plan_entry(snapshot, account_snapshot))
    assert decision.allowed is False
    assert decision.reason == "breakout_long_rsi_too_high"


def test_breakout_long_allows_tempered_extension() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = _build_breakout_snapshot("LONG")
    account_snapshot = AccountSnapshot(available_balance=Decimal("1000"), wallet_balance=Decimal("1000"), max_withdraw_amount=Decimal("1000"))
    decision = asyncio.run(adapter._plan_entry(snapshot, account_snapshot))
    assert decision.allowed is True
    assert decision.plan is not None
    assert decision.plan.reason == "breakout_chase_long"


def test_decision_log_contains_entry_blockers() -> None:
    adapter = BacktestStrategyAdapter(HistoricalMarketDataProvider({}), ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = _build_pullback_snapshot("LONG")
    snapshot = replace(snapshot, volume_ratio=Decimal("0.50"))
    account_snapshot = AccountSnapshot(available_balance=Decimal("1000"), wallet_balance=Decimal("1000"), max_withdraw_amount=Decimal("1000"))
    entry_decision = asyncio.run(adapter._plan_entry(snapshot, account_snapshot))
    log = adapter._build_decision_log(0, snapshot, "TREND", None, entry_decision, PlanDecision(allowed=False, reason="not_evaluated"))
    assert log.entry_type_candidate != "none"
    assert "volume_below_floor" in log.entry_blockers


def test_compare_backtests_reads_summary_files(tmp_path: Path) -> None:
    report_a = tmp_path / "a"
    report_b = tmp_path / "b"
    report_a.mkdir()
    report_b.mkdir()
    (report_a / "summary.json").write_text(json.dumps({"trade_count": 2, "net_pnl": 10.5, "max_drawdown_pct": 1.2, "win_rate": 50.0, "profit_factor": 1.3}), encoding="utf-8")
    (report_b / "summary.json").write_text(json.dumps({"trade_count": 1, "net_pnl": -3.0, "max_drawdown_pct": 0.8, "win_rate": 0.0, "profit_factor": 0.0}), encoding="utf-8")
    (report_a / "trades.csv").write_text("entry_time,exit_time,symbol,side,quantity,entry_price,exit_price,gross_pnl,fees,slippage_cost,net_pnl,return_on_margin,entry_reason,exit_reason,holding_seconds\n1,2,BTCUSDT,LONG,1,100,101,1,0,0,1,1,pullback_reaccel_long,take_profit,60\n3,4,BTCUSDT,LONG,1,101,100,-1,0,0,-1,-1,breakout_chase_long,stop_loss,30\n", encoding="utf-8")
    (report_b / "trades.csv").write_text("entry_time,exit_time,symbol,side,quantity,entry_price,exit_price,gross_pnl,fees,slippage_cost,net_pnl,return_on_margin,entry_reason,exit_reason,holding_seconds\n1,2,BTCUSDT,SHORT,1,100,101,-1,0,0,-1,-1,breakout_chase_short,breakout_failure,40\n", encoding="utf-8")
    rows = compare_report_dirs([report_a, report_b])
    assert rows[0]["trade_count"] == 2
    assert rows[0]["entry_type_counts"]["pullback_reaccel_long"] == 1
    assert rows[1]["exit_reason_counts"]["breakout_failure"] == 1