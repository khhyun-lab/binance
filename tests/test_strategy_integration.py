from __future__ import annotations

import asyncio
import importlib
from decimal import Decimal
from pathlib import Path

from binance_bot.backtest.data_loader import CandleSeries, load_candle_dataset
from binance_bot.backtest.replay import HistoricalMarketDataProvider
from binance_bot.backtest.runner import BacktestConfig, run_backtest_sync
from binance_bot.backtest.strategy_adapter import BacktestStrategyAdapter
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