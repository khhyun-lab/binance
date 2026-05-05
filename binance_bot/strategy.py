from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from binance_bot.config import Settings
from binance_bot.indicators import atr, ema, rsi, sma
from binance_bot.state import Position, StateStore
from binance_bot.services.binance_futures_service import AccountSnapshot, BinanceFuturesService, PositionRisk
from upbit_bot.heartbeat import HeartbeatWriter
from upbit_bot.telegram import TelegramNotifier, format_telegram_message


@dataclass(slots=True)
class MarketSnapshot:
    symbol: str
    mark_price: Decimal
    ask_price: Decimal
    bid_price: Decimal
    ema_fast_1m: Decimal
    ema_slow_1m: Decimal
    long_score: int
    short_score: int
    long_reasons: list[str]
    short_reasons: list[str]
    volume_ratio: Decimal
    rsi_1m: Decimal
    rsi_3m: Decimal
    atr_3m: Decimal
    atr_15m: Decimal
    recent_high: Decimal
    recent_low: Decimal
    breakout_high: Decimal
    breakout_low: Decimal
    trend_long_ok: bool
    trend_short_ok: bool

    @property
    def preferred_side(self) -> str | None:
        if self.long_score > self.short_score and self.long_score > 0:
            return "LONG"
        if self.short_score > self.long_score and self.short_score > 0:
            return "SHORT"
        return None


class StrategyEngine:
    def __init__(
        self,
        settings: Settings,
        service: BinanceFuturesService,
        state_store: StateStore,
        notifier: TelegramNotifier,
        heartbeat: HeartbeatWriter,
    ) -> None:
        self.settings = settings
        self.service = service
        self.state_store = state_store
        self.notifier = notifier
        self.heartbeat = heartbeat
        self.logger = logging.getLogger(self.__class__.__name__)

    def _resolve_margin_usdt(self, available_balance: Decimal) -> Decimal:
        if self.settings.use_available_balance:
            return available_balance
        return min(available_balance, self.settings.margin_per_trade_usdt)

    def _resolve_entry_margin_usdt(self, available_balance: Decimal, position: Position | None) -> Decimal:
        total_margin = self._resolve_margin_usdt(available_balance)
        if position is None:
            remaining_splits = self.settings.entry_splits
        else:
            remaining_splits = max(1, self.settings.entry_splits - position.entry_count)
        return total_margin / Decimal(remaining_splits)

    async def _resolve_entry_order_plan(
        self,
        symbol: str,
        price: Decimal,
        available_balance: Decimal,
        leverage: int,
        position: Position | None,
    ) -> tuple[Decimal, Decimal, int]:
        total_margin = self._resolve_margin_usdt(available_balance)
        if total_margin <= 0 or price <= 0:
            return Decimal("0"), Decimal("0"), 1

        if position is None:
            remaining_splits = self.settings.entry_splits
        else:
            remaining_splits = max(1, self.settings.entry_splits - position.entry_count)

        rules = await self.service.get_symbol_rules(symbol)
        effective_remaining_splits = remaining_splits
        quantity = Decimal("0")

        while effective_remaining_splits >= 1:
            margin_per_order = total_margin / Decimal(effective_remaining_splits)
            notional = margin_per_order * Decimal(leverage)
            quantity = self.service.normalize_quantity(rules, notional / price)
            if quantity >= rules.min_qty and (quantity * price) >= rules.min_notional:
                if effective_remaining_splits != remaining_splits:
                    self.logger.info(
                        "[BINANCE ENTRY SPLIT ADJUST] symbol=%s requested_remaining_splits=%s effective_remaining_splits=%s total_margin=%s margin_per_order=%s min_qty=%s min_notional=%s",
                        symbol,
                        remaining_splits,
                        effective_remaining_splits,
                        self._format_decimal(total_margin, "0.00"),
                        self._format_decimal(margin_per_order, "0.00"),
                        self._format_decimal(rules.min_qty, "0.000"),
                        self._format_decimal(rules.min_notional, "0.00"),
                    )
                return margin_per_order, quantity, effective_remaining_splits
            effective_remaining_splits -= 1

        return total_margin, Decimal("0"), 1

    def _resolve_exit_quantity(self, position: Position) -> Decimal:
        remaining_splits = max(1, self.settings.exit_splits - position.exit_count)
        return position.quantity_decimal / Decimal(remaining_splits)

    def _build_position(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        entry_price: Decimal,
        order_id: str,
        opened_at: str,
        leverage: int,
        margin_usdt: Decimal,
        notional_usdt: Decimal,
        take_profit_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_pct: Decimal,
        stop_loss_pct: Decimal,
        entry_count: int,
        exit_count: int,
        realized_pnl_usdt: Decimal = Decimal("0"),
        commission_usdt: Decimal = Decimal("0"),
        last_exit_update_at: str = "",
        last_trade_sync_at: str = "",
    ) -> Position:
        return Position(
            symbol=symbol,
            side=side,
            quantity=str(quantity),
            entry_price=str(entry_price),
            order_id=order_id,
            opened_at=opened_at,
            leverage=leverage,
            margin_usdt=str(margin_usdt),
            notional_usdt=str(notional_usdt),
            take_profit_price=str(take_profit_price),
            stop_loss_price=str(stop_loss_price),
            take_profit_pct=str(take_profit_pct),
            stop_loss_pct=str(stop_loss_pct),
            realized_pnl_usdt=str(realized_pnl_usdt),
            commission_usdt=str(commission_usdt),
            last_exit_update_at=last_exit_update_at,
            last_trade_sync_at=last_trade_sync_at,
            trend_follow_armed=False,
            entry_count=entry_count,
            exit_count=exit_count,
        )

    def _format_decimal(self, value: Decimal, digits: str = "0.0000") -> str:
        return f"{value.quantize(Decimal(digits))}"

    def _format_line_delta(self, previous_value: Decimal, next_value: Decimal, digits: str = "0.0000") -> str:
        delta = next_value - previous_value
        return f"{delta.quantize(Decimal(digits)):+f}"

    def _margin_pct_to_usdt(self, margin_usdt: Decimal, margin_pct: Decimal) -> Decimal:
        return (margin_usdt * margin_pct).copy_abs()

    def _now_utc(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    def _is_exit_line_refresh_due(self, last_exit_update_at: str) -> bool:
        if self.settings.exit_line_refresh_seconds <= 0:
            return False
        if not last_exit_update_at:
            return True
        try:
            last_updated_at = datetime.fromisoformat(last_exit_update_at)
        except ValueError:
            return True
        return self._now_utc() - last_updated_at >= timedelta(seconds=self.settings.exit_line_refresh_seconds)

    def _round_trip_fee_pct_for_leverage(self, leverage: int) -> Decimal:
        return self.settings.round_trip_fee_pct * Decimal(leverage)

    def _single_side_fee_pct_for_leverage(self, leverage: int) -> Decimal:
        return self._round_trip_fee_pct_for_leverage(leverage) / Decimal("2")

    def _position_trade_margin_terms(self, position: Position | None, leverage: int) -> tuple[Decimal, Decimal, Decimal]:
        remaining_exit_fee_margin_pct = self._single_side_fee_pct_for_leverage(leverage)
        if position is None or position.margin_usdt_decimal <= 0:
            return Decimal("0"), Decimal("0"), remaining_exit_fee_margin_pct
        realized_margin_pct = position.realized_pnl_usdt_decimal / position.margin_usdt_decimal
        paid_fee_margin_pct = position.commission_usdt_decimal / position.margin_usdt_decimal
        return realized_margin_pct, paid_fee_margin_pct, remaining_exit_fee_margin_pct

    def _take_profit_move_with_trade_costs(self, target_margin_pct: Decimal, leverage: int, position: Position | None) -> Decimal:
        if position is None:
            return self._price_move_from_margin_target(target_margin_pct, leverage)
        realized_margin_pct, paid_fee_margin_pct, remaining_exit_fee_margin_pct = self._position_trade_margin_terms(position, leverage)
        required_margin_pct = target_margin_pct - realized_margin_pct + paid_fee_margin_pct + remaining_exit_fee_margin_pct
        if required_margin_pct <= 0:
            return Decimal("0")
        return required_margin_pct / Decimal(leverage)

    def _stop_loss_move_with_trade_costs(self, target_margin_pct: Decimal, leverage: int, position: Position | None) -> Decimal:
        if position is None:
            return self._price_move_from_margin_target(target_margin_pct, leverage)
        realized_margin_pct, paid_fee_margin_pct, remaining_exit_fee_margin_pct = self._position_trade_margin_terms(position, leverage)
        remaining_loss_budget_pct = target_margin_pct + realized_margin_pct - paid_fee_margin_pct - remaining_exit_fee_margin_pct
        clamped_loss_budget_pct = min(target_margin_pct, remaining_loss_budget_pct)
        minimum_loss_budget_pct = max(target_margin_pct * Decimal("0.35"), Decimal("0.0010"))
        if clamped_loss_budget_pct <= 0:
            clamped_loss_budget_pct = minimum_loss_budget_pct
        else:
            clamped_loss_budget_pct = max(clamped_loss_budget_pct, minimum_loss_budget_pct)
        return clamped_loss_budget_pct / Decimal(leverage)

    def _cycle_net_margin_pct(self, position: Position | None, gross_margin_pct: Decimal, leverage: int) -> Decimal:
        realized_margin_pct, paid_fee_margin_pct, remaining_exit_fee_margin_pct = self._position_trade_margin_terms(position, leverage)
        return gross_margin_pct + realized_margin_pct - paid_fee_margin_pct - remaining_exit_fee_margin_pct

    def _clamp_pct(self, value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
        return min(max(value, minimum), maximum)

    def _reward_risk_take_profit_target_pct(
        self,
        stop_loss_pct: Decimal,
        minimum_take_profit_pct: Decimal,
        maximum_take_profit_pct: Decimal,
    ) -> Decimal:
        base_target = abs(stop_loss_pct) * self.settings.exit_reward_risk_ratio
        return self._clamp_pct(base_target, minimum_take_profit_pct, maximum_take_profit_pct)

    def _take_profit_price_from_target_pct(
        self,
        side: str,
        entry_price: Decimal,
        leverage: int,
        position: Position | None,
        target_take_profit_pct: Decimal,
    ) -> tuple[Decimal, Decimal]:
        take_profit_move = self._take_profit_move_with_trade_costs(target_take_profit_pct, leverage, position)
        if side == "LONG":
            take_profit_price = entry_price * (Decimal("1") + take_profit_move)
            take_profit_pct = self._cycle_net_margin_pct(position, ((take_profit_price / entry_price) - Decimal("1")) * Decimal(leverage), leverage)
        else:
            take_profit_price = entry_price * (Decimal("1") - take_profit_move)
            take_profit_pct = self._cycle_net_margin_pct(position, ((Decimal("1") - (take_profit_price / entry_price)) * Decimal(leverage)), leverage)
        return take_profit_price, take_profit_pct

    def _current_net_margin_pct(self, snapshot: MarketSnapshot, position: Position) -> Decimal:
        current_price = snapshot.bid_price if position.side == "LONG" else snapshot.ask_price
        if position.side == "LONG":
            gross_pnl = ((current_price - position.entry_price_decimal) / position.entry_price_decimal) * Decimal(position.leverage)
        else:
            gross_pnl = ((position.entry_price_decimal - current_price) / position.entry_price_decimal) * Decimal(position.leverage)
        return self._cycle_net_margin_pct(position, gross_pnl, position.leverage)

    async def _sync_position_trade_metrics(self, position: Position) -> Position:
        if self.settings.dry_run or not self.settings.api_key or not self.settings.api_secret:
            return position

        try:
            opened_at = datetime.fromisoformat(position.opened_at)
        except ValueError:
            return position

        start_time_ms = int(opened_at.timestamp() * 1000)
        trades = await self.service.get_user_trades(position.symbol, start_time_ms=start_time_ms, limit=200)
        realized_pnl_usdt = sum((trade.realized_pnl for trade in trades), Decimal("0"))
        commission_usdt = sum(
            (trade.commission for trade in trades if trade.commission_asset == "USDT"),
            Decimal("0"),
        )
        synced_at = self._now_utc().isoformat()

        if (
            realized_pnl_usdt == position.realized_pnl_usdt_decimal
            and commission_usdt == position.commission_usdt_decimal
            and position.last_trade_sync_at
        ):
            return position

        updated = Position(
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            order_id=position.order_id,
            opened_at=position.opened_at,
            leverage=position.leverage,
            margin_usdt=position.margin_usdt,
            notional_usdt=position.notional_usdt,
            take_profit_price=position.take_profit_price,
            stop_loss_price=position.stop_loss_price,
            take_profit_pct=position.take_profit_pct,
            stop_loss_pct=position.stop_loss_pct,
            realized_pnl_usdt=str(realized_pnl_usdt),
            commission_usdt=str(commission_usdt),
            last_exit_update_at=position.last_exit_update_at,
            last_trade_sync_at=synced_at,
            trend_follow_armed=position.trend_follow_armed,
            entry_count=position.entry_count,
            exit_count=position.exit_count,
        )
        self.state_store.set(updated)
        self.logger.info(
            "[BINANCE TRADE METRICS] symbol=%s realized_pnl_usdt=%s commission_usdt=%s trades=%s",
            position.symbol,
            self._format_decimal(realized_pnl_usdt, "0.0000"),
            self._format_decimal(commission_usdt, "0.0000"),
            len(trades),
        )
        return updated

    def _entry_score_threshold_for_side(self, side: str) -> int:
        if side == "SHORT":
            return self.settings.short_entry_score_threshold
        return self.settings.long_entry_score_threshold

    def _recent_crash_context_key(self, symbol: str) -> str:
        return f"recent_crash_context:{symbol}"

    def _mark_recent_crash_context(self, snapshot: MarketSnapshot) -> None:
        self.state_store.set_metadata(self._recent_crash_context_key(snapshot.symbol), self._now_utc().isoformat())

    def _clear_recent_crash_context(self, symbol: str) -> None:
        self.state_store.set_metadata(self._recent_crash_context_key(symbol), "")

    def _has_recent_crash_context(self, symbol: str, window_seconds: int = 480) -> bool:
        raw_value = self.state_store.get_metadata(self._recent_crash_context_key(symbol)) or ""
        if not raw_value:
            return False
        try:
            marked_at = datetime.fromisoformat(raw_value)
        except ValueError:
            return False
        return (self._now_utc() - marked_at) <= timedelta(seconds=window_seconds)

    def _is_crash_short_context(self, snapshot: MarketSnapshot) -> bool:
        breakout_pressure = snapshot.mark_price <= snapshot.breakout_low or snapshot.mark_price <= (snapshot.recent_low + (snapshot.atr_3m * Decimal("0.12")))
        momentum_pressure = snapshot.ema_fast_1m < snapshot.ema_slow_1m and snapshot.short_score >= max(3, self._entry_score_threshold_for_side("SHORT") - 1)
        oversold_pressure = snapshot.rsi_1m <= Decimal("38") and snapshot.rsi_3m <= Decimal("42")
        volume_pressure = snapshot.volume_ratio >= Decimal("1.40")
        return breakout_pressure and momentum_pressure and oversold_pressure and volume_pressure

    def _get_crash_short_signal(self, snapshot: MarketSnapshot) -> tuple[str, list[str]] | None:
        if not self._is_crash_short_context(snapshot):
            return None
        self._mark_recent_crash_context(snapshot)
        return (
            "SHORT",
            [
                "급락선행숏",
                f"short_score={snapshot.short_score}",
                f"rsi_1m={self._format_decimal(snapshot.rsi_1m, '0.00')}",
                f"volume={self._format_decimal(snapshot.volume_ratio, '0.00')}",
            ],
        )

    def _get_rebound_long_signal(self, snapshot: MarketSnapshot) -> tuple[str, list[str]] | None:
        if not self._has_recent_crash_context(snapshot.symbol):
            return None
        if self._is_crash_short_context(snapshot):
            return None
        if snapshot.trend_short_ok and snapshot.short_score > snapshot.long_score + 1:
            return None
        rebound_ready = (
            snapshot.ema_fast_1m > snapshot.ema_slow_1m
            and snapshot.long_score >= 2
            and snapshot.rsi_1m >= Decimal("22")
            and snapshot.rsi_1m <= Decimal("48")
            and snapshot.rsi_3m >= Decimal("28")
            and snapshot.volume_ratio >= Decimal("0.40")
            and snapshot.mark_price >= (snapshot.recent_low + (snapshot.atr_3m * Decimal("0.20")))
        )
        if not rebound_ready:
            return None
        return (
            "LONG",
            [
                "급락후반등롱",
                f"long_score={snapshot.long_score}",
                f"rsi_1m={self._format_decimal(snapshot.rsi_1m, '0.00')}",
                f"volume={self._format_decimal(snapshot.volume_ratio, '0.00')}",
            ],
        )

    def _has_entry_momentum(self, snapshot: MarketSnapshot, side: str) -> bool:
        threshold = self._entry_score_threshold_for_side(side)
        if side == "LONG":
            conditions = [
                snapshot.mark_price >= snapshot.breakout_high,
                snapshot.volume_ratio >= self.settings.entry_min_volume_ratio,
                snapshot.rsi_3m >= self.settings.long_entry_min_rsi_3m,
                snapshot.long_score >= threshold + 1,
            ]
            score_edge_ok = snapshot.long_score >= snapshot.short_score + 2
            return (
                snapshot.trend_long_ok
                and score_edge_ok
                and snapshot.ema_fast_1m > snapshot.ema_slow_1m
                and sum(1 for condition in conditions if condition) >= self.settings.entry_momentum_min_conditions
            )

        conditions = [
            snapshot.mark_price <= snapshot.breakout_low,
            snapshot.volume_ratio >= self.settings.entry_min_volume_ratio,
            snapshot.rsi_3m <= self.settings.short_entry_max_rsi_3m,
            snapshot.short_score >= threshold + 1,
        ]
        score_edge_ok = snapshot.short_score >= snapshot.long_score + 2
        return (
            snapshot.trend_short_ok
            and score_edge_ok
            and snapshot.ema_fast_1m < snapshot.ema_slow_1m
            and sum(1 for condition in conditions if condition) >= self.settings.entry_momentum_min_conditions
        )

    def _is_exhausted_entry(self, snapshot: MarketSnapshot, side: str) -> bool:
        if side == "LONG":
            return snapshot.rsi_3m >= Decimal("72") and not snapshot.trend_long_ok
        return snapshot.rsi_3m <= Decimal("28") and not snapshot.trend_short_ok

    def _has_reverse_exit_confirmation(self, snapshot: MarketSnapshot, position: Position, preferred_side: str | None, reverse_score: int) -> bool:
        if preferred_side is None or preferred_side == position.side:
            return False

        threshold = self._entry_score_threshold_for_side(preferred_side) + 1
        if reverse_score < threshold:
            return False

        if position.side == "LONG":
            return (
                snapshot.trend_short_ok
                or (
                    snapshot.mark_price <= snapshot.breakout_low
                    and snapshot.ema_fast_1m < snapshot.ema_slow_1m
                    and snapshot.rsi_3m <= Decimal("45")
                )
            )

        return (
            snapshot.trend_long_ok
            or (
                snapshot.mark_price >= snapshot.breakout_high
                and snapshot.ema_fast_1m > snapshot.ema_slow_1m
                and snapshot.rsi_3m >= Decimal("55")
            )
        )

    def _can_scale_in_position(self, snapshot: MarketSnapshot, position: Position) -> bool:
        if position.side == "LONG":
            price_ok = snapshot.mark_price >= position.entry_price_decimal
            trend_ok = self._is_strong_long_trend(snapshot)
        else:
            price_ok = snapshot.mark_price <= position.entry_price_decimal
            trend_ok = snapshot.trend_short_ok and snapshot.short_score >= self._entry_score_threshold_for_side("SHORT") + 1 and snapshot.ema_fast_1m < snapshot.ema_slow_1m
        return price_ok and trend_ok

    def _has_near_target_fade_exit(
        self,
        snapshot: MarketSnapshot,
        position: Position,
        current_price: Decimal,
        net_pnl: Decimal,
    ) -> bool:
        if net_pnl < Decimal("0.0010"):
            return False

        if position.side == "LONG":
            target_span = position.take_profit_price_decimal - position.entry_price_decimal
            if target_span <= 0:
                return False
            progress = (current_price - position.entry_price_decimal) / target_span
            momentum_faded = (
                snapshot.ema_fast_1m <= snapshot.ema_slow_1m
                or snapshot.long_score <= snapshot.short_score + 1
                or snapshot.rsi_3m < Decimal("58")
            )
            return progress >= Decimal("0.45") and momentum_faded

        target_span = position.entry_price_decimal - position.take_profit_price_decimal
        if target_span <= 0:
            return False
        progress = (position.entry_price_decimal - current_price) / target_span
        momentum_faded = (
            snapshot.ema_fast_1m >= snapshot.ema_slow_1m
            or snapshot.short_score <= snapshot.long_score + 1
            or snapshot.rsi_3m > Decimal("42")
        )
        return progress >= Decimal("0.45") and momentum_faded

    def _sideways_target_price_move(self, leverage: int) -> Decimal:
        return self._price_move_from_margin_target(self.settings.sideways_take_profit_on_margin_pct, leverage)

    def _sideways_range_ratio(self, snapshot: MarketSnapshot) -> Decimal:
        if snapshot.recent_high <= snapshot.recent_low or snapshot.mark_price <= 0:
            return Decimal("0")
        return (snapshot.recent_high - snapshot.recent_low) / snapshot.mark_price

    def _sideways_range_threshold(self) -> Decimal:
        return self._sideways_target_price_move(self.settings.leverage) * Decimal("1.8")

    def _classify_market_regime(self, snapshot: MarketSnapshot) -> str:
        if snapshot.trend_long_ok or snapshot.trend_short_ok:
            return "TREND"
        if snapshot.volume_ratio > self.settings.sideways_max_volume_ratio:
            return "TREND"

        breakout_buffer = snapshot.atr_3m * self.settings.sideways_entry_buffer_atr_multiplier
        if snapshot.mark_price >= snapshot.breakout_high + breakout_buffer:
            return "TREND"
        if snapshot.mark_price <= snapshot.breakout_low - breakout_buffer:
            return "TREND"

        range_ratio = self._sideways_range_ratio(snapshot)
        if range_ratio <= 0:
            return "TREND"
        if range_ratio >= self._sideways_range_threshold():
            return "SIDEWAYS"
        return "STAGNATION"

    def _is_raw_sideways_regime(self, snapshot: MarketSnapshot) -> bool:
        return self._classify_market_regime(snapshot) == "SIDEWAYS"

    def _is_stagnation_regime(self, snapshot: MarketSnapshot) -> bool:
        return (self.state_store.get_metadata(f"market_regime_state:{snapshot.symbol}") or "TREND") == "STAGNATION"

    def _is_sideways_regime(self, snapshot: MarketSnapshot) -> bool:
        return (self.state_store.get_metadata(f"market_regime_state:{snapshot.symbol}") or "TREND") == "SIDEWAYS"

    def _update_market_regime_state(self, snapshot: MarketSnapshot) -> tuple[str, bool]:
        state_key = f"market_regime_state:{snapshot.symbol}"
        candidate_key = f"market_regime_candidate:{snapshot.symbol}"
        counter_key = f"market_regime_counter:{snapshot.symbol}"
        previous_state = self.state_store.get_metadata(state_key) or "TREND"
        candidate_state = self.state_store.get_metadata(candidate_key) or previous_state
        counter_raw = self.state_store.get_metadata(counter_key) or "0"
        raw_state = self._classify_market_regime(snapshot)

        try:
            counter = int(counter_raw)
        except ValueError:
            counter = 0

        if raw_state == previous_state:
            confirmed_state = previous_state
            candidate_state = raw_state
            counter = 0
        else:
            if candidate_state == raw_state:
                counter += 1
            else:
                candidate_state = raw_state
                counter = 1

            required_cycles = self.settings.sideways_regime_release_cycles if raw_state == "TREND" else self.settings.sideways_regime_confirm_cycles
            if counter >= required_cycles:
                confirmed_state = raw_state
                candidate_state = raw_state
                counter = 0
            else:
                confirmed_state = previous_state

        self.state_store.set_metadata(state_key, confirmed_state)
        self.state_store.set_metadata(candidate_key, candidate_state)
        self.state_store.set_metadata(counter_key, str(counter))
        return confirmed_state, confirmed_state != previous_state

    def _build_market_regime_reasons(self, snapshot: MarketSnapshot, regime: str) -> list[str]:
        range_ratio = self._sideways_range_ratio(snapshot) * Decimal("100")
        threshold_ratio = self._sideways_range_threshold() * Decimal("100")
        reasons = [
            f"박스폭={self._format_decimal(range_ratio, '0.00')}%",
            f"횡보최소폭={self._format_decimal(threshold_ratio, '0.00')}%",
            f"거래량비율={self._format_decimal(snapshot.volume_ratio, '0.00')}",
        ]
        if regime == "STAGNATION":
            reasons.insert(0, "박스폭 부족으로 거래 비권장")
        elif regime == "SIDEWAYS":
            reasons.insert(0, "박스폭 충족으로 거래 가능한 횡보")
        else:
            reasons.insert(0, "횡보 조건 미충족")
        return reasons

    def _get_sideways_entry_signal(self, snapshot: MarketSnapshot) -> tuple[str, list[str]] | None:
        if not self.settings.sideways_trade_enabled:
            return None
        if not self._is_sideways_regime(snapshot):
            return None
        if snapshot.volume_ratio >= Decimal("0.85"):
            return None

        buffer = snapshot.atr_3m * self.settings.sideways_entry_buffer_atr_multiplier
        target_move = self._sideways_target_price_move(self.settings.leverage)
        upside_room = (snapshot.recent_high - snapshot.mark_price) / snapshot.mark_price if snapshot.mark_price > 0 else Decimal("0")
        downside_room = (snapshot.mark_price - snapshot.recent_low) / snapshot.mark_price if snapshot.mark_price > 0 else Decimal("0")
        required_room = target_move * Decimal("1.35")

        if (
            snapshot.mark_price <= snapshot.recent_low + buffer
            and snapshot.rsi_3m <= self.settings.sideways_long_entry_max_rsi_3m
            and snapshot.ema_fast_1m >= snapshot.ema_slow_1m * Decimal("0.998")
            and upside_room >= required_room
        ):
            return (
                "LONG",
                [
                    "횡보하단반등대기",
                    f"RSI={self._format_decimal(snapshot.rsi_3m, '0.00')}",
                    f"room={self._format_decimal(upside_room * Decimal('100'), '0.00')}%",
                ],
            )

        if (
            snapshot.mark_price >= snapshot.recent_high - buffer
            and snapshot.rsi_3m >= self.settings.sideways_short_entry_min_rsi_3m
            and snapshot.ema_fast_1m <= snapshot.ema_slow_1m * Decimal("1.002")
            and downside_room >= required_room
        ):
            return (
                "SHORT",
                [
                    "횡보상단되돌림대기",
                    f"RSI={self._format_decimal(snapshot.rsi_3m, '0.00')}",
                    f"room={self._format_decimal(downside_room * Decimal('100'), '0.00')}%",
                ],
            )

        return None

    async def _maybe_alert_sideways_regime(self, snapshot: MarketSnapshot) -> None:
        state_key = f"market_regime_state:{snapshot.symbol}"
        timestamp_key = f"market_regime_alert_at:{snapshot.symbol}"
        confirmed_state, state_changed = self._update_market_regime_state(snapshot)

        if confirmed_state == "TREND":
            if (self.state_store.get_metadata(state_key) or "TREND") != "TREND":
                self.state_store.set_metadata(state_key, "TREND")
            return

        now = self._now_utc()
        last_alert_raw = self.state_store.get_metadata(timestamp_key) or ""
        last_alert_at: datetime | None = None
        if last_alert_raw:
            try:
                last_alert_at = datetime.fromisoformat(last_alert_raw)
            except ValueError:
                last_alert_at = None

        cooldown_elapsed = (
            last_alert_at is None
            or self.settings.sideways_alert_cooldown_seconds <= 0
            or (now - last_alert_at) >= timedelta(seconds=self.settings.sideways_alert_cooldown_seconds)
        )
        if not cooldown_elapsed and not state_changed:
            return

        signal = self._get_sideways_entry_signal(snapshot) if confirmed_state == "SIDEWAYS" else None
        candidate_side = signal[0] if signal is not None else "NONE"
        candidate_reasons = signal[1] if signal is not None else self._build_market_regime_reasons(snapshot, confirmed_state)
        range_ratio = self._sideways_range_ratio(snapshot) * Decimal("100")
        self.state_store.set_metadata(timestamp_key, now.isoformat())
        alert_title = "[Binance 횡보장 감지]" if confirmed_state == "SIDEWAYS" else "[Binance 정체 구간 감지]"
        self.logger.info(
            "[BINANCE REGIME] symbol=%s regime=%s mark_price=%s range_pct=%s volume_ratio=%s candidate_side=%s",
            snapshot.symbol,
            confirmed_state,
            self._format_decimal(snapshot.mark_price, "0.0000"),
            self._format_decimal(range_ratio, "0.00"),
            self._format_decimal(snapshot.volume_ratio, "0.00"),
            candidate_side,
        )
        await self.notifier.send(
            format_telegram_message(
                alert_title,
                fields=[
                    ("심볼", snapshot.symbol),
                    ("상태", confirmed_state),
                    ("후보 방향", candidate_side),
                    ("현재가", f"{snapshot.mark_price:.4f}"),
                    ("박스 상단", f"{snapshot.recent_high:.4f}"),
                    ("박스 하단", f"{snapshot.recent_low:.4f}"),
                    ("박스 폭", f"{range_ratio:.2f}%"),
                    ("횡보 최소폭", f"{self._sideways_range_threshold() * Decimal('100'):.2f}%"),
                    ("거래량 비율", f"{snapshot.volume_ratio:.2f}"),
                    ("RSI", f"{snapshot.rsi_3m:.2f}"),
                    ("알림 쿨다운", f"{self.settings.sideways_alert_cooldown_seconds}초"),
                ],
                sections=[("판단 근거", candidate_reasons)],
            )
        )

    def _price_move_from_margin_target(self, target_margin_pct: Decimal, leverage: int) -> Decimal:
        return (target_margin_pct + self._round_trip_fee_pct_for_leverage(leverage)) / Decimal(leverage)

    async def _resolve_effective_exit_quantity(self, position: Position, requested_quantity: Decimal) -> Decimal:
        rules = await self.service.get_symbol_rules(position.symbol)
        normalized_requested = self.service.normalize_quantity(rules, requested_quantity)
        normalized_full = self.service.normalize_quantity(rules, position.quantity_decimal)
        if normalized_requested <= 0:
            return normalized_full
        if normalized_requested * position.entry_price_decimal < rules.min_notional:
            return normalized_full
        return normalized_requested

    async def _ensure_entry_leverage(self, symbol: str, leverage: int) -> None:
        if self.settings.dry_run or not self.settings.api_key or not self.settings.api_secret:
            return
        await self.service.set_leverage(symbol, leverage)

    def _is_strong_long_trend(self, snapshot: MarketSnapshot) -> bool:
        threshold = self._entry_score_threshold_for_side("LONG")
        score_ok = snapshot.long_score >= threshold + 2
        momentum_ok = snapshot.volume_ratio >= Decimal("1.05") or snapshot.rsi_3m >= Decimal("61")
        breakout_ok = snapshot.mark_price >= snapshot.breakout_high or snapshot.mark_price >= snapshot.recent_high
        micro_trend_ok = snapshot.ema_fast_1m > snapshot.ema_slow_1m and snapshot.mark_price >= snapshot.ema_fast_1m
        return snapshot.trend_long_ok and snapshot.long_score > snapshot.short_score and score_ok and momentum_ok and breakout_ok and micro_trend_ok

    def _is_long_trend_bent(self, snapshot: MarketSnapshot) -> bool:
        threshold = self._entry_score_threshold_for_side("LONG")
        score_bent = snapshot.long_score < threshold or snapshot.long_score <= snapshot.short_score
        micro_bent = snapshot.ema_fast_1m <= snapshot.ema_slow_1m or snapshot.mark_price < snapshot.ema_slow_1m or snapshot.rsi_3m < Decimal("54")
        return (not snapshot.trend_long_ok) or score_bent or micro_bent

    def _build_exit_line_update_reasons(
        self,
        snapshot: MarketSnapshot,
        position: Position,
        previous_take_profit_price: Decimal,
        next_take_profit_price: Decimal,
        previous_stop_loss_price: Decimal,
        next_stop_loss_price: Decimal,
    ) -> list[str]:
        if not position.has_exit_lines:
            return ["초기 라인 설정"]

        reasons: list[str] = []
        volatility_ratio = snapshot.atr_3m / position.entry_price_decimal if position.entry_price_decimal > 0 else Decimal("0")
        if snapshot.volume_ratio >= Decimal("1.8") or volatility_ratio >= Decimal("0.0035"):
            reasons.append(f"고변동 유지 vol={self._format_decimal(snapshot.volume_ratio, '0.00')} atr={self._format_decimal(volatility_ratio * Decimal('100'), '0.00')}%")
        elif snapshot.volume_ratio >= Decimal("1.2") or volatility_ratio >= Decimal("0.0023"):
            reasons.append(f"중변동 vol={self._format_decimal(snapshot.volume_ratio, '0.00')} atr={self._format_decimal(volatility_ratio * Decimal('100'), '0.00')}%")
        else:
            reasons.append(f"저변동 vol={self._format_decimal(snapshot.volume_ratio, '0.00')} atr={self._format_decimal(volatility_ratio * Decimal('100'), '0.00')}%")

        if position.side == "LONG":
            if next_take_profit_price > previous_take_profit_price:
                reasons.append("익절 상향")
            elif next_take_profit_price < previous_take_profit_price:
                reasons.append("익절 하향")
            else:
                reasons.append("익절라인 유지")

            if next_stop_loss_price > previous_stop_loss_price:
                reasons.append("손절 상향")
            elif next_stop_loss_price < previous_stop_loss_price:
                reasons.append("손절 하향")
            else:
                reasons.append("손절라인 유지")
        else:
            if next_take_profit_price < previous_take_profit_price:
                reasons.append("익절 하향")
            elif next_take_profit_price > previous_take_profit_price:
                reasons.append("익절 상향")
            else:
                reasons.append("익절라인 유지")

            if next_stop_loss_price < previous_stop_loss_price:
                reasons.append("손절 하향")
            elif next_stop_loss_price > previous_stop_loss_price:
                reasons.append("손절 상향")
            else:
                reasons.append("손절라인 유지")

        if snapshot.ema_fast_1m > snapshot.ema_slow_1m and snapshot.mark_price >= snapshot.ema_fast_1m:
            reasons.append("1분 상승 유지")
        elif snapshot.ema_fast_1m <= snapshot.ema_slow_1m:
            reasons.append("1분 둔화")
        else:
            reasons.append("1분 혼조")

        if self._is_sideways_regime(snapshot):
            reasons.append("횡보 확정 상태")
        return reasons

    def _has_material_exit_line_change(
        self,
        snapshot: MarketSnapshot,
        position: Position,
        next_take_profit_price: Decimal,
        next_stop_loss_price: Decimal,
    ) -> bool:
        if not position.has_exit_lines:
            return True

        minimum_change = max(
            position.entry_price_decimal * self.settings.exit_line_min_change_pct,
            snapshot.atr_3m * self.settings.exit_line_min_change_atr_ratio,
        )
        take_profit_delta = (next_take_profit_price - position.take_profit_price_decimal).copy_abs()
        stop_loss_delta = (next_stop_loss_price - position.stop_loss_price_decimal).copy_abs()
        return take_profit_delta >= minimum_change or stop_loss_delta >= minimum_change

    async def sync_positions_from_exchange(self) -> None:
        if self.settings.dry_run or not self.settings.api_key or not self.settings.api_secret:
            return
        positions = await self.service.get_position_risks(self.settings.symbols)
        active_symbols = {position.symbol for position in positions}
        for stale_symbol in [symbol for symbol in self.state_store.positions if symbol not in active_symbols]:
            self.state_store.remove(stale_symbol)
        for position in positions:
            existing = self.state_store.get(position.symbol)
            if existing is not None:
                normalized_entry_count = max(1, min(existing.entry_count, self.settings.entry_splits))
                normalized_exit_count = max(0, min(existing.exit_count, self.settings.exit_splits))
                if normalized_entry_count == existing.entry_count and normalized_exit_count == existing.exit_count:
                    continue
                normalized = Position(
                    symbol=existing.symbol,
                    side=existing.side,
                    quantity=existing.quantity,
                    entry_price=existing.entry_price,
                    order_id=existing.order_id,
                    opened_at=existing.opened_at,
                    leverage=existing.leverage,
                    margin_usdt=existing.margin_usdt,
                    notional_usdt=existing.notional_usdt,
                    take_profit_price=existing.take_profit_price,
                    stop_loss_price=existing.stop_loss_price,
                    take_profit_pct=existing.take_profit_pct,
                    stop_loss_pct=existing.stop_loss_pct,
                    realized_pnl_usdt=existing.realized_pnl_usdt,
                    commission_usdt=existing.commission_usdt,
                    last_exit_update_at=existing.last_exit_update_at,
                    last_trade_sync_at=existing.last_trade_sync_at,
                    trend_follow_armed=existing.trend_follow_armed,
                    entry_count=normalized_entry_count,
                    exit_count=normalized_exit_count,
                )
                self.state_store.set(normalized)
                self.logger.info(
                    "[BINANCE POSITION NORMALIZED] symbol=%s entry_split=%s->%s exit_split=%s->%s",
                    existing.symbol,
                    existing.entry_count,
                    normalized_entry_count,
                    existing.exit_count,
                    normalized_exit_count,
                )
                continue
            synced = Position(
                symbol=position.symbol,
                side=position.side,
                quantity=str(abs(position.quantity)),
                entry_price=str(position.entry_price),
                order_id=f"sync-{position.symbol}-{int(datetime.now(tz=timezone.utc).timestamp())}",
                opened_at=datetime.now(tz=timezone.utc).isoformat(),
                leverage=position.leverage,
                margin_usdt=str((abs(position.quantity) * position.entry_price) / Decimal(position.leverage)),
                notional_usdt=str(abs(position.quantity) * position.entry_price),
                realized_pnl_usdt="0",
                commission_usdt="0",
                entry_count=1,
                exit_count=0,
            )
            self.state_store.set(synced)

    async def run_cycle(self) -> None:
        self.heartbeat.touch("running")
        self.logger.info(
            "Binance 선물 사이클 시작 symbols=%s dry_run=%s leverage=%s open_positions=%s",
            ",".join(self.settings.symbols),
            self.settings.dry_run,
            self.settings.leverage,
            len(self.state_store.positions),
        )
        await self.sync_positions_from_exchange()
        account_snapshot = await self._get_account_snapshot()
        self.logger.info(
            "Binance 계좌 스냅샷 available_balance=%s wallet_balance=%s max_withdraw=%s",
            self._format_decimal(account_snapshot.available_balance, "0.00"),
            self._format_decimal(account_snapshot.wallet_balance, "0.00"),
            self._format_decimal(account_snapshot.max_withdraw_amount, "0.00"),
        )

        for symbol in self.settings.symbols:
            snapshot = await self._build_snapshot(symbol)
            await self._maybe_alert_sideways_regime(snapshot)
            position = self.state_store.get(symbol)
            if position is None:
                await self._maybe_enter(snapshot, account_snapshot)
            else:
                position = await self._maybe_scale_in(snapshot, account_snapshot, position)
                position = await self._sync_position_trade_metrics(position)
                position = await self._ensure_exit_lines(snapshot, position)
                await self._maybe_exit(snapshot, position)

        self.logger.info("Binance 선물 사이클 종료 open_positions=%s", len(self.state_store.positions))
        self.heartbeat.touch("cycle-complete")

    async def run_exit_monitor_cycle(self) -> None:
        open_symbols = list(self.state_store.positions.keys())
        if not open_symbols:
            return

        self.heartbeat.touch("exit-monitor")
        self.logger.info(
            "Binance 청산 감시 시작 symbols=%s open_positions=%s",
            ",".join(open_symbols),
            len(open_symbols),
        )
        await self.sync_positions_from_exchange()

        for symbol in list(self.state_store.positions.keys()):
            position = self.state_store.get(symbol)
            if position is None:
                continue
            snapshot = await self._build_snapshot(symbol)
            position = await self._sync_position_trade_metrics(position)
            await self._maybe_exit(snapshot, position)

        self.logger.info("Binance 청산 감시 종료 open_positions=%s", len(self.state_store.positions))
        self.heartbeat.touch("exit-monitor-complete")

    async def _get_account_snapshot(self) -> AccountSnapshot:
        if not self.settings.api_key or not self.settings.api_secret:
            simulated_balance = self.settings.margin_per_trade_usdt * Decimal(self.settings.max_open_positions)
            return AccountSnapshot(available_balance=simulated_balance, wallet_balance=simulated_balance)
        return await self.service.get_account_snapshot()

    async def _build_snapshot(self, symbol: str) -> MarketSnapshot:
        klines_1m = await self.service.get_klines(symbol, self.settings.entry_interval, self.settings.candle_count)
        klines_3m = await self.service.get_klines(symbol, "3m", self.settings.candle_count)
        klines_5m = await self.service.get_klines(symbol, self.settings.trend_interval, self.settings.candle_count)
        klines_15m = await self.service.get_klines(symbol, self.settings.context_interval, 80)
        ask_price, bid_price = await self.service.get_book_ticker(symbol)
        mark_price = await self.service.get_mark_price(symbol)

        closes_1m = [row["close"] for row in klines_1m]
        highs_1m = [row["high"] for row in klines_1m]
        lows_1m = [row["low"] for row in klines_1m]
        volumes_1m = [row["volume"] for row in klines_1m]
        closes_3m = [row["close"] for row in klines_3m]
        closes_5m = [row["close"] for row in klines_5m]
        closes_15m = [row["close"] for row in klines_15m]

        ema9_1m = ema(closes_1m, 9)
        ema21_1m = ema(closes_1m, 21)
        ema9_5m = ema(closes_5m, 9)
        ema21_5m = ema(closes_5m, 21)
        ema50_5m = ema(closes_5m, 50)
        ema21_15m = ema(closes_15m, 21)
        ema50_15m = ema(closes_15m, 50)
        rsi_1m = rsi(closes_1m, 14)
        rsi_3m = rsi(closes_3m, 14)
        atr_3m = atr(
            [row["high"] for row in klines_3m],
            [row["low"] for row in klines_3m],
            closes_3m,
            14,
        )
        atr_5m = atr(
            [row["high"] for row in klines_5m],
            [row["low"] for row in klines_5m],
            closes_5m,
            14,
        )
        recent_high = max(highs_1m[-15:])
        recent_low = min(lows_1m[-15:])
        breakout_high = max(highs_1m[-31:-1])
        breakout_low = min(lows_1m[-31:-1])
        average_volume = sma(volumes_1m[-31:-1], 20)
        volume_ratio = Decimal("0") if average_volume <= 0 else volumes_1m[-1] / average_volume

        trend_long_ok = ema9_5m > ema21_5m > ema50_5m and closes_15m[-1] > ema21_15m > ema50_15m
        trend_short_ok = ema9_5m < ema21_5m < ema50_5m and closes_15m[-1] < ema21_15m < ema50_15m

        long_score = 0
        short_score = 0
        long_reasons: list[str] = []
        short_reasons: list[str] = []

        if trend_long_ok:
            long_score += 2
            long_reasons.append("5분/15분하모니상승+2")
        if trend_short_ok:
            short_score += 2
            short_reasons.append("5분/15분하모니하락+2")
        if mark_price >= breakout_high:
            long_score += 2
            long_reasons.append("30봉상단돌파+2")
        if mark_price <= breakout_low:
            short_score += 2
            short_reasons.append("30봉하단이탈+2")
        if ema9_1m > ema21_1m and closes_1m[-1] > ema9_1m:
            long_score += 1
            long_reasons.append("1분추세복원+1")
        if ema9_1m < ema21_1m and closes_1m[-1] < ema9_1m:
            short_score += 1
            short_reasons.append("1분추세약세+1")
        if Decimal("50") <= rsi_1m <= Decimal("67"):
            long_score += 1
            long_reasons.append("RSI롱영역+1")
        if Decimal("33") <= rsi_1m <= Decimal("50"):
            short_score += 1
            short_reasons.append("RSI숏영역+1")
        if volume_ratio >= Decimal("1.15"):
            long_score += 1
            short_score += 1
            long_reasons.append("거래량확대+1")
            short_reasons.append("거래량확대+1")
        if closes_1m[-1] > closes_1m[-2]:
            long_score += 1
            long_reasons.append("직전봉상승+1")
        if closes_1m[-1] < closes_1m[-2]:
            short_score += 1
            short_reasons.append("직전봉하락+1")

        self.logger.info(
            "Binance 시장상태 symbol=%s mark_price=%s long_score=%s short_score=%s rsi_1m=%s rsi_3m=%s volume_ratio=%s trend_long_ok=%s trend_short_ok=%s breakout_high=%s breakout_low=%s",
            symbol,
            self._format_decimal(mark_price, "0.0000"),
            long_score,
            short_score,
            self._format_decimal(rsi_1m, "0.00"),
            self._format_decimal(rsi_3m, "0.00"),
            self._format_decimal(volume_ratio, "0.00"),
            trend_long_ok,
            trend_short_ok,
            self._format_decimal(breakout_high, "0.0000"),
            self._format_decimal(breakout_low, "0.0000"),
        )

        return MarketSnapshot(
            symbol=symbol,
            mark_price=mark_price,
            ask_price=ask_price,
            bid_price=bid_price,
            ema_fast_1m=ema9_1m,
            ema_slow_1m=ema21_1m,
            long_score=long_score,
            short_score=short_score,
            long_reasons=long_reasons,
            short_reasons=short_reasons,
            volume_ratio=volume_ratio,
            rsi_1m=rsi_1m,
            rsi_3m=rsi_3m,
            atr_3m=atr_3m,
            atr_15m=atr_5m,
            recent_high=recent_high,
            recent_low=recent_low,
            breakout_high=breakout_high,
            breakout_low=breakout_low,
            trend_long_ok=trend_long_ok,
            trend_short_ok=trend_short_ok,
        )

    def _calculate_exit_lines(self, snapshot: MarketSnapshot, side: str, entry_price: Decimal, leverage: int, position: Position | None = None) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        atr_fast = snapshot.atr_3m
        atr_slow = snapshot.atr_15m
        atr_base = (atr_fast * Decimal("0.7")) + (atr_slow * Decimal("0.3"))
        volatility_ratio = atr_fast / entry_price if entry_price > 0 else Decimal("0")
        realized_margin_pct, paid_fee_margin_pct, remaining_exit_fee_margin_pct = self._position_trade_margin_terms(position, leverage)
        if self._is_sideways_regime(snapshot) and position is None:
            take_profit_move = self._take_profit_move_with_trade_costs(self.settings.sideways_take_profit_on_margin_pct, leverage, position)
            stop_loss_move = self._stop_loss_move_with_trade_costs(self.settings.sideways_stop_loss_on_margin_pct, leverage, position)
            if side == "LONG":
                take_profit_price = entry_price * (Decimal("1") + take_profit_move)
                stop_loss_price = max(
                    entry_price * (Decimal("1") - stop_loss_move),
                    snapshot.recent_low - (atr_fast * Decimal("0.05")),
                )
                take_profit_pct = self._cycle_net_margin_pct(position, ((take_profit_price / entry_price) - Decimal("1")) * Decimal(leverage), leverage)
                stop_loss_pct = self._cycle_net_margin_pct(position, ((stop_loss_price / entry_price) - Decimal("1")) * Decimal(leverage), leverage)
            else:
                take_profit_price = entry_price * (Decimal("1") - take_profit_move)
                stop_loss_price = min(
                    entry_price * (Decimal("1") + stop_loss_move),
                    snapshot.recent_high + (atr_fast * Decimal("0.05")),
                )
                take_profit_pct = self._cycle_net_margin_pct(position, ((Decimal("1") - (take_profit_price / entry_price)) * Decimal(leverage)), leverage)
                stop_loss_pct = self._cycle_net_margin_pct(position, -(((stop_loss_price / entry_price) - Decimal("1")) * Decimal(leverage)), leverage)

            sideways_target_take_profit_pct = max(
                self.settings.sideways_take_profit_on_margin_pct,
                abs(stop_loss_pct) * self.settings.exit_reward_risk_ratio,
            )
            take_profit_price, take_profit_pct = self._take_profit_price_from_target_pct(
                side,
                entry_price,
                leverage,
                position,
                sideways_target_take_profit_pct,
            )

            target_net_usdt = self._margin_pct_to_usdt(position.margin_usdt_decimal, take_profit_pct) if position is not None else Decimal("0")
            stop_net_usdt = self._margin_pct_to_usdt(position.margin_usdt_decimal, stop_loss_pct) if position is not None else Decimal("0")

            self.logger.info(
                "[BINANCE EXIT MODEL] symbol=%s side=%s mode=SIDEWAYS_SCALP atr_fast=%s atr_slow=%s volume_ratio=%s target_net_pct=%s target_net_usdt=%s stop_net_pct=%s stop_net_usdt=%s realized_pct=%s paid_fee_pct=%s",
                snapshot.symbol,
                side,
                self._format_decimal(atr_fast),
                self._format_decimal(atr_slow),
                self._format_decimal(snapshot.volume_ratio),
                self._format_decimal(take_profit_pct * Decimal('100'), '0.00'),
                self._format_decimal(target_net_usdt, '0.0000'),
                self._format_decimal(stop_loss_pct * Decimal('100'), '0.00'),
                self._format_decimal(stop_net_usdt, '0.0000'),
                self._format_decimal(realized_margin_pct * Decimal('100'), '0.00'),
                self._format_decimal(paid_fee_margin_pct * Decimal('100'), '0.00'),
            )
            return (
                take_profit_price.quantize(Decimal("0.0001")),
                stop_loss_price.quantize(Decimal("0.0001")),
                take_profit_pct.quantize(Decimal("0.0001")),
                stop_loss_pct.quantize(Decimal("0.0001")),
            )

        if side == "SHORT":
            if snapshot.volume_ratio >= Decimal("1.8") or volatility_ratio >= Decimal("0.0035"):
                stop_atr_multiplier = Decimal("0.62")
                take_profit_atr_multiplier = Decimal("0.78")
            elif snapshot.volume_ratio >= Decimal("1.2") or volatility_ratio >= Decimal("0.0023"):
                stop_atr_multiplier = Decimal("0.54")
                take_profit_atr_multiplier = Decimal("0.68")
            else:
                stop_atr_multiplier = Decimal("0.48")
                take_profit_atr_multiplier = Decimal("0.58")
            minimum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.short_min_take_profit_on_margin_pct, leverage, position)
            maximum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.short_max_take_profit_on_margin_pct, leverage, position)
            minimum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.short_min_stop_loss_on_margin_pct, leverage, position)
            maximum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.short_max_stop_loss_on_margin_pct, leverage, position)
        elif snapshot.volume_ratio >= Decimal("1.8") or volatility_ratio >= Decimal("0.0035"):
            stop_atr_multiplier = Decimal("0.85")
            take_profit_atr_multiplier = Decimal("1.25")
            minimum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.min_take_profit_on_margin_pct, leverage, position)
            maximum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.max_take_profit_on_margin_pct, leverage, position)
            minimum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.min_stop_loss_on_margin_pct, leverage, position)
            maximum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.max_stop_loss_on_margin_pct, leverage, position)
        elif snapshot.volume_ratio >= Decimal("1.2") or volatility_ratio >= Decimal("0.0023"):
            stop_atr_multiplier = Decimal("0.72")
            take_profit_atr_multiplier = Decimal("1.05")
            minimum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.min_take_profit_on_margin_pct, leverage, position)
            maximum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.max_take_profit_on_margin_pct, leverage, position)
            minimum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.min_stop_loss_on_margin_pct, leverage, position)
            maximum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.max_stop_loss_on_margin_pct, leverage, position)
        else:
            stop_atr_multiplier = Decimal("0.58")
            take_profit_atr_multiplier = Decimal("0.88")
            minimum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.min_take_profit_on_margin_pct, leverage, position)
            maximum_take_profit_pct = self._take_profit_move_with_trade_costs(self.settings.max_take_profit_on_margin_pct, leverage, position)
            minimum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.min_stop_loss_on_margin_pct, leverage, position)
            maximum_stop_loss_pct = self._stop_loss_move_with_trade_costs(self.settings.max_stop_loss_on_margin_pct, leverage, position)

        if side == "LONG":
            fixed_take_profit_price = entry_price * (
                Decimal("1") + minimum_take_profit_pct
            )
            fixed_stop_loss_price = entry_price * (
                Decimal("1") - minimum_stop_loss_pct
            )
            deepest_stop_price = entry_price * (
                Decimal("1") - maximum_stop_loss_pct
            )
            structural_stop_price = min(
                snapshot.recent_low - (atr_fast * Decimal("0.08")),
                entry_price - (atr_base * stop_atr_multiplier),
            )
            stop_loss_price = min(fixed_stop_loss_price, structural_stop_price)
            stop_loss_price = max(stop_loss_price, deepest_stop_price)
            structural_take_profit = min(
                max(snapshot.recent_high, entry_price) + (atr_fast * Decimal("0.12")),
                entry_price + (atr_base * take_profit_atr_multiplier),
            )
            take_profit_cap = entry_price * (
                Decimal("1") + maximum_take_profit_pct
            )
            take_profit_price = max(fixed_take_profit_price, structural_take_profit)
            take_profit_price = min(take_profit_price, take_profit_cap)
            stop_loss_pct = self._cycle_net_margin_pct(position, -((Decimal("1") - (stop_loss_price / entry_price)) * Decimal(leverage)), leverage)
            target_take_profit_pct = self._reward_risk_take_profit_target_pct(
                stop_loss_pct,
                self.settings.min_take_profit_on_margin_pct,
                self.settings.max_take_profit_on_margin_pct,
            )
            take_profit_price, take_profit_pct = self._take_profit_price_from_target_pct(
                side,
                entry_price,
                leverage,
                position,
                target_take_profit_pct,
            )
        else:
            fixed_take_profit_price = entry_price * (
                Decimal("1") - minimum_take_profit_pct
            )
            fixed_stop_loss_price = entry_price * (
                Decimal("1") + minimum_stop_loss_pct
            )
            highest_stop_price = entry_price * (
                Decimal("1") + maximum_stop_loss_pct
            )
            structural_stop_price = max(
                snapshot.recent_high + (atr_fast * Decimal("0.08")),
                entry_price + (atr_base * stop_atr_multiplier),
            )
            stop_loss_price = max(fixed_stop_loss_price, structural_stop_price)
            stop_loss_price = min(stop_loss_price, highest_stop_price)
            structural_take_profit = max(
                min(snapshot.recent_low, entry_price) - (atr_fast * Decimal("0.12")),
                entry_price - (atr_base * take_profit_atr_multiplier),
            )
            take_profit_floor = entry_price * (
                Decimal("1") - maximum_take_profit_pct
            )
            take_profit_price = min(fixed_take_profit_price, structural_take_profit)
            take_profit_price = max(take_profit_price, take_profit_floor)
            stop_loss_pct = self._cycle_net_margin_pct(position, -(((stop_loss_price / entry_price) - Decimal("1")) * Decimal(leverage)), leverage)
            target_take_profit_pct = self._reward_risk_take_profit_target_pct(
                stop_loss_pct,
                self.settings.short_min_take_profit_on_margin_pct,
                self.settings.short_max_take_profit_on_margin_pct,
            )
            take_profit_price, take_profit_pct = self._take_profit_price_from_target_pct(
                side,
                entry_price,
                leverage,
                position,
                target_take_profit_pct,
            )

        target_net_usdt = self._margin_pct_to_usdt(position.margin_usdt_decimal, take_profit_pct) if position is not None else Decimal("0")
        stop_net_usdt = self._margin_pct_to_usdt(position.margin_usdt_decimal, stop_loss_pct) if position is not None else Decimal("0")

        self.logger.info(
            "[BINANCE EXIT MODEL] symbol=%s side=%s atr_fast=%s atr_slow=%s volume_ratio=%s stop_atr_mult=%s tp_atr_mult=%s rr_ratio=%s target_net_pct=%s target_net_usdt=%s stop_net_pct=%s stop_net_usdt=%s realized_pct=%s paid_fee_pct=%s remaining_fee_pct=%s",
            snapshot.symbol,
            side,
            self._format_decimal(atr_fast),
            self._format_decimal(atr_slow),
            self._format_decimal(snapshot.volume_ratio),
            self._format_decimal(stop_atr_multiplier),
            self._format_decimal(take_profit_atr_multiplier),
            self._format_decimal(self.settings.exit_reward_risk_ratio, '0.00'),
            self._format_decimal(take_profit_pct * Decimal('100'), '0.00'),
            self._format_decimal(target_net_usdt, '0.0000'),
            self._format_decimal(stop_loss_pct * Decimal('100'), '0.00'),
            self._format_decimal(stop_net_usdt, '0.0000'),
            self._format_decimal(realized_margin_pct * Decimal('100'), '0.00'),
            self._format_decimal(paid_fee_margin_pct * Decimal('100'), '0.00'),
            self._format_decimal(remaining_exit_fee_margin_pct * Decimal('100'), '0.00'),
        )

        return (
            take_profit_price.quantize(Decimal("0.0001")),
            stop_loss_price.quantize(Decimal("0.0001")),
            take_profit_pct.quantize(Decimal("0.0001")),
            stop_loss_pct.quantize(Decimal("0.0001")),
        )

    async def _ensure_exit_lines(self, snapshot: MarketSnapshot, position: Position) -> Position:
        calculated_take_profit_price, calculated_stop_loss_price, _, _ = self._calculate_exit_lines(
            snapshot,
            position.side,
            position.entry_price_decimal,
            position.leverage,
            position,
        )
        refresh_due = self._is_exit_line_refresh_due(position.last_exit_update_at)
        sideways_transition_due = False
        if position.has_exit_lines and not refresh_due and not sideways_transition_due:
            return position

        current_net_margin_pct = self._current_net_margin_pct(snapshot, position)
        refreshed_at = self._now_utc().isoformat()
        if position.has_exit_lines and not sideways_transition_due and current_net_margin_pct <= 0:
            frozen = Position(
                symbol=position.symbol,
                side=position.side,
                quantity=position.quantity,
                entry_price=position.entry_price,
                order_id=position.order_id,
                opened_at=position.opened_at,
                leverage=position.leverage,
                margin_usdt=position.margin_usdt,
                notional_usdt=position.notional_usdt,
                take_profit_price=position.take_profit_price,
                stop_loss_price=position.stop_loss_price,
                take_profit_pct=position.take_profit_pct,
                stop_loss_pct=position.stop_loss_pct,
                realized_pnl_usdt=position.realized_pnl_usdt,
                commission_usdt=position.commission_usdt,
                last_exit_update_at=refreshed_at,
                last_trade_sync_at=position.last_trade_sync_at,
                trend_follow_armed=position.trend_follow_armed,
                entry_count=position.entry_count,
                exit_count=position.exit_count,
            )
            self.state_store.set(frozen)
            self.logger.info(
                "[BINANCE EXIT FREEZE] symbol=%s side=%s net_pnl_pct=%s reason=underwater_full_freeze",
                position.symbol,
                position.side,
                self._format_decimal(current_net_margin_pct * Decimal('100'), '0.00'),
            )
            return frozen

        if (
            position.has_exit_lines
            and not sideways_transition_due
            and not self._has_material_exit_line_change(
                snapshot,
                position,
                calculated_take_profit_price,
                calculated_stop_loss_price,
            )
        ):
            return position

        take_profit_price = calculated_take_profit_price
        stop_loss_price = calculated_stop_loss_price
        previous_take_profit_price = position.take_profit_price_decimal
        previous_stop_loss_price = position.stop_loss_price_decimal
        previous_take_profit_pct = position.take_profit_pct_decimal
        previous_stop_loss_pct = position.stop_loss_pct_decimal

        if (
            position.has_exit_lines
            and take_profit_price == position.take_profit_price_decimal
            and stop_loss_price == position.stop_loss_price_decimal
        ):
            updated = Position(
                symbol=position.symbol,
                side=position.side,
                quantity=position.quantity,
                entry_price=position.entry_price,
                order_id=position.order_id,
                opened_at=position.opened_at,
                leverage=position.leverage,
                margin_usdt=position.margin_usdt,
                notional_usdt=position.notional_usdt,
                take_profit_price=position.take_profit_price,
                stop_loss_price=position.stop_loss_price,
                take_profit_pct=position.take_profit_pct,
                stop_loss_pct=position.stop_loss_pct,
                realized_pnl_usdt=position.realized_pnl_usdt,
                commission_usdt=position.commission_usdt,
                last_exit_update_at=refreshed_at,
                last_trade_sync_at=position.last_trade_sync_at,
                trend_follow_armed=position.trend_follow_armed,
                entry_count=position.entry_count,
                exit_count=position.exit_count,
            )
            self.state_store.set(updated)
            return updated

        if position.side == "LONG":
            take_profit_pct = self._cycle_net_margin_pct(position, ((take_profit_price / position.entry_price_decimal) - Decimal("1")) * Decimal(position.leverage), position.leverage)
            stop_loss_pct = self._cycle_net_margin_pct(position, -((Decimal("1") - (stop_loss_price / position.entry_price_decimal)) * Decimal(position.leverage)), position.leverage)
        else:
            take_profit_pct = self._cycle_net_margin_pct(position, ((Decimal("1") - (take_profit_price / position.entry_price_decimal)) * Decimal(position.leverage)), position.leverage)
            stop_loss_pct = self._cycle_net_margin_pct(position, -(((stop_loss_price / position.entry_price_decimal) - Decimal("1")) * Decimal(position.leverage)), position.leverage)

        take_profit_pct = take_profit_pct.quantize(Decimal("0.0001"))
        stop_loss_pct = stop_loss_pct.quantize(Decimal("0.0001"))
        updated = Position(
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            order_id=position.order_id,
            opened_at=position.opened_at,
            leverage=position.leverage,
            margin_usdt=position.margin_usdt,
            notional_usdt=position.notional_usdt,
            take_profit_price=str(take_profit_price),
            stop_loss_price=str(stop_loss_price),
            take_profit_pct=str(take_profit_pct),
            stop_loss_pct=str(stop_loss_pct),
            realized_pnl_usdt=position.realized_pnl_usdt,
            commission_usdt=position.commission_usdt,
            last_exit_update_at=refreshed_at,
            last_trade_sync_at=position.last_trade_sync_at,
            trend_follow_armed=position.trend_follow_armed,
            entry_count=position.entry_count,
            exit_count=position.exit_count,
        )
        self.state_store.set(updated)
        line_update_reasons = self._build_exit_line_update_reasons(
            snapshot,
            position,
            previous_take_profit_price,
            take_profit_price,
            previous_stop_loss_price,
            stop_loss_price,
        )
        alert_title = "[Binance 횡보 라인 전환]" if sideways_transition_due else "[Binance 라인 재설정]"
        if sideways_transition_due:
            line_update_reasons = [
                "횡보장 감지 즉시 전환",
                f"박스상단={self._format_decimal(snapshot.recent_high, '0.0000')}",
                f"박스하단={self._format_decimal(snapshot.recent_low, '0.0000')}",
                *line_update_reasons,
            ]
        await self.notifier.send(
            format_telegram_message(
                alert_title,
                fields=[
                    ("심볼", position.symbol),
                    ("방향", position.side),
                    ("평단", f"{position.entry_price_decimal:.4f}"),
                    (
                        "익절 라인",
                        f"{previous_take_profit_price:.4f} -> {take_profit_price:.4f} ({self._format_line_delta(previous_take_profit_price, take_profit_price)})" if position.has_exit_lines else f"{take_profit_price:.4f}",
                    ),
                    (
                        "순익 목표",
                        f"{previous_take_profit_pct * Decimal('100'):.2f}% -> {take_profit_pct * Decimal('100'):.2f}% ({self._format_line_delta(previous_take_profit_pct * Decimal('100'), take_profit_pct * Decimal('100'), '0.01')})" if position.has_exit_lines else f"{take_profit_pct * Decimal('100'):.2f}%",
                    ),
                    (
                        "목표 순익 USDT",
                        f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, previous_take_profit_pct):.4f} -> {self._margin_pct_to_usdt(position.margin_usdt_decimal, take_profit_pct):.4f}" if position.has_exit_lines else f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, take_profit_pct):.4f}",
                    ),
                    (
                        "손절 라인",
                        f"{previous_stop_loss_price:.4f} -> {stop_loss_price:.4f} ({self._format_line_delta(previous_stop_loss_price, stop_loss_price)})" if position.has_exit_lines else f"{stop_loss_price:.4f}",
                    ),
                    (
                        "허용 손실",
                        f"{previous_stop_loss_pct * Decimal('100'):.2f}% -> {stop_loss_pct * Decimal('100'):.2f}% ({self._format_line_delta(previous_stop_loss_pct * Decimal('100'), stop_loss_pct * Decimal('100'), '0.01')})" if position.has_exit_lines else f"{stop_loss_pct * Decimal('100'):.2f}%",
                    ),
                    (
                        "허용 손실 USDT",
                        f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, previous_stop_loss_pct):.4f} -> {self._margin_pct_to_usdt(position.margin_usdt_decimal, stop_loss_pct):.4f}" if position.has_exit_lines else f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, stop_loss_pct):.4f}",
                    ),
                    ("라인 갱신", f"{self.settings.exit_line_refresh_seconds}초"),
                ],
                sections=[("변동 이유", line_update_reasons)],
            )
        )
        self.logger.info(
            "[BINANCE EXIT UPDATE] symbol=%s side=%s tp=%s sl=%s had_existing=%s",
            position.symbol,
            position.side,
            self._format_decimal(take_profit_price),
            self._format_decimal(stop_loss_price),
            position.has_exit_lines,
        )
        return updated

    async def _maybe_scale_in(self, snapshot: MarketSnapshot, account_snapshot: AccountSnapshot, position: Position) -> Position:
        if position.entry_count >= self.settings.entry_splits:
            return position

        if self._is_sideways_regime(snapshot):
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=횡보분할진입차단 side=%s entry_split=%s/%s volume_ratio=%s",
                snapshot.symbol,
                position.side,
                position.entry_count,
                self.settings.entry_splits,
                self._format_decimal(snapshot.volume_ratio, "0.00"),
            )
            return position

        preferred_side = snapshot.preferred_side
        if preferred_side != position.side:
            return position

        preferred_score = snapshot.long_score if preferred_side == "LONG" else snapshot.short_score
        preferred_reasons = snapshot.long_reasons if preferred_side == "LONG" else snapshot.short_reasons
        score_threshold = self._entry_score_threshold_for_side(preferred_side)
        if preferred_score < score_threshold:
            return position

        if not self._has_entry_momentum(snapshot, preferred_side):
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=횡보진입차단 side=%s score=%s/%s volume_ratio=%s rsi_3m=%s breakout_high=%s breakout_low=%s",
                snapshot.symbol,
                preferred_side,
                preferred_score,
                score_threshold,
                self._format_decimal(snapshot.volume_ratio, "0.00"),
                self._format_decimal(snapshot.rsi_3m, "0.00"),
                self._format_decimal(snapshot.breakout_high, "0.0000"),
                self._format_decimal(snapshot.breakout_low, "0.0000"),
            )
            return position

        if not self._can_scale_in_position(snapshot, position):
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=분할진입차단 side=%s mark_price=%s avg_entry=%s long_score=%s short_score=%s",
                snapshot.symbol,
                preferred_side,
                self._format_decimal(snapshot.mark_price, "0.0000"),
                self._format_decimal(position.entry_price_decimal, "0.0000"),
                snapshot.long_score,
                snapshot.short_score,
            )
            return position

        await self._ensure_entry_leverage(snapshot.symbol, position.leverage)
        entry_price = snapshot.mark_price
        additional_margin, quantity, effective_remaining_splits = await self._resolve_entry_order_plan(
            snapshot.symbol,
            entry_price,
            account_snapshot.available_balance,
            position.leverage,
            position,
        )
        effective_total_splits = position.entry_count + effective_remaining_splits
        if quantity <= 0:
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=분할진입수량정규화후0 split=%s/%s requested_margin=%s",
                snapshot.symbol,
                position.entry_count + 1,
                effective_total_splits,
                self._format_decimal(additional_margin, "0.00"),
            )
            return position

        execution = await self.service.create_market_order(
            symbol=snapshot.symbol,
            side="BUY" if preferred_side == "LONG" else "SELL",
            quantity=quantity,
            reduce_only=False,
            dry_run=self.settings.dry_run,
        )
        if execution is None or execution.executed_qty <= 0:
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=분할진입미체결 split=%s/%s requested_margin=%s requested_qty=%s",
                snapshot.symbol,
                position.entry_count + 1,
                effective_total_splits,
                self._format_decimal(additional_margin, "0.00"),
                self._format_decimal(quantity, "0.000"),
            )
            return position

        total_quantity = position.quantity_decimal + execution.executed_qty
        total_notional = position.notional_usdt_decimal + (execution.executed_qty * execution.avg_price)
        average_entry = total_notional / total_quantity
        total_margin = position.margin_usdt_decimal + additional_margin
        take_profit_price, stop_loss_price, take_profit_pct, stop_loss_pct = self._calculate_exit_lines(
            snapshot,
            preferred_side,
            average_entry,
            position.leverage,
        )
        updated = self._build_position(
            symbol=position.symbol,
            side=position.side,
            quantity=total_quantity,
            entry_price=average_entry,
            order_id=execution.order_id,
            opened_at=position.opened_at,
            leverage=position.leverage,
            margin_usdt=total_margin,
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
        self.logger.info(
            "[BINANCE SCALE-IN] symbol=%s side=%s split=%s/%s requested_margin=%s executed_qty=%s avg_entry=%s total_qty=%s",
            snapshot.symbol,
            preferred_side,
            updated.entry_count,
            effective_total_splits,
            self._format_decimal(additional_margin, "0.00"),
            self._format_decimal(execution.executed_qty, "0.000"),
            self._format_decimal(average_entry, "0.0000"),
            self._format_decimal(total_quantity, "0.000"),
        )
        await self.notifier.send(
            format_telegram_message(
                "[Binance 분할진입 체결]",
                fields=[
                    ("심볼", snapshot.symbol),
                    ("방향", preferred_side),
                    ("분할", f"{updated.entry_count}/{effective_total_splits}"),
                    ("추가 증거금", f"{additional_margin:.2f} USDT"),
                    ("추가 수량", execution.executed_qty),
                    ("평단", f"{average_entry:.4f}"),
                    ("총 수량", total_quantity),
                    ("dry_run", self.settings.dry_run),
                ],
                sections=[("진입 근거", preferred_reasons or ["none"])],
            )
        )
        return updated

    async def _maybe_enter(self, snapshot: MarketSnapshot, account_snapshot: AccountSnapshot) -> None:
        if len(self.state_store.positions) >= self.settings.max_open_positions:
            self.logger.info("[BINANCE HOLD] symbol=%s reasons=최대포지션도달=%s", snapshot.symbol, self.settings.max_open_positions)
            return

        crash_short_signal = self._get_crash_short_signal(snapshot)
        rebound_long_signal = None if crash_short_signal is not None else self._get_rebound_long_signal(snapshot)
        if crash_short_signal is not None:
            self.logger.info(
                "[BINANCE SPECIAL SIGNAL] symbol=%s type=CRASH_SHORT reasons=%s",
                snapshot.symbol,
                "/".join(crash_short_signal[1]),
            )
        elif rebound_long_signal is not None:
            self.logger.info(
                "[BINANCE SPECIAL SIGNAL] symbol=%s type=REBOUND_LONG reasons=%s",
                snapshot.symbol,
                "/".join(rebound_long_signal[1]),
            )
        preferred_side = snapshot.preferred_side
        preferred_reasons: list[str] = []
        score_display = "range"
        if crash_short_signal is not None:
            preferred_side, preferred_reasons = crash_short_signal
            score_display = "crash"
        elif rebound_long_signal is not None:
            preferred_side, preferred_reasons = rebound_long_signal
            score_display = "rebound"
        elif preferred_side is None:
            sideways_signal = self._get_sideways_entry_signal(snapshot)
            if sideways_signal is None:
                self.logger.info("[BINANCE HOLD] symbol=%s reasons=매수신호없음", snapshot.symbol)
                return
            preferred_side, preferred_reasons = sideways_signal
        else:
            preferred_score = snapshot.long_score if preferred_side == "LONG" else snapshot.short_score
            preferred_reasons = snapshot.long_reasons if preferred_side == "LONG" else snapshot.short_reasons
            score_threshold = self._entry_score_threshold_for_side(preferred_side)
            score_display = f"{preferred_score}/{score_threshold}"
            if self._is_exhausted_entry(snapshot, preferred_side):
                self.logger.info(
                    "[BINANCE HOLD] symbol=%s reasons=과열진입차단 side=%s rsi_3m=%s trend_long_ok=%s trend_short_ok=%s",
                    snapshot.symbol,
                    preferred_side,
                    self._format_decimal(snapshot.rsi_3m, "0.00"),
                    snapshot.trend_long_ok,
                    snapshot.trend_short_ok,
                )
                return
            if preferred_score < score_threshold:
                sideways_signal = self._get_sideways_entry_signal(snapshot)
                if sideways_signal is None:
                    self.logger.info(
                        "[BINANCE HOLD] symbol=%s reasons=점수부족 side=%s score=%s/%s detail=%s",
                        snapshot.symbol,
                        preferred_side,
                        preferred_score,
                        score_threshold,
                        "/".join(preferred_reasons) if preferred_reasons else "none",
                    )
                    return
                preferred_side, preferred_reasons = sideways_signal
                score_display = "range"
            elif not self._has_entry_momentum(snapshot, preferred_side):
                sideways_signal = self._get_sideways_entry_signal(snapshot)
                if sideways_signal is None:
                    self.logger.info(
                        "[BINANCE HOLD] symbol=%s reasons=횡보진입차단 side=%s score=%s/%s volume_ratio=%s rsi_3m=%s breakout_high=%s breakout_low=%s",
                        snapshot.symbol,
                        preferred_side,
                        preferred_score,
                        score_threshold,
                        self._format_decimal(snapshot.volume_ratio, "0.00"),
                        self._format_decimal(snapshot.rsi_3m, "0.00"),
                        self._format_decimal(snapshot.breakout_high, "0.0000"),
                        self._format_decimal(snapshot.breakout_low, "0.0000"),
                    )
                    return
                preferred_side, preferred_reasons = sideways_signal
                score_display = "range"

        entry_price = snapshot.mark_price
        await self._ensure_entry_leverage(snapshot.symbol, self.settings.leverage)
        available_margin, quantity, effective_remaining_splits = await self._resolve_entry_order_plan(
            snapshot.symbol,
            entry_price,
            account_snapshot.available_balance,
            self.settings.leverage,
            None,
        )
        effective_total_splits = effective_remaining_splits
        if quantity <= 0:
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=주문수량정규화후0 split=%s/%s requested_margin=%s",
                snapshot.symbol,
                1,
                effective_total_splits,
                self._format_decimal(available_margin, "0.00"),
            )
            return

        execution = await self.service.create_market_order(
            symbol=snapshot.symbol,
            side="BUY" if preferred_side == "LONG" else "SELL",
            quantity=quantity,
            reduce_only=False,
            dry_run=self.settings.dry_run,
        )
        if execution is None or execution.executed_qty <= 0:
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=진입미체결 split=%s/%s requested_margin=%s requested_qty=%s",
                snapshot.symbol,
                1,
                effective_total_splits,
                self._format_decimal(available_margin, "0.00"),
                self._format_decimal(quantity, "0.000"),
            )
            return

        take_profit_price, stop_loss_price, take_profit_pct, stop_loss_pct = self._calculate_exit_lines(
            snapshot,
            preferred_side,
            execution.avg_price,
            self.settings.leverage,
        )
        position = self._build_position(
            symbol=snapshot.symbol,
            side=preferred_side,
            quantity=execution.executed_qty,
            entry_price=execution.avg_price,
            order_id=execution.order_id,
            opened_at=datetime.now(tz=timezone.utc).isoformat(),
            leverage=self.settings.leverage,
            margin_usdt=available_margin,
            notional_usdt=execution.executed_qty * execution.avg_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            entry_count=1,
            exit_count=0,
            realized_pnl_usdt=Decimal("0"),
            commission_usdt=Decimal("0"),
            last_exit_update_at=self._now_utc().isoformat(),
            last_trade_sync_at="",
        )
        self.state_store.set(position)
        if preferred_side == "LONG" and score_display == "rebound":
            self._clear_recent_crash_context(snapshot.symbol)

        self.logger.info(
            "[BINANCE ENTRY] symbol=%s side=%s split=%s/%s requested_margin=%s executed_qty=%s entry_price=%s",
            snapshot.symbol,
            preferred_side,
            position.entry_count,
            effective_total_splits,
            self._format_decimal(available_margin, "0.00"),
            self._format_decimal(execution.executed_qty, "0.000"),
            self._format_decimal(execution.avg_price, "0.0000"),
        )

        await self.notifier.send(
            format_telegram_message(
                "[Binance 진입 체결]",
                fields=[
                    ("심볼", snapshot.symbol),
                    ("방향", preferred_side),
                    ("분할", f"{position.entry_count}/{effective_total_splits}"),
                    ("사용 증거금", f"{available_margin:.2f} USDT"),
                    ("점수", score_display),
                    ("레버리지", f"{self.settings.leverage}배"),
                    ("수량", execution.executed_qty),
                    ("체결가", f"{execution.avg_price:.4f}"),
                    ("익절 라인", f"{take_profit_price:.4f}"),
                    ("순익 목표", f"{take_profit_pct * Decimal('100'):.2f}%"),
                    ("손절 라인", f"{stop_loss_price:.4f}"),
                    ("허용 손실", f"{stop_loss_pct * Decimal('100'):.2f}%"),
                    ("dry_run", self.settings.dry_run),
                ],
                sections=[("진입 근거", preferred_reasons or ["none"])],
            )
        )

    async def _maybe_exit(self, snapshot: MarketSnapshot, position: Position) -> None:
        preferred_side = snapshot.preferred_side
        reverse_score = snapshot.long_score if preferred_side == "LONG" else snapshot.short_score
        reverse_reasons = snapshot.long_reasons if preferred_side == "LONG" else snapshot.short_reasons
        crash_short_signal = self._get_crash_short_signal(snapshot)
        rebound_long_signal = self._get_rebound_long_signal(snapshot)
        if position.side == "LONG" and crash_short_signal is not None:
            self.logger.info(
                "[BINANCE SPECIAL EXIT SIGNAL] symbol=%s type=CRASH_SHORT previous_side=%s reasons=%s",
                snapshot.symbol,
                position.side,
                "/".join(crash_short_signal[1]),
            )
            preferred_side = "SHORT"
            reverse_score = max(reverse_score, snapshot.short_score)
            reverse_reasons = crash_short_signal[1]
        elif position.side == "SHORT" and rebound_long_signal is not None:
            self.logger.info(
                "[BINANCE SPECIAL EXIT SIGNAL] symbol=%s type=REBOUND_LONG previous_side=%s reasons=%s",
                snapshot.symbol,
                position.side,
                "/".join(rebound_long_signal[1]),
            )
            preferred_side = "LONG"
            reverse_score = max(reverse_score, snapshot.long_score)
            reverse_reasons = rebound_long_signal[1]
        reverse_signal_hit = self._has_reverse_exit_confirmation(snapshot, position, preferred_side, reverse_score)
        current_price = snapshot.bid_price if position.side == "LONG" else snapshot.ask_price
        trend_follow_exit = False
        near_target_fade_exit = False
        if position.side == "LONG":
            gross_pnl = (current_price - position.entry_price_decimal) / position.entry_price_decimal
            gross_pnl = gross_pnl * Decimal(position.leverage)
            net_pnl = self._cycle_net_margin_pct(position, gross_pnl, position.leverage)
            take_hit = current_price >= position.take_profit_price_decimal
            stop_hit = current_price <= position.stop_loss_price_decimal
            exit_side = "SELL"

            if take_hit and self._is_strong_long_trend(snapshot) and not position.trend_follow_armed:
                position = Position(
                    symbol=position.symbol,
                    side=position.side,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    order_id=position.order_id,
                    opened_at=position.opened_at,
                    leverage=position.leverage,
                    margin_usdt=position.margin_usdt,
                    notional_usdt=position.notional_usdt,
                    take_profit_price=position.take_profit_price,
                    stop_loss_price=position.stop_loss_price,
                    take_profit_pct=position.take_profit_pct,
                    stop_loss_pct=position.stop_loss_pct,
                    realized_pnl_usdt=position.realized_pnl_usdt,
                    commission_usdt=position.commission_usdt,
                    last_exit_update_at=position.last_exit_update_at,
                    last_trade_sync_at=position.last_trade_sync_at,
                    trend_follow_armed=True,
                    entry_count=position.entry_count,
                    exit_count=position.exit_count,
                )
                self.state_store.set(position)
                self.logger.info(
                    "[BINANCE TREND FOLLOW] symbol=%s state=armed side=%s net_pnl_pct=%s long_score=%s short_score=%s",
                    snapshot.symbol,
                    position.side,
                    self._format_decimal(net_pnl * Decimal('100'), '0.00'),
                    snapshot.long_score,
                    snapshot.short_score,
                )

            if position.trend_follow_armed:
                if self._is_long_trend_bent(snapshot):
                    trend_follow_exit = True
                    take_hit = False
                else:
                    take_hit = False
            if not take_hit and not stop_hit and not trend_follow_exit:
                near_target_fade_exit = self._has_near_target_fade_exit(snapshot, position, current_price, net_pnl)
        else:
            gross_pnl = (position.entry_price_decimal - current_price) / position.entry_price_decimal
            gross_pnl = gross_pnl * Decimal(position.leverage)
            net_pnl = self._cycle_net_margin_pct(position, gross_pnl, position.leverage)
            take_hit = current_price <= position.take_profit_price_decimal
            stop_hit = current_price >= position.stop_loss_price_decimal
            exit_side = "BUY"
            if not take_hit and not stop_hit:
                near_target_fade_exit = self._has_near_target_fade_exit(snapshot, position, current_price, net_pnl)

        if not take_hit and not stop_hit and not reverse_signal_hit and not trend_follow_exit and not near_target_fade_exit:
            if position.trend_follow_armed:
                self.logger.info(
                    "[BINANCE HOLD] symbol=%s side=%s reasons=강추세추종 net_pnl_pct=%s long_score=%s short_score=%s ema_fast=%s ema_slow=%s",
                    snapshot.symbol,
                    position.side,
                    self._format_decimal(net_pnl * Decimal('100'), '0.00'),
                    snapshot.long_score,
                    snapshot.short_score,
                    self._format_decimal(snapshot.ema_fast_1m, '0.0000'),
                    self._format_decimal(snapshot.ema_slow_1m, '0.0000'),
                )
                return
            self.logger.info(
                "[BINANCE HOLD] symbol=%s side=%s reasons=매도대기 gross_pnl_pct=%s net_pnl_pct=%s 익절라인=%s 손절라인=%s entry_split=%s/%s exit_split=%s/%s qty=%s",
                snapshot.symbol,
                position.side,
                self._format_decimal(gross_pnl * Decimal('100'), '0.00'),
                self._format_decimal(net_pnl * Decimal('100'), '0.00'),
                self._format_decimal(position.take_profit_price_decimal, '0.0000'),
                self._format_decimal(position.stop_loss_price_decimal, '0.0000'),
                position.entry_count,
                self.settings.entry_splits,
                position.exit_count,
                self.settings.exit_splits,
                self._format_decimal(position.quantity_decimal, '0.000'),
            )
            return

        requested_exit_quantity = position.quantity_decimal if (trend_follow_exit or near_target_fade_exit) else self._resolve_exit_quantity(position)
        exit_quantity = await self._resolve_effective_exit_quantity(position, requested_exit_quantity)
        execution = await self.service.create_market_order(
            symbol=snapshot.symbol,
            side=exit_side,
            quantity=exit_quantity,
            reduce_only=True,
            dry_run=self.settings.dry_run,
        )
        if execution is None or execution.executed_qty <= 0:
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reasons=청산미체결 split=%s/%s requested_qty=%s",
                snapshot.symbol,
                position.exit_count + 1,
                self.settings.exit_splits,
                self._format_decimal(exit_quantity, '0.000'),
            )
            return

        remaining_quantity = position.quantity_decimal - execution.executed_qty
        remaining_notional = remaining_quantity * position.entry_price_decimal
        remaining_margin = Decimal('0') if position.quantity_decimal <= 0 else position.margin_usdt_decimal * (remaining_quantity / position.quantity_decimal)
        if reverse_signal_hit:
            exit_reason = f"반대신호전환:{preferred_side}"
        elif near_target_fade_exit:
            exit_reason = "익절근접후모멘텀둔화"
        elif trend_follow_exit:
            exit_reason = "상승추세꺾임익절"
        else:
            exit_reason = "익절라인도달" if take_hit else "손절라인이탈"
        exit_reason_details = reverse_reasons if reverse_signal_hit and reverse_reasons else []
        if near_target_fade_exit:
            exit_reason_details = [
                f"net_pnl={self._format_decimal(net_pnl * Decimal('100'), '0.00')}%",
                f"ema_fast={self._format_decimal(snapshot.ema_fast_1m, '0.0000')}",
                f"ema_slow={self._format_decimal(snapshot.ema_slow_1m, '0.0000')}",
            ]
        exit_split_count = position.exit_count + 1
        if remaining_quantity > 0 and exit_split_count < self.settings.exit_splits and not trend_follow_exit and not near_target_fade_exit:
            updated = self._build_position(
                symbol=position.symbol,
                side=position.side,
                quantity=remaining_quantity,
                entry_price=position.entry_price_decimal,
                order_id=position.order_id,
                opened_at=position.opened_at,
                leverage=position.leverage,
                margin_usdt=remaining_margin,
                notional_usdt=remaining_notional,
                take_profit_price=position.take_profit_price_decimal,
                stop_loss_price=position.stop_loss_price_decimal,
                take_profit_pct=position.take_profit_pct_decimal,
                stop_loss_pct=position.stop_loss_pct_decimal,
                entry_count=position.entry_count,
                exit_count=exit_split_count,
                realized_pnl_usdt=position.realized_pnl_usdt_decimal,
                commission_usdt=position.commission_usdt_decimal,
                last_exit_update_at=position.last_exit_update_at,
                last_trade_sync_at=position.last_trade_sync_at,
            )
            self.state_store.set(updated)
            self.logger.info(
                "[BINANCE SCALE-OUT] symbol=%s side=%s reason=%s split=%s/%s executed_qty=%s remaining_qty=%s",
                snapshot.symbol,
                position.side,
                exit_reason,
                exit_split_count,
                self.settings.exit_splits,
                self._format_decimal(execution.executed_qty, '0.000'),
                self._format_decimal(remaining_quantity, '0.000'),
            )
        else:
            self.state_store.remove(snapshot.symbol)
            self.logger.info(
                "[BINANCE EXIT] symbol=%s side=%s reason=%s split=%s/%s executed_qty=%s remaining_qty=0.000",
                snapshot.symbol,
                position.side,
                exit_reason,
                exit_split_count,
                self.settings.exit_splits,
                self._format_decimal(execution.executed_qty, '0.000'),
            )
        await self.notifier.send(
            format_telegram_message(
                "[Binance 청산 체결]",
                fields=[
                    ("심볼", snapshot.symbol),
                    ("기존 방향", position.side),
                    ("청산 분할", f"{min(exit_split_count, self.settings.exit_splits)}/{self.settings.exit_splits}"),
                    ("체결 수량", execution.executed_qty),
                    ("잔여 수량", max(Decimal('0'), remaining_quantity)),
                    ("체결가", f"{execution.avg_price:.4f}"),
                    ("총 손익률", f"{gross_pnl * Decimal('100'):.2f}%"),
                    ("수수료 차감 후", f"{net_pnl * Decimal('100'):.2f}%"),
                    ("익절 라인", f"{position.take_profit_price_decimal:.4f}"),
                    ("순익 목표", f"{position.take_profit_pct_decimal * Decimal('100'):.2f}%"),
                    ("손절 라인", f"{position.stop_loss_price_decimal:.4f}"),
                    ("허용 손실", f"{position.stop_loss_pct_decimal * Decimal('100'):.2f}%"),
                    ("dry_run", self.settings.dry_run),
                ],
                sections=[
                    ("청산 사유", [exit_reason]),
                    ("반대 신호 근거", exit_reason_details or ["없음"]),
                ],
            )
        )