from __future__ import annotations

from decimal import Decimal

from binance_bot.state import Position
from binance_bot.telegram import format_telegram_message

from .plans import PlanDecision, StrategyOrderPlan
from .snapshot import MarketSnapshot


class ExitMixin:
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
        if position.side == "LONG":
            target_span = position.take_profit_price_decimal - position.entry_price_decimal
            if target_span <= 0:
                return False
            progress = (current_price - position.entry_price_decimal) / target_span
            momentum_faded = snapshot.ema_fast_1m <= snapshot.ema_slow_1m or snapshot.long_score <= snapshot.short_score + 1 or snapshot.rsi_3m < Decimal("58")
            return progress >= Decimal("0.30") and momentum_faded

        target_span = position.entry_price_decimal - position.take_profit_price_decimal
        if target_span <= 0:
            return False
        progress = (position.entry_price_decimal - current_price) / target_span
        momentum_faded = snapshot.ema_fast_1m >= snapshot.ema_slow_1m or snapshot.short_score <= snapshot.long_score + 1 or snapshot.rsi_3m > Decimal("42")
        return progress >= Decimal("0.30") and momentum_faded

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
        preferred_side = snapshot.preferred_side
        reverse_score = snapshot.long_score if preferred_side == "LONG" else snapshot.short_score
        reverse_reasons = snapshot.long_reasons if preferred_side == "LONG" else snapshot.short_reasons
        crash_short_signal = self._get_crash_short_signal(snapshot)
        rebound_long_signal = self._get_rebound_long_signal(snapshot)
        if position.side == "LONG" and crash_short_signal is not None:
            preferred_side = "SHORT"
            reverse_score = max(reverse_score, snapshot.short_score)
            reverse_reasons = crash_short_signal[1]
        elif position.side == "SHORT" and rebound_long_signal is not None:
            preferred_side = "LONG"
            reverse_score = max(reverse_score, snapshot.long_score)
            reverse_reasons = rebound_long_signal[1]

        reverse_signal_hit = self._has_reverse_exit_confirmation(snapshot, position, preferred_side, reverse_score)
        current_price = snapshot.bid_price if position.side == "LONG" else snapshot.ask_price
        trend_follow_exit = False
        near_target_fade_exit = False
        if position.side == "LONG":
            gross_pnl = ((current_price - position.entry_price_decimal) / position.entry_price_decimal) * Decimal(position.leverage)
            net_pnl = self._cycle_net_margin_pct(position, gross_pnl, position.leverage)
            take_hit = current_price >= position.take_profit_price_decimal
            stop_hit = current_price <= position.stop_loss_price_decimal
            order_side = "SELL"
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
            if position.trend_follow_armed:
                if self._is_long_trend_bent(snapshot):
                    trend_follow_exit = True
                    take_hit = False
                else:
                    take_hit = False
            if not take_hit and not stop_hit and not trend_follow_exit:
                near_target_fade_exit = self._has_near_target_fade_exit(snapshot, position, current_price, net_pnl)
        else:
            gross_pnl = ((position.entry_price_decimal - current_price) / position.entry_price_decimal) * Decimal(position.leverage)
            net_pnl = self._cycle_net_margin_pct(position, gross_pnl, position.leverage)
            take_hit = current_price <= position.take_profit_price_decimal
            stop_hit = current_price >= position.stop_loss_price_decimal
            order_side = "BUY"
            if not take_hit and not stop_hit:
                near_target_fade_exit = self._has_near_target_fade_exit(snapshot, position, current_price, net_pnl)

        if not take_hit and not stop_hit and not reverse_signal_hit and not trend_follow_exit and not near_target_fade_exit:
            return PlanDecision(allowed=False, reason="hold")

        requested_exit_quantity = position.quantity_decimal if (trend_follow_exit or near_target_fade_exit) else self._resolve_exit_quantity(position)
        exit_quantity = await self._resolve_effective_exit_quantity(position, requested_exit_quantity)
        if exit_quantity <= 0:
            return PlanDecision(allowed=False, reason="exit_quantity_zero")

        if reverse_signal_hit:
            reason = f"reverse:{preferred_side}"
            detail_reasons = reverse_reasons
        elif near_target_fade_exit:
            reason = "fade_exit"
            detail_reasons = [
                f"net_pnl={self._format_decimal(net_pnl * Decimal('100'), '0.00')}%",
                f"ema_fast={self._format_decimal(snapshot.ema_fast_1m, '0.0000')}",
                f"ema_slow={self._format_decimal(snapshot.ema_slow_1m, '0.0000')}",
            ]
        elif trend_follow_exit:
            reason = "trend_bent"
            detail_reasons = []
        else:
            reason = "take_profit" if take_hit else "stop_loss"
            detail_reasons = []

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
        if remaining_quantity > 0 and exit_split_count < self.settings.exit_splits and plan.reason not in {"trend_bent", "fade_exit"}:
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