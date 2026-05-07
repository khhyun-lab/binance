from __future__ import annotations

import logging

from binance_bot.config import Settings
from binance_bot.heartbeat import HeartbeatWriter
from binance_bot.services.binance_futures_service import BinanceFuturesService
from binance_bot.state import StateStore
from binance_bot.telegram import TelegramNotifier

from .entry import EntryMixin
from .exit import ExitMixin
from .regime import RegimeMixin
from .risk import RiskMixin
from .snapshot import SnapshotMixin


class StrategyEngine(SnapshotMixin, RegimeMixin, RiskMixin, EntryMixin, ExitMixin):
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