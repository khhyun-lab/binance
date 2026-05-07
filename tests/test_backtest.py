from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from binance_bot.backtest.data_loader import DataValidationError, load_candle_series
from binance_bot.backtest.execution import FuturesExecutionSimulator, PendingOrder
from binance_bot.backtest.metrics import calculate_metrics
from binance_bot.backtest.runner import BacktestConfig, run_backtest_sync
from tests.fixtures import write_fixture_dataset


def test_candle_parsing(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    series = load_candle_series("BTCUSDT", "1m", "2025-01-01", "2025-01-01", data_dir=tmp_path)
    assert series.candles[0].symbol == "BTCUSDT"
    assert len(series.candles) == 1440


def test_duplicate_timestamp_detection(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    file_path = tmp_path / "BTCUSDT" / "1m" / "2025-01.jsonl"
    with file_path.open("a", encoding="utf-8") as handle:
        first_line = file_path.read_text(encoding="utf-8").splitlines()[0]
        handle.write(first_line + "\n")
    try:
        load_candle_series("BTCUSDT", "1m", "2025-01-01", "2025-01-01", data_dir=tmp_path)
    except DataValidationError as exc:
        assert "중복 캔들" in str(exc)
    else:
        raise AssertionError("중복 timestamp를 감지하지 못했습니다.")


def test_missing_candle_gap_detection(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
    file_path = tmp_path / "BTCUSDT" / "1m" / "2025-01.jsonl"
    lines = file_path.read_text(encoding="utf-8").splitlines()
    file_path.write_text("\n".join(lines[:100] + lines[101:]) + "\n", encoding="utf-8")
    series = load_candle_series("BTCUSDT", "1m", "2025-01-01", "2025-01-01", data_dir=tmp_path)
    assert len(series.gaps) == 1


def test_fee_and_slippage_and_long_short_pnl() -> None:
    simulator = FuturesExecutionSimulator(
        initial_balance=Decimal("1000"),
        leverage=7,
        margin_per_trade=Decimal("20"),
        taker_fee=Decimal("0.0004"),
        maker_fee=Decimal("0.0002"),
        slippage_bps=Decimal("2"),
        max_open_positions=1,
    )
    from binance_bot.backtest.data_loader import Candle

    open_candle = Candle("BTCUSDT", "1m", 1, 100.0, 101.0, 99.0, 100.5, 10.0, 60_000)
    simulator.queue_order(PendingOrder(kind="enter", symbol="BTCUSDT", side="LONG", quantity=Decimal("1"), margin_usdt=Decimal("20"), created_at=0, reason="test"))
    fills = simulator.execute_pending_orders({"BTCUSDT": open_candle})
    assert fills[0].fill_price > Decimal("100")
    position = simulator.positions["BTCUSDT"]
    position.take_profit_price = Decimal("102")
    position.stop_loss_price = Decimal("98")
    close_candle = Candle("BTCUSDT", "1m", 60_000, 101.0, 103.0, 100.0, 102.0, 10.0, 120_000)
    exit_fills = simulator.process_intrabar_triggers({"BTCUSDT": close_candle})
    assert exit_fills[0].trade is not None
    assert exit_fills[0].trade.fees > 0
    assert exit_fills[0].trade.slippage_cost >= 0


def test_same_candle_tp_sl_conservative_handling() -> None:
    simulator = FuturesExecutionSimulator(Decimal("1000"), 7, Decimal("20"), Decimal("0.0004"), Decimal("0.0002"), Decimal("2"), 1)
    from binance_bot.backtest.data_loader import Candle

    entry_candle = Candle("BTCUSDT", "1m", 1, 100.0, 100.0, 100.0, 100.0, 1.0, 60_000)
    simulator.queue_order(PendingOrder(kind="enter", symbol="BTCUSDT", side="LONG", quantity=Decimal("1"), margin_usdt=Decimal("20"), created_at=0, reason="test"))
    simulator.execute_pending_orders({"BTCUSDT": entry_candle})
    position = simulator.positions["BTCUSDT"]
    position.take_profit_price = Decimal("101")
    position.stop_loss_price = Decimal("99")
    candle = Candle("BTCUSDT", "1m", 60_000, 100.0, 101.5, 98.5, 100.5, 1.0, 120_000)
    fills = simulator.process_intrabar_triggers({"BTCUSDT": candle})
    assert fills[0].reason == "stop_loss"


def test_max_drawdown_calculation() -> None:
    from binance_bot.backtest.execution import EquityPoint

    metrics = calculate_metrics(
        Decimal("1000"),
        Decimal("950"),
        [],
        [
            EquityPoint(timestamp=1, balance=Decimal("1000"), equity=Decimal("1000"), drawdown_pct=Decimal("0")),
            EquityPoint(timestamp=2, balance=Decimal("980"), equity=Decimal("980"), drawdown_pct=Decimal("2")),
            EquityPoint(timestamp=3, balance=Decimal("950"), equity=Decimal("950"), drawdown_pct=Decimal("5")),
        ],
    )
    assert metrics.max_drawdown_pct == Decimal("5")


def test_cli_smoke_with_fixture_data(tmp_path: Path) -> None:
    write_fixture_dataset(tmp_path)
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
    assert (result.report_dir / "summary.json").exists()
    assert (result.report_dir / "trades.csv").exists()
    assert (result.report_dir / "equity_curve.csv").exists()
    assert (result.report_dir / "daily_pnl.csv").exists()
    assert (result.report_dir / "config.json").exists()