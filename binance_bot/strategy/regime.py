from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from binance_bot.telegram import format_telegram_message

from .snapshot import MarketSnapshot


class RegimeMixin:
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