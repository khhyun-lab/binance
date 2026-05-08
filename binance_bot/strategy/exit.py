from __future__ import annotations

from decimal import Decimal

from binance_bot.state import Position
from binance_bot.telegram import format_telegram_message

from .plans import PlanDecision, StrategyOrderPlan
from .snapshot import MarketSnapshot


class ExitMixin:
    def _has_breakout_failure_exit(self, snapshot: MarketSnapshot, position: Position) -> bool:
        if position.entry_type.startswith("pullback_reaccel"):
            return False
        entry_buffer = snapshot.atr_3m * Decimal("0.60")
        if position.side == "LONG":
            lost_breakout = (
                snapshot.previous_close_1m < snapshot.breakout_high
                and snapshot.latest_close_1m < snapshot.breakout_high
            )
            adverse_move_confirmed = snapshot.mark_price <= position.entry_price_decimal - entry_buffer
            momentum_failed = (
                snapshot.ema_fast_1m <= snapshot.ema_slow_1m
                or snapshot.mark_price < snapshot.ema_slow_1m
                or snapshot.rsi_1m <= Decimal("38")
                or snapshot.rsi_3m <= Decimal("45")
            )
            return lost_breakout and adverse_move_confirmed and momentum_failed

        lost_breakout = (
            snapshot.previous_close_1m > snapshot.breakout_low
            and snapshot.latest_close_1m > snapshot.breakout_low
        )
        adverse_move_confirmed = snapshot.mark_price >= position.entry_price_decimal + entry_buffer
        momentum_failed = (
            snapshot.ema_fast_1m >= snapshot.ema_slow_1m
            or snapshot.mark_price > snapshot.ema_slow_1m
            or snapshot.rsi_1m >= Decimal("62")
            or snapshot.rsi_3m >= Decimal("55")
        )
        return lost_breakout and adverse_move_confirmed and momentum_failed

    def _has_reverse_exit_confirmation(self, snapshot: MarketSnapshot, position: Position, preferred_side: str | None, reverse_score: int) -> bool:
        if preferred_side is None or preferred_side == position.side:
            return False
        threshold = self._entry_score_threshold_for_side(preferred_side) + 1
        if reverse_score < threshold:
            return False
        if position.side == "LONG":
            return snapshot.trend_short_ok or (snapshot.mark_price <= snapshot.breakout_low and snapshot.ema_fast_1m < snapshot.ema_slow_1m and snapshot.rsi_3m <= Decimal("45"))
        return snapshot.trend_long_ok or (snapshot.mark_price >= snapshot.breakout_high and snapshot.ema_fast_1m > snapshot.ema_slow_1m and snapshot.rsi_3m >= Decimal("55"))

    def _has_near_target_fade_exit(self, snapshot: MarketSnapshot, position: Position, current_price: Decimal, net_pnl: Decimal) -> bool:
        if net_pnl < Decimal("0.0005"):
            return False
        progress_threshold = self.settings.pullback_near_target_fade_progress if position.entry_type.startswith("pullback_reaccel") else self.settings.near_target_fade_progress
        if position.side == "LONG":
            target_span = position.take_profit_price_decimal - position.entry_price_decimal
            if target_span <= 0:
                return False
            progress = (current_price - position.entry_price_decimal) / target_span
            momentum_faded = snapshot.ema_fast_1m <= snapshot.ema_slow_1m or snapshot.long_score <= snapshot.short_score + 1 or snapshot.rsi_3m < Decimal("58")
            return progress >= progress_threshold and momentum_faded

        target_span = position.entry_price_decimal - position.take_profit_price_decimal
        if target_span <= 0:
            return False
        progress = (position.entry_price_decimal - current_price) / target_span
        momentum_faded = snapshot.ema_fast_1m >= snapshot.ema_slow_1m or snapshot.short_score <= snapshot.long_score + 1 or snapshot.rsi_3m > Decimal("42")
        return progress >= progress_threshold and momentum_faded

    def _has_pullback_invalidation_exit(self, snapshot: MarketSnapshot, position: Position, current_price: Decimal, net_pnl: Decimal) -> bool:
        if not position.entry_type.startswith("pullback_reaccel"):
            return False
        risk_distance = (
            position.entry_price_decimal - position.stop_loss_price_decimal
            if position.side == "LONG"
            else position.stop_loss_price_decimal - position.entry_price_decimal
        )
        if risk_distance <= 0:
            return False
        current_r = (
            (current_price - position.entry_price_decimal) / risk_distance
            if position.side == "LONG"
            else (position.entry_price_decimal - current_price) / risk_distance
        )
        if position.side == "LONG":
            structure_broken = position.pullback_low_decimal > 0 and snapshot.latest_close_1m < position.pullback_low_decimal
            momentum_failed = snapshot.ema_fast_1m <= snapshot.ema_slow_1m or snapshot.rsi_3m < self.settings.pullback_reaccel_rsi_long_min
        else:
            structure_broken = position.pullback_high_decimal > 0 and snapshot.latest_close_1m > position.pullback_high_decimal
            momentum_failed = snapshot.ema_fast_1m >= snapshot.ema_slow_1m or snapshot.rsi_3m > self.settings.pullback_reaccel_rsi_short_max
        stalled = position.invalidation_deadline_ms > 0 and snapshot.latest_close_time_ms >= position.invalidation_deadline_ms and current_r < Decimal("0.35") and net_pnl <= Decimal("0.0010")
        return structure_broken or (stalled and momentum_failed)

    def _is_strong_short_trend(self, snapshot: MarketSnapshot) -> bool:
        threshold = self._entry_score_threshold_for_side("SHORT")
        score_ok = snapshot.short_score >= threshold + 2
        momentum_ok = snapshot.volume_ratio >= Decimal("1.05") or snapshot.rsi_3m <= Decimal("39")
        breakout_ok = snapshot.mark_price <= snapshot.breakout_low or snapshot.mark_price <= snapshot.recent_low
        micro_trend_ok = snapshot.ema_fast_1m < snapshot.ema_slow_1m and snapshot.mark_price <= snapshot.ema_fast_1m
        return snapshot.trend_short_ok and snapshot.short_score > snapshot.long_score and score_ok and momentum_ok and breakout_ok and micro_trend_ok

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

    async def _plan_exit(self, snapshot: MarketSnapshot, position: Position) -> PlanDecision:
        current_price = snapshot.bid_price if position.side == "LONG" else snapshot.ask_price
        if position.side == "LONG":
            gross_pnl = ((current_price - position.entry_price_decimal) / position.entry_price_decimal) * Decimal(position.leverage)
            net_pnl = self._cycle_net_margin_pct(position, gross_pnl, position.leverage)
            take_hit = current_price >= position.take_profit_price_decimal
            stop_hit = current_price <= position.stop_loss_price_decimal
            order_side = "SELL"
        else:
            gross_pnl = ((position.entry_price_decimal - current_price) / position.entry_price_decimal) * Decimal(position.leverage)
            net_pnl = self._cycle_net_margin_pct(position, gross_pnl, position.leverage)
            take_hit = current_price <= position.take_profit_price_decimal
            stop_hit = current_price >= position.stop_loss_price_decimal
            order_side = "BUY"

        near_target_fade_exit = self._has_near_target_fade_exit(snapshot, position, current_price, net_pnl)
        breakout_failure_exit = self._has_breakout_failure_exit(snapshot, position)
        pullback_invalidation_exit = self._has_pullback_invalidation_exit(snapshot, position, current_price, net_pnl)

        if not take_hit and not stop_hit and not near_target_fade_exit and not breakout_failure_exit and not pullback_invalidation_exit:
            return PlanDecision(allowed=False, reason="hold")

        requested_exit_quantity = self._resolve_exit_quantity(position)
        exit_quantity = await self._resolve_effective_exit_quantity(position, requested_exit_quantity)
        if exit_quantity <= 0:
            return PlanDecision(allowed=False, reason="exit_quantity_zero")

        if near_target_fade_exit and not take_hit and not stop_hit:
            reason = "near_target_fade"
            detail_reasons = [
                f"net_pnl={self._format_decimal(net_pnl * Decimal('100'), '0.00')}%",
                f"long_score={snapshot.long_score}",
                f"short_score={snapshot.short_score}",
                f"rsi_3m={self._format_decimal(snapshot.rsi_3m, '0.00')}",
            ]
        elif breakout_failure_exit and not take_hit and not stop_hit:
            reason = "breakout_failure"
            detail_reasons = [
                f"net_pnl={self._format_decimal(net_pnl * Decimal('100'), '0.00')}%",
                f"rsi_1m={self._format_decimal(snapshot.rsi_1m, '0.00')}",
                f"rsi_3m={self._format_decimal(snapshot.rsi_3m, '0.00')}",
            ]
        elif pullback_invalidation_exit and not take_hit and not stop_hit:
            reason = "pullback_invalidation"
            detail_reasons = [
                f"net_pnl={self._format_decimal(net_pnl * Decimal('100'), '0.00')}%",
                f"pullback_low={self._format_decimal(position.pullback_low_decimal, '0.0000')}",
                f"pullback_high={self._format_decimal(position.pullback_high_decimal, '0.0000')}",
            ]
        else:
            reason = "take_profit" if take_hit else "stop_loss"
            detail_reasons = [f"net_pnl={self._format_decimal(net_pnl * Decimal('100'), '0.00')}%"]

        return PlanDecision(
            allowed=True,
            reason=reason,
            detail_reasons=detail_reasons,
            plan=StrategyOrderPlan(
                kind="exit",
                symbol=snapshot.symbol,
                position_side=position.side,
                order_side=order_side,
                quantity=exit_quantity,
                margin_usdt=position.margin_usdt_decimal,
                reason=reason,
                detail_reasons=detail_reasons,
                score_display=f"{position.exit_count + 1}/{self.settings.exit_splits}",
            ),
        )

    async def _maybe_exit(self, snapshot: MarketSnapshot, position: Position) -> None:
        decision = await self._plan_exit(snapshot, position)
        if not decision.allowed or decision.plan is None:
            self.logger.info("[BINANCE HOLD] symbol=%s side=%s reasons=%s", snapshot.symbol, position.side, decision.reason)
            return

        plan = decision.plan
        try:
            execution = await self.service.create_aggressive_limit_order(symbol=snapshot.symbol, side=plan.order_side, quantity=plan.quantity, reduce_only=True, dry_run=self.settings.dry_run, market_fallback=True)
        except Exception as exc:
            self.logger.exception(
                "[BINANCE ORDER ERROR] symbol=%s phase=exit side=%s qty=%s error=%s",
                snapshot.symbol,
                plan.order_side,
                self._format_decimal(plan.quantity, "0.000"),
                exc,
            )
            return
        if execution is None or execution.executed_qty <= 0:
            return

        current_price = snapshot.bid_price if position.side == "LONG" else snapshot.ask_price
        if position.side == "LONG":
            gross_pnl = ((current_price - position.entry_price_decimal) / position.entry_price_decimal) * Decimal(position.leverage)
        else:
            gross_pnl = ((position.entry_price_decimal - current_price) / position.entry_price_decimal) * Decimal(position.leverage)
        net_pnl = self._cycle_net_margin_pct(position, gross_pnl, position.leverage)
        remaining_quantity = position.quantity_decimal - execution.executed_qty
        remaining_notional = remaining_quantity * position.entry_price_decimal
        remaining_margin = Decimal('0') if position.quantity_decimal <= 0 else position.margin_usdt_decimal * (remaining_quantity / position.quantity_decimal)
        exit_reason = plan.reason
        exit_reason_details = plan.detail_reasons
        exit_split_count = position.exit_count + 1
        if remaining_quantity > 0 and exit_split_count < self.settings.exit_splits:
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
                entry_type=position.entry_type,
                breakout_level=position.breakout_level_decimal,
                pullback_low=position.pullback_low_decimal,
                pullback_high=position.pullback_high_decimal,
                invalidation_deadline_ms=position.invalidation_deadline_ms,
            )
            self.state_store.set(updated)
            self.logger.info(
                "[BINANCE SCALE-OUT] symbol=%s side=%s reason=%s split=%s/%s executed_qty=%s remaining_qty=%s gross_pnl_pct=%s net_pnl_pct=%s",
                snapshot.symbol,
                position.side,
                exit_reason,
                exit_split_count,
                self.settings.exit_splits,
                self._format_decimal(execution.executed_qty, '0.000'),
                self._format_decimal(remaining_quantity, '0.000'),
                self._format_decimal(gross_pnl * Decimal('100'), '0.00'),
                self._format_decimal(net_pnl * Decimal('100'), '0.00'),
            )
        else:
            self.state_store.remove(snapshot.symbol)
            self.logger.info(
                "[BINANCE EXIT] symbol=%s side=%s reason=%s split=%s/%s executed_qty=%s remaining_qty=0.000 gross_pnl_pct=%s net_pnl_pct=%s",
                snapshot.symbol,
                position.side,
                exit_reason,
                exit_split_count,
                self.settings.exit_splits,
                self._format_decimal(execution.executed_qty, '0.000'),
                self._format_decimal(gross_pnl * Decimal('100'), '0.00'),
                self._format_decimal(net_pnl * Decimal('100'), '0.00'),
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
                sections=[("청산 사유", [exit_reason]), ("반대 신호 근거", exit_reason_details or ["없음"])],
            )
        )