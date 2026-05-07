from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from binance_bot.config import Settings, load_settings
from binance_bot.services.binance_futures_service import AccountSnapshot
from binance_bot.state import Position, StateStore
from binance_bot.strategy.entry import EntryMixin
from binance_bot.strategy.exit import ExitMixin
from binance_bot.strategy.plans import PlanDecision, StrategyOrderPlan
from binance_bot.strategy.regime import RegimeMixin
from binance_bot.strategy.risk import RiskMixin
from binance_bot.strategy.snapshot import MarketSnapshot, SnapshotMixin


@dataclass(slots=True)
class StrategyAction:
    kind: str
    symbol: str
    side: str
    quantity: Decimal
    margin_usdt: Decimal
    reason: str
    snapshot: MarketSnapshot


@dataclass(slots=True)
class DecisionLog:
    timestamp: int
    symbol: str
    price: str
    market_regime: str
    trend_direction: str
    long_score: int
    short_score: int
    entry_side: str
    entry_allowed: bool
    entry_reason: str
    entry_detail_reasons: list[str]
    exit_allowed: bool
    exit_reason: str
    exit_detail_reasons: list[str]
    tp_price: str
    sl_price: str
    rsi_1m: str
    rsi_3m: str
    ema_fast_1m: str
    ema_slow_1m: str
    atr_3m: str
    atr_15m: str
    volume_ratio: str
    breakout_high: str
    breakout_low: str
    recent_high: str
    recent_low: str
    position_state: dict[str, str | int | bool]


class _NoOpNotifier:
    async def send(self, message: str) -> None:
        return None


class _NoOpHeartbeat:
    def touch(self, status: str = "running") -> None:
        return None


class BacktestStrategyAdapter(SnapshotMixin, RegimeMixin, RiskMixin, EntryMixin, ExitMixin):
    def __init__(self, service: object, symbols: list[str], leverage: int, margin_per_trade: Decimal) -> None:
        base_settings = load_settings()
        self._tmp_dir = TemporaryDirectory(prefix="binance-backtest-state-")
        self.settings: Settings = replace(
            base_settings,
            api_key="",
            api_secret="",
            telegram_bot_token=None,
            telegram_chat_id=None,
            dry_run=True,
            symbols=symbols,
            leverage=leverage,
            margin_per_trade_usdt=margin_per_trade,
            state_file=Path(self._tmp_dir.name) / "state.json",
            heartbeat_file=Path(self._tmp_dir.name) / "heartbeat.json",
        )
        self.service = service
        self.state_store = StateStore(self.settings.state_file)
        self.state_store.load()
        self.notifier = _NoOpNotifier()
        self.heartbeat = _NoOpHeartbeat()
        self.logger = logging.getLogger(self.__class__.__name__)

    async def evaluate(self, available_balance: Decimal, wallet_balance: Decimal, timestamp: int) -> tuple[list[StrategyAction], list[DecisionLog]]:
        actions: list[StrategyAction] = []
        decisions: list[DecisionLog] = []
        account_snapshot = AccountSnapshot(available_balance=available_balance, wallet_balance=wallet_balance, max_withdraw_amount=available_balance)
        for symbol in self.settings.symbols:
            snapshot = await self._build_snapshot(symbol)
            confirmed_regime, _ = self._update_market_regime_state(snapshot)
            position = self.state_store.get(symbol)
            entry_decision = PlanDecision(allowed=False, reason="not_evaluated")
            scale_decision = PlanDecision(allowed=False, reason="not_evaluated")
            exit_decision = PlanDecision(allowed=False, reason="not_evaluated")
            if position is None:
                entry_decision = await self._plan_entry(snapshot, account_snapshot)
                action = self._plan_to_action(entry_decision.plan, snapshot)
            else:
                position = await self._sync_position_trade_metrics(position)
                position = await self._ensure_exit_lines(snapshot, position)
                exit_decision = await self._plan_exit(snapshot, position)
                action = self._plan_to_action(exit_decision.plan, snapshot)
                if action is None:
                    scale_decision = await self._plan_scale_in(snapshot, account_snapshot, position)
                    action = self._plan_to_action(scale_decision.plan, snapshot)
            decisions.append(
                self._build_decision_log(
                    timestamp=timestamp,
                    snapshot=snapshot,
                    confirmed_regime=confirmed_regime,
                    position=position,
                    entry_decision=entry_decision if position is None else scale_decision,
                    exit_decision=exit_decision,
                )
            )
            if action is not None:
                actions.append(action)
        return actions, decisions

    def _build_decision_log(
        self,
        timestamp: int,
        snapshot: MarketSnapshot,
        confirmed_regime: str,
        position: Position | None,
        entry_decision: PlanDecision,
        exit_decision: PlanDecision,
    ) -> DecisionLog:
        current_position = position or self.state_store.get(snapshot.symbol)
        return DecisionLog(
            timestamp=timestamp,
            symbol=snapshot.symbol,
            price=self._format_decimal(snapshot.mark_price, "0.0000"),
            market_regime=confirmed_regime,
            trend_direction=self._trend_direction(snapshot),
            long_score=snapshot.long_score,
            short_score=snapshot.short_score,
            entry_side=(entry_decision.plan.position_side if entry_decision.plan is not None else (snapshot.preferred_side or "NONE")),
            entry_allowed=entry_decision.allowed,
            entry_reason=entry_decision.reason,
            entry_detail_reasons=entry_decision.detail_reasons,
            exit_allowed=exit_decision.allowed,
            exit_reason=exit_decision.reason,
            exit_detail_reasons=exit_decision.detail_reasons,
            tp_price=self._format_decimal(current_position.take_profit_price_decimal, "0.0000") if current_position is not None and current_position.has_exit_lines else "0.0000",
            sl_price=self._format_decimal(current_position.stop_loss_price_decimal, "0.0000") if current_position is not None and current_position.has_exit_lines else "0.0000",
            rsi_1m=self._format_decimal(snapshot.rsi_1m, "0.00"),
            rsi_3m=self._format_decimal(snapshot.rsi_3m, "0.00"),
            ema_fast_1m=self._format_decimal(snapshot.ema_fast_1m, "0.0000"),
            ema_slow_1m=self._format_decimal(snapshot.ema_slow_1m, "0.0000"),
            atr_3m=self._format_decimal(snapshot.atr_3m, "0.0000"),
            atr_15m=self._format_decimal(snapshot.atr_15m, "0.0000"),
            volume_ratio=self._format_decimal(snapshot.volume_ratio, "0.00"),
            breakout_high=self._format_decimal(snapshot.breakout_high, "0.0000"),
            breakout_low=self._format_decimal(snapshot.breakout_low, "0.0000"),
            recent_high=self._format_decimal(snapshot.recent_high, "0.0000"),
            recent_low=self._format_decimal(snapshot.recent_low, "0.0000"),
            position_state=self._position_state_payload(current_position),
        )

    def _plan_to_action(self, plan: StrategyOrderPlan | None, snapshot: MarketSnapshot) -> StrategyAction | None:
        if plan is None:
            return None
        side = plan.position_side if plan.kind != "exit" else plan.order_side
        return StrategyAction(
            kind=plan.kind,
            symbol=plan.symbol,
            side=side,
            quantity=plan.quantity,
            margin_usdt=plan.margin_usdt,
            reason=plan.reason,
            snapshot=snapshot,
        )

    def _trend_direction(self, snapshot: MarketSnapshot) -> str:
        if snapshot.trend_long_ok and not snapshot.trend_short_ok:
            return "LONG"
        if snapshot.trend_short_ok and not snapshot.trend_long_ok:
            return "SHORT"
        return "NEUTRAL"

    def _position_state_payload(self, position: Position | None) -> dict[str, str | int | bool]:
        if position is None:
            return {"open": False}
        return {
            "open": True,
            "side": position.side,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "entry_count": position.entry_count,
            "exit_count": position.exit_count,
            "trend_follow_armed": position.trend_follow_armed,
        }

    def on_entry_fill(self, action: StrategyAction, fill_price: Decimal, quantity: Decimal, timestamp: int) -> Position:
        take_profit_price, stop_loss_price, take_profit_pct, stop_loss_pct = self._calculate_exit_lines(action.snapshot, action.side, fill_price, self.settings.leverage)
        position = self._build_position(
            symbol=action.symbol,
            side=action.side,
            quantity=quantity,
            entry_price=fill_price,
            order_id=f"backtest-entry-{action.symbol}-{timestamp}",
            opened_at=self._now_utc().isoformat(),
            leverage=self.settings.leverage,
            margin_usdt=action.margin_usdt,
            notional_usdt=quantity * fill_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            entry_count=1,
            exit_count=0,
        )
        self.state_store.set(position)
        return position

    def on_scale_in_fill(self, action: StrategyAction, fill_price: Decimal, quantity: Decimal, timestamp: int) -> Position:
        position = self.state_store.get(action.symbol)
        if position is None:
            return self.on_entry_fill(action, fill_price, quantity, timestamp)
        total_quantity = position.quantity_decimal + quantity
        total_notional = position.notional_usdt_decimal + (quantity * fill_price)
        average_entry = total_notional / total_quantity
        take_profit_price, stop_loss_price, take_profit_pct, stop_loss_pct = self._calculate_exit_lines(action.snapshot, action.side, average_entry, position.leverage)
        updated = self._build_position(
            symbol=position.symbol,
            side=position.side,
            quantity=total_quantity,
            entry_price=average_entry,
            order_id=f"backtest-scale-{action.symbol}-{timestamp}",
            opened_at=position.opened_at,
            leverage=position.leverage,
            margin_usdt=position.margin_usdt_decimal + action.margin_usdt,
            notional_usdt=total_notional,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            entry_count=position.entry_count + 1,
            exit_count=position.exit_count,
            realized_pnl_usdt=position.realized_pnl_usdt_decimal,
            commission_usdt=position.commission_usdt_decimal,
            last_exit_update_at=self._now_utc().isoformat(),
            last_trade_sync_at=position.last_trade_sync_at,
        )
        self.state_store.set(updated)
        return updated

    def on_exit_fill(self, symbol: str) -> None:
        self.state_store.remove(symbol)