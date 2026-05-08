from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from binance_bot.state import Position
from binance_bot.telegram import format_telegram_message

from .snapshot import MarketSnapshot


class RiskMixin:
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
        commission_usdt = sum((trade.commission for trade in trades if trade.commission_asset == "USDT"), Decimal("0"))
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

    def _effective_exit_target_bounds(self, snapshot: MarketSnapshot, side: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        if side == "SHORT":
            return (
                self.settings.short_min_take_profit_on_margin_pct,
                self.settings.short_max_take_profit_on_margin_pct,
                self.settings.short_min_stop_loss_on_margin_pct,
                self.settings.short_max_stop_loss_on_margin_pct,
            )

        return (
            self.settings.min_take_profit_on_margin_pct,
            self.settings.max_take_profit_on_margin_pct,
            self.settings.min_stop_loss_on_margin_pct,
            self.settings.max_stop_loss_on_margin_pct,
        )

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

    def _calculate_exit_lines(self, snapshot: MarketSnapshot, side: str, entry_price: Decimal, leverage: int, position: Position | None = None) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        atr_fast = snapshot.atr_3m
        atr_slow = snapshot.atr_15m
        atr_base = (atr_fast * Decimal("0.7")) + (atr_slow * Decimal("0.3"))
        volatility_ratio = atr_fast / entry_price if entry_price > 0 else Decimal("0")
        realized_margin_pct, paid_fee_margin_pct, remaining_exit_fee_margin_pct = self._position_trade_margin_terms(position, leverage)
        (
            effective_min_take_profit_pct,
            effective_max_take_profit_pct,
            effective_min_stop_loss_pct,
            effective_max_stop_loss_pct,
        ) = self._effective_exit_target_bounds(snapshot, side)
        if side == "SHORT":
            if snapshot.volume_ratio >= Decimal("1.8") or volatility_ratio >= Decimal("0.0035"):
                stop_atr_multiplier = Decimal("0.58")
                take_profit_atr_multiplier = Decimal("0.60")
            elif snapshot.volume_ratio >= Decimal("1.2") or volatility_ratio >= Decimal("0.0023"):
                stop_atr_multiplier = Decimal("0.52")
                take_profit_atr_multiplier = Decimal("0.54")
            else:
                stop_atr_multiplier = Decimal("0.46")
                take_profit_atr_multiplier = Decimal("0.48")
        elif snapshot.volume_ratio >= Decimal("1.8") or volatility_ratio >= Decimal("0.0035"):
            stop_atr_multiplier = Decimal("0.86")
            take_profit_atr_multiplier = Decimal("0.78")
        elif snapshot.volume_ratio >= Decimal("1.2") or volatility_ratio >= Decimal("0.0023"):
            stop_atr_multiplier = Decimal("0.76")
            take_profit_atr_multiplier = Decimal("0.70")
        else:
            stop_atr_multiplier = Decimal("0.66")
            take_profit_atr_multiplier = Decimal("0.60")

        if side == "LONG" and snapshot.rsi_3m >= Decimal("68"):
            stop_atr_multiplier = max(Decimal("0.40"), stop_atr_multiplier - Decimal("0.08"))
            take_profit_atr_multiplier = max(Decimal("0.62"), take_profit_atr_multiplier - Decimal("0.12"))
        if side == "SHORT" and snapshot.rsi_3m <= Decimal("32"):
            stop_atr_multiplier = max(Decimal("0.32"), stop_atr_multiplier - Decimal("0.08"))
            take_profit_atr_multiplier = max(Decimal("0.48"), take_profit_atr_multiplier - Decimal("0.10"))

        minimum_take_profit_pct = self._take_profit_move_with_trade_costs(effective_min_take_profit_pct, leverage, position)
        maximum_take_profit_pct = self._take_profit_move_with_trade_costs(effective_max_take_profit_pct, leverage, position)
        minimum_stop_loss_pct = self._stop_loss_move_with_trade_costs(effective_min_stop_loss_pct, leverage, position)
        maximum_stop_loss_pct = self._stop_loss_move_with_trade_costs(effective_max_stop_loss_pct, leverage, position)

        if side == "LONG":
            fixed_take_profit_price = entry_price * (Decimal("1") + minimum_take_profit_pct)
            fixed_stop_loss_price = entry_price * (Decimal("1") - minimum_stop_loss_pct)
            deepest_stop_price = entry_price * (Decimal("1") - maximum_stop_loss_pct)
            structural_stop_price = min(snapshot.recent_low - (atr_fast * Decimal("0.08")), entry_price - (atr_base * stop_atr_multiplier))
            stop_loss_price = min(fixed_stop_loss_price, structural_stop_price)
            stop_loss_price = max(stop_loss_price, deepest_stop_price)
            structural_take_profit = min(max(snapshot.recent_high, entry_price) + (atr_fast * Decimal("0.12")), entry_price + (atr_base * take_profit_atr_multiplier))
            take_profit_cap = entry_price * (Decimal("1") + maximum_take_profit_pct)
            take_profit_price = max(fixed_take_profit_price, structural_take_profit)
            take_profit_price = min(take_profit_price, take_profit_cap)
            stop_loss_pct = self._cycle_net_margin_pct(position, -((Decimal("1") - (stop_loss_price / entry_price)) * Decimal(leverage)), leverage)
            target_take_profit_pct = self._reward_risk_take_profit_target_pct(stop_loss_pct, effective_min_take_profit_pct, effective_max_take_profit_pct)
            take_profit_price, take_profit_pct = self._take_profit_price_from_target_pct(side, entry_price, leverage, position, target_take_profit_pct)
        else:
            fixed_take_profit_price = entry_price * (Decimal("1") - minimum_take_profit_pct)
            fixed_stop_loss_price = entry_price * (Decimal("1") + minimum_stop_loss_pct)
            highest_stop_price = entry_price * (Decimal("1") + maximum_stop_loss_pct)
            structural_stop_price = max(snapshot.recent_high + (atr_fast * Decimal("0.08")), entry_price + (atr_base * stop_atr_multiplier))
            stop_loss_price = max(fixed_stop_loss_price, structural_stop_price)
            stop_loss_price = min(stop_loss_price, highest_stop_price)
            structural_take_profit = max(min(snapshot.recent_low, entry_price) - (atr_fast * Decimal("0.12")), entry_price - (atr_base * take_profit_atr_multiplier))
            take_profit_floor = entry_price * (Decimal("1") - maximum_take_profit_pct)
            take_profit_price = min(fixed_take_profit_price, structural_take_profit)
            take_profit_price = max(take_profit_price, take_profit_floor)
            stop_loss_pct = self._cycle_net_margin_pct(position, -(((stop_loss_price / entry_price) - Decimal("1")) * Decimal(leverage)), leverage)
            target_take_profit_pct = self._reward_risk_take_profit_target_pct(stop_loss_pct, effective_min_take_profit_pct, effective_max_take_profit_pct)
            take_profit_price, take_profit_pct = self._take_profit_price_from_target_pct(side, entry_price, leverage, position, target_take_profit_pct)

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
        calculated_take_profit_price, calculated_stop_loss_price, _, _ = self._calculate_exit_lines(snapshot, position.side, position.entry_price_decimal, position.leverage, position)
        refresh_due = self._is_exit_line_refresh_due(position.last_exit_update_at)
        sideways_transition_due = False
        if position.has_exit_lines and not refresh_due and not sideways_transition_due:
            return position

        current_net_margin_pct = self._current_net_margin_pct(snapshot, position)
        _, _, remaining_exit_fee_margin_pct = self._position_trade_margin_terms(position, position.leverage)
        refreshed_at = self._now_utc().isoformat()
        freeze_floor = -(remaining_exit_fee_margin_pct + Decimal("0.0015"))
        if position.has_exit_lines and not sideways_transition_due and freeze_floor <= current_net_margin_pct <= 0:
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

        if position.has_exit_lines and not sideways_transition_due and not self._has_material_exit_line_change(snapshot, position, calculated_take_profit_price, calculated_stop_loss_price):
            return position

        take_profit_price = calculated_take_profit_price
        stop_loss_price = calculated_stop_loss_price
        previous_take_profit_price = position.take_profit_price_decimal
        previous_stop_loss_price = position.stop_loss_price_decimal
        previous_take_profit_pct = position.take_profit_pct_decimal
        previous_stop_loss_pct = position.stop_loss_pct_decimal

        if position.has_exit_lines and take_profit_price == position.take_profit_price_decimal and stop_loss_price == position.stop_loss_price_decimal:
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
        line_update_reasons = self._build_exit_line_update_reasons(snapshot, position, previous_take_profit_price, take_profit_price, previous_stop_loss_price, stop_loss_price)
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
                    ("익절 라인", f"{previous_take_profit_price:.4f} -> {take_profit_price:.4f} ({self._format_line_delta(previous_take_profit_price, take_profit_price)})" if position.has_exit_lines else f"{take_profit_price:.4f}"),
                    ("순익 목표", f"{previous_take_profit_pct * Decimal('100'):.2f}% -> {take_profit_pct * Decimal('100'):.2f}% ({self._format_line_delta(previous_take_profit_pct * Decimal('100'), take_profit_pct * Decimal('100'), '0.01')})" if position.has_exit_lines else f"{take_profit_pct * Decimal('100'):.2f}%"),
                    ("목표 순익 USDT", f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, previous_take_profit_pct):.4f} -> {self._margin_pct_to_usdt(position.margin_usdt_decimal, take_profit_pct):.4f}" if position.has_exit_lines else f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, take_profit_pct):.4f}"),
                    ("손절 라인", f"{previous_stop_loss_price:.4f} -> {stop_loss_price:.4f} ({self._format_line_delta(previous_stop_loss_price, stop_loss_price)})" if position.has_exit_lines else f"{stop_loss_price:.4f}"),
                    ("허용 손실", f"{previous_stop_loss_pct * Decimal('100'):.2f}% -> {stop_loss_pct * Decimal('100'):.2f}% ({self._format_line_delta(previous_stop_loss_pct * Decimal('100'), stop_loss_pct * Decimal('100'), '0.01')})" if position.has_exit_lines else f"{stop_loss_pct * Decimal('100'):.2f}%"),
                    ("허용 손실 USDT", f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, previous_stop_loss_pct):.4f} -> {self._margin_pct_to_usdt(position.margin_usdt_decimal, stop_loss_pct):.4f}" if position.has_exit_lines else f"{self._margin_pct_to_usdt(position.margin_usdt_decimal, stop_loss_pct):.4f}"),
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