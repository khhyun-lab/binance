from __future__ import annotations

import asyncio
import importlib
from decimal import Decimal
from pathlib import Path

from binance_bot.backtest.data_loader import CandleSeries, load_candle_dataset
from binance_bot.backtest.replay import HistoricalMarketDataProvider
from binance_bot.backtest.runner import BacktestConfig, run_backtest_sync
from binance_bot.backtest.strategy_adapter import BacktestStrategyAdapter
from binance_bot.strategy.scoring import build_market_scores
from binance_bot.strategy.snapshot import MarketSnapshot
from tests.fixtures import write_fixture_dataset


def test_historical_provider_does_not_leak_future_candles(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    dataset = load_candle_dataset(["BTCUSDT"], ["1m", "3m", "5m", "15m"], "2025-01-01", "2025-01-01", data_dir=tmp_path)
    provider = HistoricalMarketDataProvider(dataset)
    visible_candle = dataset["BTCUSDT"]["1m"].candles[140]
    future_candle = dataset["BTCUSDT"]["1m"].candles[141]
    provider.set_current_time(visible_candle.close_time)
    rows = asyncio.run(provider.get_klines("BTCUSDT", "1m", 5))
    assert rows[-1]["open_time"] == visible_candle.open_time
    assert rows[-1]["close"] == Decimal(str(visible_candle.close))
    assert rows[-1]["open_time"] != future_candle.open_time


def test_strategy_snapshot_uses_only_available_candles(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    dataset = load_candle_dataset(["BTCUSDT"], ["1m", "3m", "5m", "15m"], "2025-01-01", "2025-01-01", data_dir=tmp_path)
    provider = HistoricalMarketDataProvider(dataset)
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    visible_candle = dataset["BTCUSDT"]["1m"].candles[1300]
    future_candle = dataset["BTCUSDT"]["1m"].candles[1301]
    provider.set_current_time(visible_candle.close_time)
    snapshot = asyncio.run(adapter._build_snapshot("BTCUSDT"))
    assert snapshot.mark_price == Decimal(str(visible_candle.close))
    assert snapshot.mark_price != Decimal(str(future_candle.close))


def test_backtest_does_not_call_live_binance_order_service(tmp_path: Path, monkeypatch) -> None:
    write_fixture_dataset(tmp_path)

    class GuardProvider(HistoricalMarketDataProvider):
        async def create_aggressive_limit_order(self, *args, **kwargs):  # type: ignore[override]
            raise AssertionError("백테스트에서 live order service를 호출했습니다.")

        async def create_market_order(self, *args, **kwargs):  # type: ignore[override]
            raise AssertionError("백테스트에서 live market order service를 호출했습니다.")

    monkeypatch.setattr("binance_bot.backtest.runner.HistoricalMarketDataProvider", GuardProvider)
    result = run_backtest_sync(
        BacktestConfig(
            symbols=["BTCUSDT"],
            start="2025-01-01",
            end="2025-01-01",
            warmup_candles=1300,
            data_dir=tmp_path,
            output_dir=tmp_path / "reports",
        )
    )
    assert result.report_dir.exists()


def test_entry_decision_path_runs_with_historical_provider(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    dataset = load_candle_dataset(["BTCUSDT"], ["1m", "3m", "5m", "15m"], "2025-01-01", "2025-01-01", data_dir=tmp_path)
    provider = HistoricalMarketDataProvider(dataset)
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    provider.set_current_time(dataset["BTCUSDT"]["1m"].candles[1300].close_time)
    actions, decisions = asyncio.run(adapter.evaluate(Decimal("1000"), Decimal("1000"), dataset["BTCUSDT"]["1m"].candles[1300].open_time))
    assert len(decisions) == 1
    assert decisions[0].symbol == "BTCUSDT"
    assert isinstance(actions, list)


def test_exit_decision_path_runs_with_simulated_position(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    dataset = load_candle_dataset(["BTCUSDT"], ["1m", "3m", "5m", "15m"], "2025-01-01", "2025-01-01", data_dir=tmp_path)
    provider = HistoricalMarketDataProvider(dataset)
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    candle = dataset["BTCUSDT"]["1m"].candles[1300]
    provider.set_current_time(candle.close_time)
    snapshot = asyncio.run(adapter._build_snapshot("BTCUSDT"))
    position = adapter._build_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("1"),
        entry_price=Decimal(str(candle.close)),
        order_id="test-position",
        opened_at=adapter._now_utc().isoformat(),
        leverage=7,
        margin_usdt=Decimal("20"),
        notional_usdt=Decimal(str(candle.close)),
        take_profit_price=Decimal(str(candle.close)) * Decimal("1.01"),
        stop_loss_price=Decimal(str(candle.close)) * Decimal("0.99"),
        take_profit_pct=Decimal("0.01"),
        stop_loss_pct=Decimal("-0.01"),
        entry_count=1,
        exit_count=0,
    )
    adapter.state_store.set(position)
    decision = asyncio.run(adapter._plan_exit(snapshot, position))
    assert isinstance(decision.allowed, bool)


def test_live_bot_entrypoint_import_still_works() -> None:
    module = importlib.import_module("binance_bot.main")
    assert hasattr(module, "main")


def test_market_scores_do_not_promote_oversold_short_without_mtf_alignment() -> None:
    _, short_score, _, short_reasons = build_market_scores(
        mark_price=Decimal("100"),
        breakout_high=Decimal("105"),
        breakout_low=Decimal("101"),
        ema_fast_1m=Decimal("101"),
        ema_slow_1m=Decimal("102"),
        latest_close_1m=Decimal("100"),
        previous_close_1m=Decimal("103"),
        rsi_1m=Decimal("14"),
        volume_ratio=Decimal("3.5"),
        trend_long_ok=False,
        trend_short_ok=False,
    )
    assert short_score <= 2
    assert "과매도숏억제-2" in short_reasons


def test_market_scores_reward_aligned_short_breakout() -> None:
    _, short_score, _, short_reasons = build_market_scores(
        mark_price=Decimal("100"),
        breakout_high=Decimal("105"),
        breakout_low=Decimal("101"),
        ema_fast_1m=Decimal("101"),
        ema_slow_1m=Decimal("102"),
        latest_close_1m=Decimal("100"),
        previous_close_1m=Decimal("103"),
        rsi_1m=Decimal("42"),
        volume_ratio=Decimal("1.8"),
        trend_long_ok=False,
        trend_short_ok=True,
    )
    assert short_score >= 6
    assert "15분/5분하락정렬+2" in short_reasons
    assert "1분하단돌파+2" in short_reasons


def test_breakout_failure_exit_triggers_before_stop() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    position = adapter._build_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        order_id="test-position",
        opened_at=adapter._now_utc().isoformat(),
        leverage=7,
        margin_usdt=Decimal("20"),
        notional_usdt=Decimal("100"),
        take_profit_price=Decimal("104"),
        stop_loss_price=Decimal("97"),
        take_profit_pct=Decimal("0.04"),
        stop_loss_pct=Decimal("-0.03"),
        entry_count=1,
        exit_count=0,
    )
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("99"),
        ask_price=Decimal("99"),
        bid_price=Decimal("99"),
        latest_close_1m=Decimal("99"),
        previous_close_1m=Decimal("98.9"),
        previous_high_1m=Decimal("100.5"),
        previous_low_1m=Decimal("98.9"),
        recent_three_highs_1m=(Decimal("100.8"), Decimal("100.5"), Decimal("99.8")),
        recent_three_lows_1m=(Decimal("99.2"), Decimal("99.0"), Decimal("98.7")),
        ema_fast_1m=Decimal("98.8"),
        ema_slow_1m=Decimal("99.4"),
        long_score=2,
        short_score=0,
        long_reasons=["test"],
        short_reasons=[],
        volume_ratio=Decimal("1.10"),
        rsi_1m=Decimal("40"),
        rsi_3m=Decimal("46"),
        atr_3m=Decimal("1.2"),
        atr_15m=Decimal("1.5"),
        recent_high=Decimal("101"),
        recent_low=Decimal("98"),
        breakout_high=Decimal("100.2"),
        breakout_low=Decimal("97.8"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    decision = asyncio.run(adapter._plan_exit(snapshot, position))
    assert decision.allowed is True
    assert decision.reason == "breakout_failure"


def test_breakout_failure_exit_ignores_single_candle_pullback() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    position = adapter._build_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        order_id="test-position",
        opened_at=adapter._now_utc().isoformat(),
        leverage=7,
        margin_usdt=Decimal("20"),
        notional_usdt=Decimal("100"),
        take_profit_price=Decimal("104"),
        stop_loss_price=Decimal("97"),
        take_profit_pct=Decimal("0.04"),
        stop_loss_pct=Decimal("-0.03"),
        entry_count=1,
        exit_count=0,
    )
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("99.4"),
        ask_price=Decimal("99.4"),
        bid_price=Decimal("99.4"),
        latest_close_1m=Decimal("99.4"),
        previous_close_1m=Decimal("100.4"),
        previous_high_1m=Decimal("100.8"),
        previous_low_1m=Decimal("99.7"),
        recent_three_highs_1m=(Decimal("100.9"), Decimal("100.8"), Decimal("100.1")),
        recent_three_lows_1m=(Decimal("99.8"), Decimal("99.7"), Decimal("99.2")),
        ema_fast_1m=Decimal("99.2"),
        ema_slow_1m=Decimal("99.5"),
        long_score=2,
        short_score=0,
        long_reasons=["test"],
        short_reasons=[],
        volume_ratio=Decimal("1.10"),
        rsi_1m=Decimal("37"),
        rsi_3m=Decimal("44"),
        atr_3m=Decimal("1.2"),
        atr_15m=Decimal("1.5"),
        recent_high=Decimal("101"),
        recent_low=Decimal("98"),
        breakout_high=Decimal("100.2"),
        breakout_low=Decimal("97.8"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    decision = asyncio.run(adapter._plan_exit(snapshot, position))
    assert decision.allowed is False
    assert decision.reason == "hold"


def test_breakout_failure_exit_requires_adverse_move_beyond_entry_buffer() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    position = adapter._build_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        order_id="test-position",
        opened_at=adapter._now_utc().isoformat(),
        leverage=7,
        margin_usdt=Decimal("20"),
        notional_usdt=Decimal("100"),
        take_profit_price=Decimal("103"),
        stop_loss_price=Decimal("97"),
        take_profit_pct=Decimal("0.03"),
        stop_loss_pct=Decimal("-0.02"),
        entry_count=1,
        exit_count=0,
    )
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("99.5"),
        ask_price=Decimal("99.5"),
        bid_price=Decimal("99.5"),
        latest_close_1m=Decimal("99.5"),
        previous_close_1m=Decimal("99.7"),
        previous_high_1m=Decimal("100.2"),
        previous_low_1m=Decimal("99.4"),
        recent_three_highs_1m=(Decimal("100.6"), Decimal("100.2"), Decimal("99.8")),
        recent_three_lows_1m=(Decimal("99.9"), Decimal("99.4"), Decimal("99.2")),
        ema_fast_1m=Decimal("99.3"),
        ema_slow_1m=Decimal("99.7"),
        long_score=2,
        short_score=0,
        long_reasons=["test"],
        short_reasons=[],
        volume_ratio=Decimal("1.10"),
        rsi_1m=Decimal("35"),
        rsi_3m=Decimal("44"),
        atr_3m=Decimal("1.2"),
        atr_15m=Decimal("1.5"),
        recent_high=Decimal("101"),
        recent_low=Decimal("98.8"),
        breakout_high=Decimal("100.2"),
        breakout_low=Decimal("97.8"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    decision = asyncio.run(adapter._plan_exit(snapshot, position))
    assert decision.allowed is False
    assert decision.reason == "hold"






def test_near_target_fade_exit_locks_small_profit() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    position = adapter._build_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        order_id="test-position",
        opened_at=adapter._now_utc().isoformat(),
        leverage=7,
        margin_usdt=Decimal("20"),
        notional_usdt=Decimal("100"),
        take_profit_price=Decimal("103"),
        stop_loss_price=Decimal("98"),
        take_profit_pct=Decimal("0.03"),
        stop_loss_pct=Decimal("-0.02"),
        entry_count=1,
        exit_count=0,
    )
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("101.0"),
        ask_price=Decimal("101.0"),
        bid_price=Decimal("101.0"),
        latest_close_1m=Decimal("101.0"),
        previous_close_1m=Decimal("101.4"),
        previous_high_1m=Decimal("101.6"),
        previous_low_1m=Decimal("100.8"),
        recent_three_highs_1m=(Decimal("101.8"), Decimal("101.6"), Decimal("101.2")),
        recent_three_lows_1m=(Decimal("100.2"), Decimal("100.8"), Decimal("100.7")),
        ema_fast_1m=Decimal("100.7"),
        ema_slow_1m=Decimal("100.9"),
        long_score=2,
        short_score=2,
        long_reasons=["test"],
        short_reasons=["test"],
        volume_ratio=Decimal("1.05"),
        rsi_1m=Decimal("49"),
        rsi_3m=Decimal("55"),
        atr_3m=Decimal("1.0"),
        atr_15m=Decimal("1.1"),
        recent_high=Decimal("102.0"),
        recent_low=Decimal("99.8"),
        breakout_high=Decimal("100.0"),
        breakout_low=Decimal("98.5"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    decision = asyncio.run(adapter._plan_exit(snapshot, position))
    assert decision.allowed is True
    assert decision.reason == "near_target_fade"


def test_long_entry_requires_one_candle_breakout_confirmation() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("101"),
        ask_price=Decimal("101"),
        bid_price=Decimal("101"),
        latest_close_1m=Decimal("101"),
        previous_close_1m=Decimal("99.8"),
        previous_high_1m=Decimal("100.2"),
        previous_low_1m=Decimal("99.4"),
        recent_three_highs_1m=(Decimal("100.0"), Decimal("100.2"), Decimal("101.1")),
        recent_three_lows_1m=(Decimal("99.1"), Decimal("99.4"), Decimal("100.1")),
        ema_fast_1m=Decimal("100.4"),
        ema_slow_1m=Decimal("99.9"),
        long_score=7,
        short_score=0,
        long_reasons=["test"],
        short_reasons=[],
        volume_ratio=Decimal("1.40"),
        rsi_1m=Decimal("60"),
        rsi_3m=Decimal("63"),
        atr_3m=Decimal("1.0"),
        atr_15m=Decimal("1.2"),
        recent_high=Decimal("101.2"),
        recent_low=Decimal("98.8"),
        breakout_high=Decimal("100.0"),
        breakout_low=Decimal("98.5"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    assert adapter._has_entry_momentum(snapshot, "LONG") is False


def test_long_entry_requires_ascending_highs_and_stronger_volume() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("101.2"),
        ask_price=Decimal("101.2"),
        bid_price=Decimal("101.2"),
        latest_close_1m=Decimal("101.2"),
        previous_close_1m=Decimal("100.8"),
        previous_high_1m=Decimal("101.1"),
        previous_low_1m=Decimal("100.0"),
        recent_three_highs_1m=(Decimal("101.0"), Decimal("100.9"), Decimal("101.2")),
        recent_three_lows_1m=(Decimal("99.7"), Decimal("100.0"), Decimal("100.4")),
        ema_fast_1m=Decimal("100.7"),
        ema_slow_1m=Decimal("100.1"),
        long_score=7,
        short_score=0,
        long_reasons=["test"],
        short_reasons=[],
        volume_ratio=Decimal("1.25"),
        rsi_1m=Decimal("60"),
        rsi_3m=Decimal("63"),
        atr_3m=Decimal("1.0"),
        atr_15m=Decimal("1.2"),
        recent_high=Decimal("101.3"),
        recent_low=Decimal("98.8"),
        breakout_high=Decimal("100.0"),
        breakout_low=Decimal("98.5"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    assert adapter._has_entry_momentum(snapshot, "LONG") is False


def test_long_entry_blocks_chasing_breakout_extension() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("101.5"),
        ask_price=Decimal("101.5"),
        bid_price=Decimal("101.5"),
        latest_close_1m=Decimal("101.5"),
        previous_close_1m=Decimal("100.8"),
        previous_high_1m=Decimal("101.0"),
        previous_low_1m=Decimal("100.1"),
        recent_three_highs_1m=(Decimal("100.4"), Decimal("101.0"), Decimal("101.5")),
        recent_three_lows_1m=(Decimal("99.8"), Decimal("100.1"), Decimal("100.7")),
        ema_fast_1m=Decimal("100.9"),
        ema_slow_1m=Decimal("100.2"),
        long_score=7,
        short_score=0,
        long_reasons=["test"],
        short_reasons=[],
        volume_ratio=Decimal("1.50"),
        rsi_1m=Decimal("62"),
        rsi_3m=Decimal("64"),
        atr_3m=Decimal("0.8"),
        atr_15m=Decimal("1.0"),
        recent_high=Decimal("101.6"),
        recent_low=Decimal("98.8"),
        breakout_high=Decimal("101.0"),
        breakout_low=Decimal("98.5"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    assert adapter._has_entry_momentum(snapshot, "LONG") is False


def test_long_entry_blocks_overheated_one_minute_rsi() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("100.9"),
        ask_price=Decimal("100.9"),
        bid_price=Decimal("100.9"),
        latest_close_1m=Decimal("100.9"),
        previous_close_1m=Decimal("100.5"),
        previous_high_1m=Decimal("100.8"),
        previous_low_1m=Decimal("100.1"),
        recent_three_highs_1m=(Decimal("100.2"), Decimal("100.8"), Decimal("101.0")),
        recent_three_lows_1m=(Decimal("99.8"), Decimal("100.1"), Decimal("100.4")),
        ema_fast_1m=Decimal("100.6"),
        ema_slow_1m=Decimal("100.0"),
        long_score=7,
        short_score=0,
        long_reasons=["test"],
        short_reasons=[],
        volume_ratio=Decimal("1.60"),
        rsi_1m=Decimal("75"),
        rsi_3m=Decimal("66"),
        atr_3m=Decimal("1.0"),
        atr_15m=Decimal("1.2"),
        recent_high=Decimal("101.2"),
        recent_low=Decimal("98.8"),
        breakout_high=Decimal("100.4"),
        breakout_low=Decimal("98.5"),
        trend_long_ok=True,
        trend_short_ok=False,
    )
    assert adapter._is_exhausted_entry(snapshot, "LONG") is True


def test_short_entry_requires_descending_lows_and_stronger_volume() -> None:
    provider = HistoricalMarketDataProvider({})
    adapter = BacktestStrategyAdapter(provider, ["BTCUSDT"], leverage=7, margin_per_trade=Decimal("20"))
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        mark_price=Decimal("98.5"),
        ask_price=Decimal("98.5"),
        bid_price=Decimal("98.5"),
        latest_close_1m=Decimal("98.5"),
        previous_close_1m=Decimal("98.8"),
        previous_high_1m=Decimal("99.5"),
        previous_low_1m=Decimal("98.4"),
        recent_three_highs_1m=(Decimal("100.0"), Decimal("99.6"), Decimal("99.1")),
        recent_three_lows_1m=(Decimal("98.2"), Decimal("98.4"), Decimal("98.1")),
        ema_fast_1m=Decimal("98.7"),
        ema_slow_1m=Decimal("99.1"),
        long_score=0,
        short_score=8,
        long_reasons=[],
        short_reasons=["test"],
        volume_ratio=Decimal("1.20"),
        rsi_1m=Decimal("41"),
        rsi_3m=Decimal("38"),
        atr_3m=Decimal("1.0"),
        atr_15m=Decimal("1.2"),
        recent_high=Decimal("100.1"),
        recent_low=Decimal("98.0"),
        breakout_high=Decimal("100.0"),
        breakout_low=Decimal("99.0"),
        trend_long_ok=False,
        trend_short_ok=True,
    )
    assert adapter._has_entry_momentum(snapshot, "SHORT") is False