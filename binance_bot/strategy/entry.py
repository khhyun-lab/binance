from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from binance_bot.services.binance_futures_service import AccountSnapshot
from binance_bot.state import Position
from binance_bot.telegram import format_telegram_message

from .plans import PlanDecision, StrategyOrderPlan
from .snapshot import MarketSnapshot


@dataclass(slots=True)
class EntryCandidate:
    side: str
    entry_type: str
    candidate: bool = False
    ready: bool = False
    blockers: list[str] = field(default_factory=list)
    detail_reasons: list[str] = field(default_factory=list)
    score_threshold: int = 0
    score_edge: int = 0
    breakout_chase_candidate: bool = False
    pullback_reaccel_candidate: bool = False
    pullback_valid: bool = False
    reaccel_valid: bool = False
    trend_alignment_ok: bool = False
    volume_ok: bool = False
    rsi_ok: bool = False
    not_chasing_ok: bool = False
    quantity_ok: bool = False
    breakout_level: Decimal = Decimal("0")
    pullback_low: Decimal = Decimal("0")
    pullback_high: Decimal = Decimal("0")
    invalidation_deadline_ms: int = 0

    def to_metadata(self, preferred_side: str | None) -> dict[str, str | int | bool | list[str]]:
        return {
            "preferred_side": preferred_side or self.side,
            "entry_type_candidate": self.entry_type,
            "entry_blockers": list(dict.fromkeys(self.blockers)),
            "score_threshold": self.score_threshold,
            "score_edge": self.score_edge,
            "breakout_chase_candidate": self.breakout_chase_candidate,
            "pullback_reaccel_candidate": self.pullback_reaccel_candidate,
            "pullback_valid": self.pullback_valid,
            "reaccel_valid": self.reaccel_valid,
            "trend_alignment_ok": self.trend_alignment_ok,
            "volume_ok": self.volume_ok,
            "rsi_ok": self.rsi_ok,
            "not_chasing_ok": self.not_chasing_ok,
            "quantity_ok": self.quantity_ok,
            "entry_type": self.entry_type,
            "breakout_level": str(self.breakout_level),
            "pullback_low": str(self.pullback_low),
            "pullback_high": str(self.pullback_high),
            "invalidation_deadline_ms": self.invalidation_deadline_ms,
        }


class EntryMixin:
    def _breakout_extension_atr(self, snapshot: MarketSnapshot, side: str) -> Decimal:
        if snapshot.atr_3m <= 0:
            return Decimal("0")
        if side == "LONG":
            return max(Decimal("0"), (snapshot.mark_price - snapshot.breakout_high) / snapshot.atr_3m)
        return max(Decimal("0"), (snapshot.breakout_low - snapshot.mark_price) / snapshot.atr_3m)

    def _score_edge_for_side(self, snapshot: MarketSnapshot, side: str) -> int:
        if side == "LONG":
            return snapshot.long_score - snapshot.short_score
        return snapshot.short_score - snapshot.long_score

    def _entry_block_reason(self, candidate: EntryCandidate) -> str:
        blockers = set(candidate.blockers)
        if "score_below_threshold" in blockers:
            return "score_below_threshold"
        if candidate.candidate:
            return "momentum_blocked"
        return "no_signal"

    def _evaluate_breakout_chase_candidate(self, snapshot: MarketSnapshot, side: str) -> EntryCandidate:
        score_threshold = self._entry_score_threshold_for_side(side)
        score_edge = self._score_edge_for_side(snapshot, side)
        detail_reasons = list(snapshot.long_reasons if side == "LONG" else snapshot.short_reasons)
        breakout_level = snapshot.breakout_high if side == "LONG" else snapshot.breakout_low
        trend_alignment_ok = snapshot.trend_long_ok if side == "LONG" else snapshot.trend_short_ok
        breakout_crossed = snapshot.mark_price >= snapshot.breakout_high if side == "LONG" else snapshot.mark_price <= snapshot.breakout_low
        confirmation_ok = self._has_breakout_confirmation(snapshot, side)
        not_chasing_ok = not self._is_chasing_breakout(snapshot, side)
        ema_ok = (
            snapshot.ema_fast_1m > snapshot.ema_slow_1m and snapshot.mark_price >= snapshot.ema_fast_1m
            if side == "LONG"
            else snapshot.ema_fast_1m < snapshot.ema_slow_1m and snapshot.mark_price <= snapshot.ema_fast_1m
        )
        volume_ok = snapshot.volume_ratio >= max(self.settings.entry_min_volume_ratio, Decimal("1.35"))
        rsi_ok = (
            Decimal("58") <= snapshot.rsi_3m <= Decimal("69")
            if side == "LONG"
            else Decimal("31") <= snapshot.rsi_3m <= Decimal("44")
        )
        score_ok = (
            snapshot.long_score >= score_threshold + 1 and score_edge >= 2
            if side == "LONG"
            else snapshot.short_score >= score_threshold + 1 and score_edge >= 3
        )
        candidate = EntryCandidate(
            side=side,
            entry_type=f"breakout_chase_{side.lower()}",
            candidate=breakout_crossed or confirmation_ok,
            detail_reasons=detail_reasons,
            score_threshold=score_threshold,
            score_edge=score_edge,
            breakout_chase_candidate=breakout_crossed or confirmation_ok,
            trend_alignment_ok=trend_alignment_ok,
            volume_ok=volume_ok,
            rsi_ok=rsi_ok,
            not_chasing_ok=not_chasing_ok,
            breakout_level=breakout_level,
        )
        if not breakout_crossed:
            candidate.blockers.append("no_breakout")
        if not trend_alignment_ok:
            candidate.blockers.append("trend_alignment_failed")
        if not confirmation_ok:
            candidate.blockers.append("breakout_confirmation_missing")
        if not ema_ok:
            candidate.blockers.append("ema_alignment_failed")
        if not not_chasing_ok:
            candidate.blockers.append("chasing_breakout")
        if not volume_ok:
            candidate.blockers.append("volume_below_floor")
        if not rsi_ok:
            candidate.blockers.append("rsi_out_of_range")
        if not score_ok:
            candidate.blockers.append("score_below_threshold" if (snapshot.long_score if side == "LONG" else snapshot.short_score) < score_threshold + 1 else "score_edge_too_small")
        candidate.ready = candidate.candidate and trend_alignment_ok and confirmation_ok and ema_ok and not_chasing_ok and volume_ok and rsi_ok and score_ok
        return candidate

    def _find_recent_breakout_index(self, snapshot: MarketSnapshot, side: str, lookback: int) -> int | None:
        closes = snapshot.recent_closes_1m
        highs = snapshot.recent_highs_window_1m
        lows = snapshot.recent_lows_window_1m
        if len(closes) < 4:
            return None
        start = max(0, len(closes) - max(lookback, 4))
        end = max(start, len(closes) - 2)
        for idx in range(end, start - 1, -1):
            previous_close = closes[idx - 1] if idx > 0 else closes[idx]
            if side == "LONG":
                if closes[idx] >= snapshot.breakout_high and previous_close < snapshot.breakout_high:
                    return idx
            else:
                if closes[idx] <= snapshot.breakout_low and previous_close > snapshot.breakout_low:
                    return idx
        return None

    def _evaluate_pullback_reaccel_candidate(self, snapshot: MarketSnapshot, side: str) -> EntryCandidate:
        detail_reasons = list(snapshot.long_reasons if side == "LONG" else snapshot.short_reasons)
        score_threshold = max(self._entry_score_threshold_for_side(side), self.settings.pullback_reaccel_min_score)
        score_edge = self._score_edge_for_side(snapshot, side)
        breakout_level = snapshot.breakout_high if side == "LONG" else snapshot.breakout_low
        trend_alignment_ok = snapshot.trend_long_ok if side == "LONG" else snapshot.trend_short_ok
        volume_ok = snapshot.volume_ratio >= self.settings.pullback_reaccel_volume_ratio
        if side == "LONG":
            rsi_ok = self.settings.pullback_reaccel_rsi_long_min <= snapshot.rsi_3m <= self.settings.pullback_reaccel_rsi_long_max
            score_ok = snapshot.long_score >= score_threshold and score_edge >= 1
        else:
            rsi_ok = self.settings.pullback_reaccel_rsi_short_min <= snapshot.rsi_3m <= self.settings.pullback_reaccel_rsi_short_max
            score_ok = snapshot.short_score >= score_threshold and score_edge >= 1
        not_chasing_ok = not self._is_chasing_breakout(snapshot, side)
        candidate = EntryCandidate(
            side=side,
            entry_type=f"pullback_reaccel_{side.lower()}",
            detail_reasons=detail_reasons,
            score_threshold=score_threshold,
            score_edge=score_edge,
            pullback_reaccel_candidate=False,
            trend_alignment_ok=trend_alignment_ok,
            volume_ok=volume_ok,
            rsi_ok=rsi_ok,
            not_chasing_ok=not_chasing_ok,
            breakout_level=breakout_level,
        )
        if not self.settings.pullback_reaccel_enabled:
            candidate.blockers.append("pullback_disabled")
            return candidate

        lookback = min(self.settings.pullback_lookback_candles, max(0, len(snapshot.recent_closes_1m) - 1))
        breakout_idx = self._find_recent_breakout_index(snapshot, side, lookback)
        if breakout_idx is None:
            candidate.blockers.append("recent_breakout_missing")
            return candidate

        highs = snapshot.recent_highs_window_1m
        lows = snapshot.recent_lows_window_1m
        post_breakout_highs = highs[breakout_idx + 1 :]
        post_breakout_lows = lows[breakout_idx + 1 :]
        if not post_breakout_highs or not post_breakout_lows:
            candidate.blockers.append("pullback_waiting")
            candidate.candidate = True
            candidate.pullback_reaccel_candidate = True
            return candidate

        atr_buffer = snapshot.atr_3m * self.settings.pullback_retest_buffer_atr
        if side == "LONG":
            pullback_low = min(post_breakout_lows)
            pullback_idx = breakout_idx + 1 + post_breakout_lows.index(pullback_low)
            pullback_high = max(highs[pullback_idx:]) if pullback_idx < len(highs) else snapshot.latest_close_1m
            pullback_depth = breakout_level - pullback_low
            max_retest_reference = max(breakout_level + atr_buffer, snapshot.ema_fast_1m + atr_buffer, snapshot.ema_slow_1m + atr_buffer)
            pullback_valid = (
                snapshot.atr_3m > 0
                and self.settings.pullback_min_depth_atr <= (pullback_depth / snapshot.atr_3m) <= self.settings.pullback_max_depth_atr
                and pullback_low <= max_retest_reference
            )
            rebound_reference = max(highs[max(pullback_idx, len(highs) - 4) : -1], default=snapshot.previous_high_1m)
            reaccel_valid = snapshot.latest_close_1m > snapshot.previous_close_1m and snapshot.latest_close_1m >= snapshot.ema_fast_1m and snapshot.latest_close_1m > rebound_reference
        else:
            pullback_high = max(post_breakout_highs)
            pullback_idx = breakout_idx + 1 + post_breakout_highs.index(pullback_high)
            pullback_low = min(lows[pullback_idx:]) if pullback_idx < len(lows) else snapshot.latest_close_1m
            pullback_depth = pullback_high - breakout_level
            min_retest_reference = min(breakout_level - atr_buffer, snapshot.ema_fast_1m - atr_buffer, snapshot.ema_slow_1m - atr_buffer)
            pullback_valid = (
                snapshot.atr_3m > 0
                and self.settings.pullback_min_depth_atr <= (pullback_depth / snapshot.atr_3m) <= self.settings.pullback_max_depth_atr
                and pullback_high >= min_retest_reference
            )
            rebound_reference = min(lows[max(pullback_idx, len(lows) - 4) : -1], default=snapshot.previous_low_1m)
            reaccel_valid = snapshot.latest_close_1m < snapshot.previous_close_1m and snapshot.latest_close_1m <= snapshot.ema_fast_1m and snapshot.latest_close_1m < rebound_reference

        candidate.candidate = True
        candidate.pullback_reaccel_candidate = True
        candidate.pullback_valid = pullback_valid
        candidate.reaccel_valid = reaccel_valid
        candidate.pullback_low = pullback_low
        candidate.pullback_high = pullback_high
        candidate.invalidation_deadline_ms = snapshot.latest_close_time_ms + (3 * 60 * 1000) if snapshot.latest_close_time_ms > 0 else 0

        if not trend_alignment_ok:
            candidate.blockers.append("trend_alignment_failed")
        if not pullback_valid:
            candidate.blockers.append("pullback_invalid")
        if not reaccel_valid:
            candidate.blockers.append("reaccel_missing")
        if not not_chasing_ok:
            candidate.blockers.append("chasing_breakout")
        if not volume_ok:
            candidate.blockers.append("volume_below_floor")
        if not rsi_ok:
            candidate.blockers.append("rsi_out_of_range")
        if not score_ok:
            candidate.blockers.append("score_below_threshold" if (snapshot.long_score if side == "LONG" else snapshot.short_score) < score_threshold else "score_edge_too_small")
        candidate.ready = candidate.candidate and trend_alignment_ok and pullback_valid and reaccel_valid and not_chasing_ok and volume_ok and rsi_ok and score_ok
        return candidate

    def _select_entry_candidate(self, snapshot: MarketSnapshot) -> tuple[str | None, EntryCandidate | None]:
        preferred_side = snapshot.preferred_side
        if preferred_side is None:
            return None, None
        breakout_candidate = self._evaluate_breakout_chase_candidate(snapshot, preferred_side)
        pullback_candidate = self._evaluate_pullback_reaccel_candidate(snapshot, preferred_side)
        if breakout_candidate.ready:
            return preferred_side, breakout_candidate
        if pullback_candidate.ready:
            return preferred_side, pullback_candidate
        if pullback_candidate.candidate:
            return preferred_side, pullback_candidate
        return preferred_side, breakout_candidate

    def _validate_pullback_reward_risk(self, snapshot: MarketSnapshot, side: str, entry_price: Decimal, candidate: EntryCandidate) -> bool:
        _, stop_loss_price, _, _ = self._calculate_exit_lines(
            snapshot,
            side,
            entry_price,
            self.settings.leverage,
            entry_metadata=candidate.to_metadata(side),
        )
        if side == "LONG":
            risk_distance = entry_price - stop_loss_price
            natural_reward = max(snapshot.recent_high - entry_price, snapshot.breakout_high - entry_price, snapshot.atr_3m * self.settings.pullback_target_atr_multiplier)
        else:
            risk_distance = stop_loss_price - entry_price
            natural_reward = max(entry_price - snapshot.recent_low, entry_price - snapshot.breakout_low, snapshot.atr_3m * self.settings.pullback_target_atr_multiplier)
        if risk_distance <= 0:
            return False
        return (natural_reward / risk_distance) >= self.settings.pullback_min_rr

    def _is_chasing_breakout(self, snapshot: MarketSnapshot, side: str) -> bool:
        max_extension = snapshot.atr_3m * Decimal("0.35")
        if side == "LONG":
            return snapshot.mark_price > snapshot.breakout_high + max_extension
        return snapshot.mark_price < snapshot.breakout_low - max_extension

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
        return None

    def _get_rebound_long_signal(self, snapshot: MarketSnapshot) -> tuple[str, list[str]] | None:
        return None

    def _has_breakout_confirmation(self, snapshot: MarketSnapshot, side: str) -> bool:
        if side == "LONG":
            high1, high2, high3 = snapshot.recent_three_highs_1m
            return (
                snapshot.previous_close_1m >= snapshot.breakout_high
                and snapshot.latest_close_1m >= snapshot.breakout_high
                and snapshot.previous_low_1m >= snapshot.ema_slow_1m
                and high1 < high2 < high3
            )

        low1, low2, low3 = snapshot.recent_three_lows_1m
        return (
            snapshot.previous_close_1m <= snapshot.breakout_low
            and snapshot.latest_close_1m <= snapshot.breakout_low
            and low1 > low2 > low3
        )

    def _is_mtf_breakout_ready(self, snapshot: MarketSnapshot, side: str) -> bool:
        if side == "LONG":
            return (
                snapshot.trend_long_ok
                and snapshot.mark_price >= snapshot.breakout_high
                and not self._is_chasing_breakout(snapshot, "LONG")
                and self._has_breakout_confirmation(snapshot, "LONG")
                and snapshot.ema_fast_1m > snapshot.ema_slow_1m
                and snapshot.mark_price >= snapshot.ema_fast_1m
                and Decimal("58") <= snapshot.rsi_3m <= Decimal("69")
                and snapshot.volume_ratio >= max(self.settings.entry_min_volume_ratio, Decimal("1.35"))
            )
        return (
            snapshot.trend_short_ok
            and snapshot.mark_price <= snapshot.breakout_low
            and not self._is_chasing_breakout(snapshot, "SHORT")
            and self._has_breakout_confirmation(snapshot, "SHORT")
            and snapshot.ema_fast_1m < snapshot.ema_slow_1m
            and snapshot.mark_price <= snapshot.ema_fast_1m
            and Decimal("31") <= snapshot.rsi_3m <= Decimal("44")
            and snapshot.volume_ratio >= max(self.settings.entry_min_volume_ratio, Decimal("1.35"))
        )

    def _post_filter_breakout_candidate(self, snapshot: MarketSnapshot, candidate: EntryCandidate) -> str | None:
        if not candidate.entry_type.startswith("breakout_chase"):
            return None
        if candidate.side == "SHORT" and not self.settings.breakout_short_enabled:
            return "breakout_side_disabled"
        if candidate.side == "LONG" and snapshot.rsi_3m > self.settings.long_breakout_max_rsi_3m:
            return "breakout_long_rsi_too_high"
        if candidate.side == "LONG" and self._breakout_extension_atr(snapshot, "LONG") > self.settings.breakout_max_extension_atr:
            return "breakout_overextended"
        return None

    def _has_entry_momentum(self, snapshot: MarketSnapshot, side: str) -> bool:
        threshold = self._entry_score_threshold_for_side(side)
        if side == "LONG":
            conditions = [
                self._is_mtf_breakout_ready(snapshot, "LONG"),
                snapshot.rsi_3m >= self.settings.long_entry_min_rsi_3m,
                snapshot.long_score >= threshold + 1,
            ]
            score_edge_ok = snapshot.long_score >= snapshot.short_score + 2
            return score_edge_ok and sum(1 for condition in conditions if condition) >= self.settings.entry_momentum_min_conditions

        conditions = [
            self._is_mtf_breakout_ready(snapshot, "SHORT"),
            snapshot.rsi_3m <= self.settings.short_entry_max_rsi_3m,
            snapshot.short_score >= threshold + 1,
        ]
        score_edge_ok = snapshot.short_score >= snapshot.long_score + 3
        return score_edge_ok and sum(1 for condition in conditions if condition) >= self.settings.entry_momentum_min_conditions

    def _is_exhausted_entry(self, snapshot: MarketSnapshot, side: str) -> bool:
        if side == "LONG":
            return snapshot.rsi_1m >= Decimal("74") or snapshot.rsi_3m >= Decimal("70") or (
                snapshot.rsi_3m >= Decimal("68")
                and snapshot.volume_ratio >= Decimal("2.00")
                and snapshot.mark_price >= snapshot.breakout_high
            )
        return snapshot.rsi_1m <= Decimal("28") or snapshot.rsi_3m <= Decimal("30")

    def _can_scale_in_position(self, snapshot: MarketSnapshot, position: Position) -> bool:
        if position.side == "LONG":
            price_ok = snapshot.mark_price >= position.entry_price_decimal
            trend_ok = self._is_strong_long_trend(snapshot)
            exhaustion_ok = snapshot.rsi_3m <= Decimal("68") and snapshot.volume_ratio <= Decimal("2.50")
        else:
            price_ok = snapshot.mark_price <= position.entry_price_decimal
            trend_ok = self._is_mtf_breakout_ready(snapshot, "SHORT") and snapshot.short_score >= self._entry_score_threshold_for_side("SHORT") + 1
            exhaustion_ok = Decimal("30") <= snapshot.rsi_3m <= Decimal("46") and snapshot.rsi_1m >= Decimal("30")
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
        preferred_side, selected_candidate = self._select_entry_candidate(snapshot)
        if preferred_side is None or selected_candidate is None:
            return PlanDecision(allowed=False, reason="no_signal", metadata={"preferred_side": "NONE", "entry_type_candidate": "none", "entry_blockers": []})

        if len(self.state_store.positions) >= self.settings.max_open_positions:
            selected_candidate.blockers.append("max_open_positions")
            return PlanDecision(allowed=False, reason="max_open_positions", detail_reasons=selected_candidate.detail_reasons, metadata=selected_candidate.to_metadata(preferred_side))

        if self._is_sideways_regime(snapshot):
            selected_candidate.blockers.append("sideways_blocked")
            return PlanDecision(allowed=False, reason="sideways_blocked", detail_reasons=selected_candidate.detail_reasons, metadata=selected_candidate.to_metadata(preferred_side))

        preferred_score = snapshot.long_score if preferred_side == "LONG" else snapshot.short_score
        score_display = f"{preferred_score}/{selected_candidate.score_threshold}"
        if self._is_exhausted_entry(snapshot, preferred_side):
            selected_candidate.blockers.append("exhausted_entry")
            return PlanDecision(allowed=False, reason="exhausted_entry", detail_reasons=selected_candidate.detail_reasons, metadata=selected_candidate.to_metadata(preferred_side))
        if not selected_candidate.ready:
            return PlanDecision(
                allowed=False,
                reason=self._entry_block_reason(selected_candidate),
                detail_reasons=selected_candidate.detail_reasons,
                metadata=selected_candidate.to_metadata(preferred_side),
            )
        breakout_post_filter_reason = self._post_filter_breakout_candidate(snapshot, selected_candidate)
        if breakout_post_filter_reason is not None:
            selected_candidate.blockers.append(breakout_post_filter_reason)
            return PlanDecision(
                allowed=False,
                reason=breakout_post_filter_reason,
                detail_reasons=selected_candidate.detail_reasons,
                metadata=selected_candidate.to_metadata(preferred_side),
            )

        available_margin, quantity, effective_remaining_splits = await self._resolve_entry_order_plan(
            snapshot.symbol,
            snapshot.mark_price,
            account_snapshot.available_balance,
            self.settings.leverage,
            None,
        )
        selected_candidate.quantity_ok = quantity > 0
        if quantity <= 0:
            selected_candidate.blockers.append("quantity_normalized_to_zero")
            return PlanDecision(allowed=False, reason="quantity_normalized_to_zero", detail_reasons=selected_candidate.detail_reasons, metadata=selected_candidate.to_metadata(preferred_side))

        if selected_candidate.entry_type.startswith("pullback_reaccel") and not self._validate_pullback_reward_risk(snapshot, preferred_side, snapshot.mark_price, selected_candidate):
            selected_candidate.blockers.append("pullback_min_rr_blocked")
            return PlanDecision(allowed=False, reason="pullback_min_rr_blocked", detail_reasons=selected_candidate.detail_reasons, metadata=selected_candidate.to_metadata(preferred_side))

        return PlanDecision(
            allowed=True,
            reason=selected_candidate.entry_type,
            detail_reasons=selected_candidate.detail_reasons,
            plan=StrategyOrderPlan(
                kind="enter",
                symbol=snapshot.symbol,
                position_side=preferred_side,
                order_side="BUY" if preferred_side == "LONG" else "SELL",
                quantity=quantity,
                margin_usdt=available_margin,
                reason=selected_candidate.entry_type,
                detail_reasons=selected_candidate.detail_reasons,
                score_display=score_display,
                metadata=selected_candidate.to_metadata(preferred_side) | {"effective_remaining_splits": effective_remaining_splits},
            ),
            metadata=selected_candidate.to_metadata(preferred_side),
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
            entry_type=position.entry_type,
            breakout_level=position.breakout_level_decimal,
            pullback_low=position.pullback_low_decimal,
            pullback_high=position.pullback_high_decimal,
            invalidation_deadline_ms=position.invalidation_deadline_ms,
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
            blockers = decision.metadata.get("entry_blockers", [])
            blocker_text = ",".join(blockers) if isinstance(blockers, list) else ""
            self.logger.info(
                "[BINANCE HOLD] symbol=%s reason=%s candidate=%s blockers=%s",
                snapshot.symbol,
                decision.reason,
                decision.metadata.get("entry_type_candidate", "none"),
                blocker_text,
            )
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

        take_profit_price, stop_loss_price, take_profit_pct, stop_loss_pct = self._calculate_exit_lines(
            snapshot,
            plan.position_side,
            execution.avg_price,
            self.settings.leverage,
            entry_metadata=plan.metadata,
        )
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
            entry_type=str(plan.metadata.get("entry_type", plan.reason)),
            breakout_level=Decimal(str(plan.metadata.get("breakout_level", "0"))),
            pullback_low=Decimal(str(plan.metadata.get("pullback_low", "0"))),
            pullback_high=Decimal(str(plan.metadata.get("pullback_high", "0"))),
            invalidation_deadline_ms=int(plan.metadata.get("invalidation_deadline_ms", 0)),
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
                    ("방향", plan.position_side),
                    ("진입 타입", plan.reason),
                    ("분할", f"{position.entry_count}/{max(self.settings.entry_splits, int(plan.metadata.get('effective_remaining_splits', self.settings.entry_splits)))}"),
                    ("사용 증거금", f"{plan.margin_usdt:.2f} USDT"),
                    ("점수", plan.score_display),
                    ("레버리지", f"{self.settings.leverage}배"),
                    ("수량", execution.executed_qty),
                    ("체결가", f"{execution.avg_price:.4f}"),
                    ("익절 라인", f"{take_profit_price:.4f}"),
                    ("순익 목표", f"{take_profit_pct * Decimal('100'):.2f}%"),
                    ("손절 라인", f"{stop_loss_price:.4f}"),
                    ("허용 손실", f"{stop_loss_pct * Decimal('100'):.2f}%"),
                    ("dry_run", self.settings.dry_run),
                ],
                sections=[("진입 근거", plan.detail_reasons or ["none"])],
            )
        )