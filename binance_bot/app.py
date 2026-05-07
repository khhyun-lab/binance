from __future__ import annotations

import asyncio
import logging
import signal
from decimal import Decimal

from binance_bot.config import Settings
from binance_bot.heartbeat import HeartbeatWriter
from binance_bot.logging_utils import LOG_SCHEMA_VERSION
from binance_bot.market_data import LiveBinanceMarketDataProvider
from binance_bot.services.binance_futures_service import BinanceFuturesService
from binance_bot.state import StateStore
from binance_bot.strategy import StrategyEngine
from binance_bot.telegram import TelegramNotifier, format_telegram_message


class BinanceFuturesApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(self.__class__.__name__)
        self.stop_event = asyncio.Event()
        self.heartbeat = HeartbeatWriter(settings.heartbeat_file)
        self.state_store = StateStore(settings.state_file)
        self.notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

    def _resolve_margin_usdt(self, available_balance: Decimal) -> Decimal:
        if self.settings.use_available_balance:
            return available_balance
        return min(available_balance, self.settings.margin_per_trade_usdt)

    async def _apply_live_fee_rate(self, service: BinanceFuturesService) -> None:
        if not self.settings.api_key or not self.settings.api_secret:
            return

        taker_rates: list[Decimal] = []
        for symbol in self.settings.symbols:
            commission_rate = await service.get_commission_rate(symbol)
            taker_rates.append(commission_rate.taker_commission_rate)

        if not taker_rates:
            return

        live_round_trip_fee_pct = (max(taker_rates) * Decimal("2")).quantize(Decimal("0.0000001"))
        if live_round_trip_fee_pct == self.settings.round_trip_fee_pct:
            self.logger.info(
                "Binance 실수수료 동기화 유지 round_trip_fee_pct=%s",
                live_round_trip_fee_pct,
            )
            return

        previous_fee_pct = self.settings.round_trip_fee_pct
        self.settings.round_trip_fee_pct = live_round_trip_fee_pct
        self.logger.info(
            "Binance 실수수료 동기화 round_trip_fee_pct=%s -> %s",
            previous_fee_pct,
            live_round_trip_fee_pct,
        )

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass

    async def run_preflight(self) -> None:
        self._register_signal_handlers()
        self.heartbeat.touch("preflight")
        self.logger.info(
            "Binance 선물 사전점검 시작 symbols=%s leverage=%s margin_per_trade=%s use_available_balance=%s entry_splits=%s exit_splits=%s",
            ",".join(self.settings.symbols),
            self.settings.leverage,
            self.settings.margin_per_trade_usdt,
            self.settings.use_available_balance,
            self.settings.entry_splits,
            self.settings.exit_splits,
        )

        async with BinanceFuturesService(
            api_key=self.settings.api_key,
            api_secret=self.settings.api_secret,
            testnet=self.settings.testnet,
            recv_window_ms=self.settings.recv_window_ms,
        ) as service:
            await self._apply_live_fee_rate(service)
            account = await service.get_account_snapshot()
            account_config = await service.get_account_config()
            api_restrictions = await service.get_api_restrictions()
            cross_margin = await service.get_cross_margin_snapshot()
            fee_burn_status = await service.get_fee_burn_status()
            positions = await service.get_position_risks(self.settings.symbols)
            lines = [
                "[Binance 사전점검] 성공",
                f"symbols={', '.join(self.settings.symbols)}",
                f"available_balance={account.available_balance:.2f} USDT",
                f"wallet_balance={account.wallet_balance:.2f} USDT",
                f"max_withdraw={account.max_withdraw_amount:.2f} USDT",
                f"use_available_balance={self.settings.use_available_balance}",
                f"entry_splits={self.settings.entry_splits}",
                f"exit_splits={self.settings.exit_splits}",
                f"open_positions={len(positions)}",
                f"futures_can_trade={account_config.can_trade}",
                f"futures_can_deposit={account_config.can_deposit}",
                f"futures_can_withdraw={account_config.can_withdraw}",
                f"api_enable_futures={api_restrictions.enable_futures}",
                f"api_enable_margin={api_restrictions.enable_margin}",
                f"api_enable_spot_margin={api_restrictions.enable_spot_and_margin_trading}",
                f"api_ip_restrict={api_restrictions.ip_restrict}",
                f"api_universal_transfer={api_restrictions.permits_universal_transfer}",
                f"cross_margin_usdt_free={cross_margin.usdt_free:.2f}",
                f"cross_margin_usdt_net_asset={cross_margin.usdt_net_asset:.2f}",
                f"um_fee_burn_enabled={fee_burn_status.fee_burn}",
                f"round_trip_fee_pct={self.settings.round_trip_fee_pct}",
            ]

            if account.available_balance <= Decimal("0") and cross_margin.usdt_net_asset > Decimal("0"):
                lines.append("진단=자금이 USD-M 선물지갑이 아니라 cross margin 지갑에 있습니다")
            if not account_config.can_trade:
                lines.append("진단=USD-M futures 계정이 아직 활성 상태가 아니어서 선물 주문이 차단됩니다")

            for symbol in self.settings.symbols:
                commission_rate = await service.get_commission_rate(symbol)
                ask_price, bid_price = await service.get_book_ticker(symbol)
                entry_price = ask_price
                quantity = await service.calculate_order_quantity(
                    symbol=symbol,
                    price=entry_price,
                    margin_usdt=self._resolve_margin_usdt(account.available_balance),
                    leverage=self.settings.leverage,
                )
                if quantity <= 0:
                    lines.append(f"{symbol}: 주문수량 계산 실패")
                    continue

                try:
                    await service.set_leverage(symbol, self.settings.leverage)
                    leverage_result = f"leverage={self.settings.leverage} 적용성공"
                except Exception as exc:
                    leverage_result = f"leverage적용실패={exc}"

                try:
                    await service.create_test_order(symbol=symbol, side="BUY", quantity=quantity, price=ask_price)
                    await service.create_test_order(symbol=symbol, side="SELL", quantity=quantity, price=bid_price)
                    order_result = "BUY/SELL 테스트주문성공"
                except Exception as exc:
                    order_result = f"테스트주문실패={exc}"

                lines.append(
                    f"{symbol}: {leverage_result} {order_result} quantity={quantity} ask={ask_price} bid={bid_price} maker_fee={commission_rate.maker_commission_rate} taker_fee={commission_rate.taker_commission_rate}"
                )

            message = format_telegram_message(
                "[Binance 사전점검]",
                fields=[
                    ("심볼", ", ".join(self.settings.symbols)),
                    ("레버리지", f"{self.settings.leverage}배"),
                    ("가용 잔고", f"{account.available_balance:.2f} USDT"),
                    ("지갑 잔고", f"{account.wallet_balance:.2f} USDT"),
                    ("출금 가능", f"{account.max_withdraw_amount:.2f} USDT"),
                    ("가용잔고 사용", self.settings.use_available_balance),
                    ("진입 분할", self.settings.entry_splits),
                    ("청산 분할", self.settings.exit_splits),
                    ("보유 포지션", len(positions)),
                    ("선물 주문 가능", account_config.can_trade),
                    ("API 선물 권한", api_restrictions.enable_futures),
                    ("API 마진 권한", api_restrictions.enable_margin),
                    ("교차마진 USDT", f"{cross_margin.usdt_net_asset:.2f}"),
                    ("수수료 BNB 차감", fee_burn_status.fee_burn),
                ],
                sections=[("심볼별 점검", lines[18:])],
            )
            self.logger.info(message)
            await self.notifier.send(message)
        self.heartbeat.touch("preflight-complete")

    async def run(self, run_once: bool = False) -> None:
        self._register_signal_handlers()
        self.state_store.load()
        if self.settings.dry_run and self.state_store.positions:
            self.logger.info("Binance dry-run 상태파일 초기화 existing_positions=%s", len(self.state_store.positions))
            self.state_store.positions = {}
            self.state_store.save()
        self.heartbeat.touch("starting")
        self.logger.info(
            "Binance 선물 봇 시작 log_schema_version=%s symbols=%s dry_run=%s leverage=%s cycle_seconds=%s exit_monitor_seconds=%s heartbeat_file=%s",
            LOG_SCHEMA_VERSION,
            ",".join(self.settings.symbols),
            self.settings.dry_run,
            self.settings.leverage,
            self.settings.cycle_seconds,
            self.settings.exit_monitor_seconds,
            self.settings.heartbeat_file,
        )
        await self.notifier.send(
            format_telegram_message(
                "[Binance 시작]",
                fields=[
                    ("심볼", ", ".join(self.settings.symbols)),
                    ("신규 진입 레버리지", f"{self.settings.leverage}배"),
                    ("실행 주기", f"{self.settings.cycle_seconds}초"),
                    ("청산 감시 주기", f"{self.settings.exit_monitor_seconds}초"),
                    ("라인 갱신 주기", f"{self.settings.exit_line_refresh_seconds}초"),
                    ("dry_run", self.settings.dry_run),
                ],
                sections=[("운용 포인트", [f"손/익절 라인 실시간 재설정: {self.settings.exit_line_refresh_seconds}초", f"보유 포지션 청산 감시: {self.settings.exit_monitor_seconds}초"] )],
            )
        )

        async with BinanceFuturesService(
            api_key=self.settings.api_key,
            api_secret=self.settings.api_secret,
            testnet=self.settings.testnet,
            recv_window_ms=self.settings.recv_window_ms,
        ) as service:
            await self._apply_live_fee_rate(service)
            strategy = StrategyEngine(
                settings=self.settings,
                service=LiveBinanceMarketDataProvider(service),
                state_store=self.state_store,
                notifier=self.notifier,
                heartbeat=self.heartbeat,
            )
            try:
                if run_once:
                    await strategy.run_cycle()
                    return
                while not self.stop_event.is_set():
                    try:
                        await strategy.run_cycle()
                    except Exception as exc:
                        self.heartbeat.touch("cycle-error")
                        self.logger.exception("Binance 선물 사이클 예외로 계속 진행합니다: %s", exc)
                        if self.stop_event.is_set():
                            break
                        await asyncio.sleep(min(5, max(1, self.settings.exit_monitor_seconds)))
                        continue
                    wait_elapsed = 0
                    self.logger.info("Binance 다음 사이클까지 대기 seconds=%s", self.settings.cycle_seconds)
                    while wait_elapsed < self.settings.cycle_seconds and not self.stop_event.is_set():
                        wait_step = self.settings.cycle_seconds - wait_elapsed
                        if 0 < self.settings.exit_monitor_seconds < wait_step:
                            wait_step = self.settings.exit_monitor_seconds
                        try:
                            await asyncio.wait_for(self.stop_event.wait(), timeout=wait_step)
                            break
                        except asyncio.TimeoutError:
                            wait_elapsed += wait_step
                            if (
                                wait_elapsed < self.settings.cycle_seconds
                                and self.settings.exit_monitor_seconds > 0
                                and self.state_store.positions
                            ):
                                try:
                                    await strategy.run_exit_monitor_cycle()
                                except Exception as exc:
                                    self.heartbeat.touch("exit-monitor-error")
                                    self.logger.exception("Binance 청산 감시 예외로 계속 진행합니다: %s", exc)
                                    break
            finally:
                self.heartbeat.touch("stopped")
                self.logger.info("Binance 선물 봇 종료")
                await self.notifier.send(format_telegram_message("[Binance 종료]"))