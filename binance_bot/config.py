from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _to_decimal(value: str | None, default: str) -> Decimal:
    if value is None or value.strip() == "":
        return Decimal(default)
    return Decimal(value)


@dataclass(slots=True)
class Settings:
    api_key: str
    api_secret: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    symbols: list[str]
    dry_run: bool
    testnet: bool
    leverage: int
    use_available_balance: bool
    margin_per_trade_usdt: Decimal
    entry_splits: int
    exit_splits: int
    max_open_positions: int
    cycle_seconds: int
    exit_monitor_seconds: int
    candle_count: int
    entry_interval: str
    trend_interval: str
    context_interval: str
    long_entry_score_threshold: int
    short_entry_score_threshold: int
    entry_momentum_min_conditions: int
    entry_min_volume_ratio: Decimal
    long_entry_min_rsi_3m: Decimal
    short_entry_max_rsi_3m: Decimal
    sideways_trade_enabled: bool
    sideways_max_volume_ratio: Decimal
    sideways_take_profit_on_margin_pct: Decimal
    sideways_stop_loss_on_margin_pct: Decimal
    sideways_long_entry_max_rsi_3m: Decimal
    sideways_short_entry_min_rsi_3m: Decimal
    sideways_entry_buffer_atr_multiplier: Decimal
    sideways_alert_cooldown_seconds: int
    sideways_regime_confirm_cycles: int
    sideways_regime_release_cycles: int
    round_trip_fee_pct: Decimal
    min_take_profit_on_margin_pct: Decimal
    max_take_profit_on_margin_pct: Decimal
    min_stop_loss_on_margin_pct: Decimal
    max_stop_loss_on_margin_pct: Decimal
    short_min_take_profit_on_margin_pct: Decimal
    short_max_take_profit_on_margin_pct: Decimal
    short_min_stop_loss_on_margin_pct: Decimal
    short_max_stop_loss_on_margin_pct: Decimal
    exit_reward_risk_ratio: Decimal
    exit_line_refresh_seconds: int
    exit_line_min_change_pct: Decimal
    exit_line_min_change_atr_ratio: Decimal
    recv_window_ms: int
    state_file: Path
    heartbeat_file: Path
    log_dir: Path
    log_level: str


def load_settings() -> Settings:
    load_dotenv()

    root_dir = Path.cwd()
    symbols = [
        symbol.strip().upper()
        for symbol in os.getenv("BINANCE_FUTURES_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
        if symbol.strip()
    ]

    return Settings(
        api_key=os.getenv("BINANCE_FUTURES_API_KEY", ""),
        api_secret=os.getenv("BINANCE_FUTURES_API_SECRET", ""),
        telegram_bot_token=os.getenv("BINANCE_FUTURES_TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("BINANCE_FUTURES_TELEGRAM_CHAT_ID") or None,
        symbols=symbols,
        dry_run=_to_bool(os.getenv("BINANCE_FUTURES_DRY_RUN"), True),
        testnet=_to_bool(os.getenv("BINANCE_FUTURES_TESTNET"), False),
        leverage=_to_int(os.getenv("BINANCE_FUTURES_LEVERAGE"), 7),
        use_available_balance=_to_bool(os.getenv("BINANCE_FUTURES_USE_AVAILABLE_BALANCE"), False),
        margin_per_trade_usdt=_to_decimal(os.getenv("BINANCE_FUTURES_MARGIN_PER_TRADE_USDT"), "25"),
        entry_splits=max(1, _to_int(os.getenv("BINANCE_FUTURES_ENTRY_SPLITS"), 1)),
        exit_splits=max(1, _to_int(os.getenv("BINANCE_FUTURES_EXIT_SPLITS"), 1)),
        max_open_positions=_to_int(os.getenv("BINANCE_FUTURES_MAX_OPEN_POSITIONS"), 2),
        cycle_seconds=_to_int(os.getenv("BINANCE_FUTURES_CYCLE_SECONDS"), 60),
        exit_monitor_seconds=max(0, _to_int(os.getenv("BINANCE_FUTURES_EXIT_MONITOR_SECONDS"), 15)),
        candle_count=_to_int(os.getenv("BINANCE_FUTURES_CANDLE_COUNT"), 120),
        entry_interval=os.getenv("BINANCE_FUTURES_ENTRY_INTERVAL", "1m"),
        trend_interval=os.getenv("BINANCE_FUTURES_TREND_INTERVAL", "5m"),
        context_interval=os.getenv("BINANCE_FUTURES_CONTEXT_INTERVAL", "15m"),
        long_entry_score_threshold=_to_int(os.getenv("BINANCE_FUTURES_LONG_ENTRY_SCORE_THRESHOLD", os.getenv("BINANCE_FUTURES_ENTRY_SCORE_THRESHOLD")), 5),
        short_entry_score_threshold=_to_int(os.getenv("BINANCE_FUTURES_SHORT_ENTRY_SCORE_THRESHOLD", os.getenv("BINANCE_FUTURES_ENTRY_SCORE_THRESHOLD")), 4),
        entry_momentum_min_conditions=max(1, _to_int(os.getenv("BINANCE_FUTURES_ENTRY_MOMENTUM_MIN_CONDITIONS"), 2)),
        entry_min_volume_ratio=_to_decimal(os.getenv("BINANCE_FUTURES_ENTRY_MIN_VOLUME_RATIO"), "0.90"),
        long_entry_min_rsi_3m=_to_decimal(os.getenv("BINANCE_FUTURES_LONG_ENTRY_MIN_RSI_3M"), "60"),
        short_entry_max_rsi_3m=_to_decimal(os.getenv("BINANCE_FUTURES_SHORT_ENTRY_MAX_RSI_3M"), "40"),
        sideways_trade_enabled=_to_bool(os.getenv("BINANCE_FUTURES_SIDEWAYS_TRADE_ENABLED"), True),
        sideways_max_volume_ratio=_to_decimal(os.getenv("BINANCE_FUTURES_SIDEWAYS_MAX_VOLUME_RATIO"), "1.10"),
        sideways_take_profit_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_SIDEWAYS_TAKE_PROFIT_ON_MARGIN_PCT"), "0.0035"),
        sideways_stop_loss_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_SIDEWAYS_STOP_LOSS_ON_MARGIN_PCT"), "0.0030"),
        sideways_long_entry_max_rsi_3m=_to_decimal(os.getenv("BINANCE_FUTURES_SIDEWAYS_LONG_ENTRY_MAX_RSI_3M"), "38"),
        sideways_short_entry_min_rsi_3m=_to_decimal(os.getenv("BINANCE_FUTURES_SIDEWAYS_SHORT_ENTRY_MIN_RSI_3M"), "62"),
        sideways_entry_buffer_atr_multiplier=_to_decimal(os.getenv("BINANCE_FUTURES_SIDEWAYS_ENTRY_BUFFER_ATR_MULTIPLIER"), "0.35"),
        sideways_alert_cooldown_seconds=max(0, _to_int(os.getenv("BINANCE_FUTURES_SIDEWAYS_ALERT_COOLDOWN_SECONDS"), 1800)),
        sideways_regime_confirm_cycles=max(1, _to_int(os.getenv("BINANCE_FUTURES_SIDEWAYS_REGIME_CONFIRM_CYCLES"), 2)),
        sideways_regime_release_cycles=max(1, _to_int(os.getenv("BINANCE_FUTURES_SIDEWAYS_REGIME_RELEASE_CYCLES"), 2)),
        round_trip_fee_pct=_to_decimal(os.getenv("BINANCE_FUTURES_ROUND_TRIP_FEE_PCT"), "0.0010"),
        min_take_profit_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_MIN_TAKE_PROFIT_ON_MARGIN_PCT"), "0.035"),
        max_take_profit_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_MAX_TAKE_PROFIT_ON_MARGIN_PCT"), "0.070"),
        min_stop_loss_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_MIN_STOP_LOSS_ON_MARGIN_PCT"), "0.021"),
        max_stop_loss_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_MAX_STOP_LOSS_ON_MARGIN_PCT"), "0.035"),
        short_min_take_profit_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_SHORT_MIN_TAKE_PROFIT_ON_MARGIN_PCT", os.getenv("BINANCE_FUTURES_MIN_TAKE_PROFIT_ON_MARGIN_PCT")), "0.030"),
        short_max_take_profit_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_SHORT_MAX_TAKE_PROFIT_ON_MARGIN_PCT", os.getenv("BINANCE_FUTURES_MAX_TAKE_PROFIT_ON_MARGIN_PCT")), "0.055"),
        short_min_stop_loss_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_SHORT_MIN_STOP_LOSS_ON_MARGIN_PCT", os.getenv("BINANCE_FUTURES_MIN_STOP_LOSS_ON_MARGIN_PCT")), "0.018"),
        short_max_stop_loss_on_margin_pct=_to_decimal(os.getenv("BINANCE_FUTURES_SHORT_MAX_STOP_LOSS_ON_MARGIN_PCT", os.getenv("BINANCE_FUTURES_MAX_STOP_LOSS_ON_MARGIN_PCT")), "0.030"),
        exit_reward_risk_ratio=_to_decimal(os.getenv("BINANCE_FUTURES_EXIT_REWARD_RISK_RATIO"), "2.0"),
        exit_line_refresh_seconds=max(0, _to_int(os.getenv("BINANCE_FUTURES_EXIT_LINE_REFRESH_SECONDS"), 300)),
        exit_line_min_change_pct=_to_decimal(os.getenv("BINANCE_FUTURES_EXIT_LINE_MIN_CHANGE_PCT"), "0.0007"),
        exit_line_min_change_atr_ratio=_to_decimal(os.getenv("BINANCE_FUTURES_EXIT_LINE_MIN_CHANGE_ATR_RATIO"), "0.18"),
        recv_window_ms=_to_int(os.getenv("BINANCE_FUTURES_RECV_WINDOW_MS"), 5000),
        state_file=root_dir / os.getenv("BINANCE_FUTURES_STATE_FILE", "runtime/binance_state.json"),
        heartbeat_file=root_dir / os.getenv("BINANCE_FUTURES_HEARTBEAT_FILE", "runtime/binance_heartbeat.json"),
        log_dir=root_dir / os.getenv("BINANCE_FUTURES_LOG_DIR", "logs"),
        log_level=os.getenv("BINANCE_FUTURES_LOG_LEVEL", "INFO").upper(),
    )