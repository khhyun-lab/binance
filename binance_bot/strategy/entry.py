from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from binance_bot.services.binance_futures_service import AccountSnapshot
from binance_bot.state import Position
from binance_bot.telegram import format_telegram_message

from .plans import PlanDecision, StrategyOrderPlan
from .snapshot import MarketSnapshot


class EntryMixin:
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
        return (self._now_utc() - marked_at).total_seconds() <= window_seconds

    def _is_crash_short_context(self, snapshot: MarketSnapshot) -> bool:
        breakout_pressure = (
            snapshot.mark_price <= (snapshot.breakout_low - (snapshot.atr_3m * Decimal("0.05")))
            or snapshot.mark_price <= (snapshot.recent_low + (snapshot.atr_3m * Decimal("0.08")))
        )
        momentum_pressure = (
            snapshot.trend_short_ok
            and snapshot.ema_fast_1m < snapshot.ema_slow_1m
            and snapshot.short_score >= max(4, self._entry_score_threshold_for_side("SHORT"))
        )
        oversold_pressure = Decimal("18") <= snapshot.rsi_1m <= Decimal("34") and Decimal("24") <= snapshot.rsi_3m <= Decimal("40")
        volume_pressure = Decimal("1.40") <= snapshot.volume_ratio <= Decimal("4.50")
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
            return snapshot.trend_long_ok and score_edge_ok and snapshot.ema_fast_1m > snapshot.ema_slow_1m and sum(1 for condition in conditions if condition) >= self.settings.entry_momentum_min_conditions

        conditions = [
            snapshot.mark_price <= snapshot.breakout_low,
            snapshot.volume_ratio >= self.settings.entry_min_volume_ratio,
            snapshot.rsi_3m <= self.settings.short_entry_max_rsi_3m,
            snapshot.short_score >= threshold + 1,
        ]
        score_edge_ok = snapshot.short_score >= snapshot.long_score + 2
        return snapshot.trend_short_ok and score_edge_ok and snapshot.ema_fast_1m < snapshot.ema_slow_1m and sum(1 for condition in conditions if condition) >= self.settings.entry_momentum_min_conditions

    def _is_exhausted_entry(self, snapshot: MarketSnapshot, side: str) -> bool:
        if side == "LONG":
            return (snapshot.rsi_3m >= Decimal("72") and not snapshot.trend_long_ok) or (
                snapshot.rsi_3m >= Decimal("68")
                and snapshot.volume_ratio >= Decimal("1.80")
                and snapshot.mark_price >= snapshot.breakout_high
            )
        return snapshot.rsi_3m <= Decimal("28") and not snapshot.trend_short_ok

    def _can_scale_in_position(self, snapshot: MarketSnapshot, position: Position) -> bool:
        if position.side == "LONG":
            price_ok = snapshot.mark_price >= position.entry_price_decimal
            trend_ok = self._is_strong_long_trend(snapshot)
            exhaustion_ok = snapshot.rsi_3m <= Decimal("68") and snapshot.volume_ratio <= Decimal("2.50")
        else:
            price_ok = snapshot.mark_price <= position.entry_price_decimal
            trend_ok = snapshot.trend_short_ok and snapshot.short_score >= self._entry_score_threshold_for_side("SHORT") + 1 and snapshot.ema_fast_1m < snapshot.ema_slow_1m
            exhaustion_ok = snapshot.rsi_1m >= Decimal("24") and snapshot.rsi_3m >= Decimal("34") and snapshot.volume_ratio >= Decimal("0.90")
        return price_ok and trend_ok and exhaustion_ok

    async def _plan_scale_in(self, snapshot: MarketSnapshot, account_snapshot: AccountSnapshot, position: Position) -> PlanDecision:
        if position.entry_count >= self.settings.entry_splits:
            return PlanDecision(allowed=False, reason="max_entry_splits")
        if self._is_sideways_regime(snapshot):
            return PlanDecision(allowed=False, reason="sideways_scale_blocked")

        preferred_side = snapshot.preferred_side
        if preferred_side != position.side:
            return PlanDecision(allowed=False, reason="preferred_side_mismatch")

        preferred_score = snapshot.long_score if preferred_side == "LONG" else snapshot.short_score
        preferred_reasons = snapshot.long_reasons if preferred_side == "LONG" else snapshot.short_reasons
        score_threshold = self._entry_score_threshold_for_side(preferred_side)
        if preferred_score < score_threshold:
            return PlanDecision(allowed=False, reason="score_below_threshold", detail_reasons=preferred_reasons)
        if not self._has_entry_momentum(snapshot, preferred_side):
            return PlanDecision(allowed=False, reason="momentum_blocked", detail_reasons=preferred_reasons)
        if not self._can_scale_in_position(snapshot, position):
            return PlanDecision(allowed=False, reason="scale_in_guard_blocked", detail_reasons=preferred_reasons)

        additional_margin, quantity, effective_remaining_splits = await self._resolve_entry_order_plan(
            snapshot.symbol,
            snapshot.mark_price,
            account_snapshot.available_balance,
            position.leverage,
            position,
        )
        if quantity <= 0:
            return PlanDecision(allowed=False, reason="quantity_normalized_to_zero", detail_reasons=preferred_reasons)

        return PlanDecision(
            allowed=True,
            reason="scale_in",
            detail_reasons=preferred_reasons,
            plan=StrategyOrderPlan(
                kind="scale_in",
                symbol=snapshot.symbol,
                position_side=preferred_side,
                order_side="BUY" if preferred_side == "LONG" else "SELL",
                quantity=quantity,
                margin_usdt=additional_margin,
                reason="scale_in",
                detail_reasons=preferred_reasons,
                score_display=f"{position.entry_count + 1}/{position.entry_count + effective_remaining_splits}",
            ),
        )

    async def _plan_entry(self, snapshot: MarketSnapshot, account_snapshot: AccountSnapshot) -> PlanDecision:
        if len(self.state_store.positions) >= self.settings.max_open_positions:
            return PlanDecision(allowed=False, reason="max_open_positions")

        crash_short_signal = self._get_crash_short_signal(snapshot)
        rebound_long_signal = None if crash_short_signal is not None else self._get_rebound_long_signal(snapshot)
        preferred_side = snapshot.preferred_side
        preferred_reasons: list[str] = []
        score_display = "range"
        reason = "signal"
        if crash_short_signal is not None:
            preferred_side, preferred_reasons = crash_short_signal
            score_display = "crash"
            reason = "crash_short"
        elif rebound_long_signal is not None:
            preferred_side, preferred_reasons = rebound_long_signal
            score_display = "rebound"
            reason = "rebound_long"
        elif preferred_side is None:
            sideways_signal = self._get_sideways_entry_signal(snapshot)
            if sideways_signal is None:
                return PlanDecision(allowed=False, reason="no_signal")
            preferred_side, preferred_reasons = sideways_signal
            reason = "sideways"
        else:
            preferred_score = snapshot.long_score if preferred_side == "LONG" else snapshot.short_score
            preferred_reasons = snapshot.long_reasons if preferred_side == "LONG" else snapshot.short_reasons
            score_threshold = self._entry_score_threshold_for_side(preferred_side)
            score_display = f"{preferred_score}/{score_threshold}"
            if self._is_exhausted_entry(snapshot, preferred_side):
                return PlanDecision(allowed=False, reason="exhausted_entry", detail_reasons=preferred_reasons)
            if preferred_score < score_threshold:
                sideways_signal = self._get_sideways_entry_signal(snapshot)
                if sideways_signal is None:
                    return PlanDecision(allowed=False, reason="score_below_threshold", detail_reasons=preferred_reasons)
                preferred_side, preferred_reasons = sideways_signal
                score_display = "range"
                reason = "sideways"
            elif not self._has_entry_momentum(snapshot, preferred_side):
                sideways_signal = self._get_sideways_entry_signal(snapshot)
                if sideways_signal is None:
                    return PlanDecision(allowed=False, reason="momentum_blocked", detail_reasons=preferred_reasons)
                preferred_side, preferred_reasons = sideways_signal
                score_display = "range"
                reason = "sideways"

        available_margin, quantity, effective_remaining_splits = await self._resolve_entry_order_plan(
            snapshot.symbol,
            snapshot.mark_price,
            account_snapshot.available_balance,
            self.settings.leverage,
            None,
        )
        if quantity <= 0:
            return PlanDecision(allowed=False, reason="quantity_normalized_to_zero", detail_reasons=preferred_reasons)

        return PlanDecision(
            allowed=True,
            reason=reason,
            detail_reasons=preferred_reasons,
            plan=StrategyOrderPlan(
                kind="enter",
                symbol=snapshot.symbol,
                position_side=preferred_side,
                order_side="BUY" if preferred_side == "LONG" else "SELL",
                quantity=quantity,
                margin_usdt=available_margin,
                reason=reason,
                detail_reasons=preferred_reasons,
                score_display=f"1/{effective_remaining_splits}" if score_display == "range" else score_display,
            ),
        )

    async def _maybe_scale_in(self, snapshot: MarketSnapshot, account_snapshot: AccountSnapshot, position: Position) -> Position:
        decision = await self._plan_scale_in(snapshot, account_snapshot, position)
        if not decision.allowed or decision.plan is None:
            return position

        await self._ensure_entry_leverage(snapshot.symbol, position.leverage)
        plan = decision.plan
        effective_total_splits = max(position.entry_count + 1, self.settings.entry_splits)

        try:
            execution = await self.service.create_aggressive_limit_order(symbol=snapshot.symbol, side=plan.order_side, quantity=plan.quantity, reduce_only=False, dry_run=self.settings.dry_run)
        except Exception as exc:
            self.logger.exception(
                "[BINANCE ORDER ERROR] symbol=%s phase=scale-in side=%s qty=%s error=%s",
                snapshot.symbol,
                plan.position_side,
                self._format_decimal(plan.quantity, "0.000"),
                exc,
            )
            return position
        if execution is None or execution.executed_qty <= 0:
            return position

        total_quantity = position.quantity_decimal + execution.executed_qty
        total_notional = position.notional_usdt_decimal + (execution.executed_qty * execution.avg_price)
        average_entry = total_notional / total_quantity
        total_margin = position.margin_usdt_decimal + plan.margin_usdt
        take_profit_price, stop_loss_price, take_profit_pct, stop_loss_pct = self._calculate_exit_lines(snapshot, plan.position_side, average_entry, position.leverage)
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
            plan.position_side,
            updated.entry_count,
            effective_total_splits,
            self._format_decimal(plan.margin_usdt, "0.00"),
            self._format_decimal(execution.executed_qty, "0.000"),
            self._format_decimal(average_entry, "0.0000"),
            self._format_decimal(total_quantity, "0.000"),
        )
        await self.notifier.send(
            format_telegram_message(
                "[Binance 분할진입 체결]",
                fields=[
                    ("심볼", snapshot.symbol),
                    ("방향", plan.position_side),
                    ("분할", f"{updated.entry_count}/{effective_total_splits}"),
                    ("추가 증거금", f"{plan.margin_usdt:.2f} USDT"),
                    ("추가 수량", execution.executed_qty),
                    ("평단", f"{average_entry:.4f}"),
                    ("총 수량", total_quantity),
                    ("dry_run", self.settings.dry_run),
                ],
                sections=[("진입 근거", plan.detail_reasons or ["none"])],
            )
        )
        return updated

    async def _maybe_enter(self, snapshot: MarketSnapshot, account_snapshot: AccountSnapshot) -> None:
        decision = await self._plan_entry(snapshot, account_snapshot)
        if not decision.allowed or decision.plan is None:
            self.logger.info("[BINANCE HOLD] symbol=%s reasons=%s", snapshot.symbol, decision.reason)
            return

        plan = decision.plan
        await self._ensure_entry_leverage(snapshot.symbol, self.settings.leverage)

        try:
            execution = await self.service.create_aggressive_limit_order(symbol=snapshot.symbol, side=plan.order_side, quantity=plan.quantity, reduce_only=False, dry_run=self.settings.dry_run)
        except Exception as exc:
            self.logger.exception(
                "[BINANCE ORDER ERROR] symbol=%s phase=entry side=%s qty=%s error=%s",
                snapshot.symbol,
                plan.position_side,
                self._format_decimal(plan.quantity, "0.000"),
                exc,
            )
            return
        if execution is None or execution.executed_qty <= 0:
            return

        take_profit_price, stop_loss_price, take_profit_pct, stop_loss_pct = self._calculate_exit_lines(snapshot, plan.position_side, execution.avg_price, self.settings.leverage)
        position = self._build_position(
            symbol=snapshot.symbol,
            side=plan.position_side,
            quantity=execution.executed_qty,
            entry_price=execution.avg_price,
            order_id=execution.order_id,
            opened_at=datetime.now(tz=timezone.utc).isoformat(),
            leverage=self.settings.leverage,
            margin_usdt=plan.margin_usdt,
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
        if plan.position_side == "LONG" and plan.reason == "rebound_long":
            self._clear_recent_crash_context(snapshot.symbol)

        self.logger.info(
            "[BINANCE ENTRY] symbol=%s side=%s split=%s/%s requested_margin=%s executed_qty=%s entry_price=%s",
            snapshot.symbol,
            plan.position_side,
            position.entry_count,
            self.settings.entry_splits,
            self._format_decimal(plan.margin_usdt, "0.00"),
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