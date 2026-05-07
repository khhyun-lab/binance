from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import aiohttp


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _normalize_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(slots=True)
class SymbolRules:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(slots=True)
class OrderExecution:
    order_id: str
    status: str
    side: str
    price: Decimal
    avg_price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal


@dataclass(slots=True)
class AccountSnapshot:
    available_balance: Decimal
    wallet_balance: Decimal
    max_withdraw_amount: Decimal = Decimal("0")


@dataclass(slots=True)
class FuturesAccountConfig:
    can_trade: bool
    can_deposit: bool
    can_withdraw: bool
    dual_side_position: bool
    multi_assets_margin: bool


@dataclass(slots=True)
class ApiRestrictions:
    enable_reading: bool
    enable_futures: bool
    enable_spot_and_margin_trading: bool
    enable_margin: bool
    ip_restrict: bool
    permits_universal_transfer: bool


@dataclass(slots=True)
class CrossMarginSnapshot:
    trade_enabled: bool
    transfer_enabled: bool
    borrow_enabled: bool
    usdt_free: Decimal
    usdt_net_asset: Decimal


@dataclass(slots=True)
class FuturesFeeBurnStatus:
    fee_burn: bool


@dataclass(slots=True)
class FuturesCommissionRate:
    symbol: str
    maker_commission_rate: Decimal
    taker_commission_rate: Decimal


@dataclass(slots=True)
class UserTrade:
    symbol: str
    trade_id: int
    order_id: str
    side: str
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    trade_time_ms: int
    maker: bool
    buyer: bool


@dataclass(slots=True)
class PositionRisk:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: int

    @property
    def side(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT"


class BinanceFuturesService:
    def __init__(self, api_key: str, api_secret: str, testnet: bool, recv_window_ms: int) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8") if api_secret else b""
        self.testnet = testnet
        self.recv_window_ms = recv_window_ms
        self.base_url = "https://demo-fapi.binance.com" if testnet else "https://fapi.binance.com"
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session: aiohttp.ClientSession | None = None
        self._server_time_offset_ms = 0
        self._exchange_info_cache: dict[str, SymbolRules] = {}
        self._rest_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._rest_interval_seconds = 0.2

    async def __aenter__(self) -> BinanceFuturesService:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        await self.sync_time()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            await self.session.close()

    def _require_session(self) -> aiohttp.ClientSession:
        if self.session is None:
            raise RuntimeError("Binance 세션이 초기화되지 않았습니다.")
        return self.session

    async def _throttle(self) -> None:
        async with self._rest_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < self._rest_interval_seconds:
                await asyncio.sleep(self._rest_interval_seconds - elapsed)
            self._last_request_at = time.monotonic()

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        return await self._request_to_base(self.base_url, method, path, params=params, signed=signed)

    async def _request_to_base(
        self,
        base_url: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        session = self._require_session()
        await self._throttle()
        params = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            if not self.api_key or not self.api_secret:
                raise RuntimeError("Binance API 키가 필요합니다.")
            params["timestamp"] = int(time.time() * 1000) + self._server_time_offset_ms
            params["recvWindow"] = self.recv_window_ms
            query = urlencode(params, doseq=True)
            signature = hmac.new(self.api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
            params["signature"] = signature
            headers["X-MBX-APIKEY"] = self.api_key
        elif self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        async with session.request(method, f"{base_url}{path}", params=params, headers=headers) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"Binance API 오류 status={response.status} body={data}")
            return data

    async def sync_time(self) -> None:
        data = await self._request("GET", "/fapi/v1/time")
        server_time = int(data.get("serverTime", 0))
        self._server_time_offset_ms = server_time - int(time.time() * 1000)

    async def get_exchange_info(self) -> dict[str, SymbolRules]:
        if self._exchange_info_cache:
            return dict(self._exchange_info_cache)
        payload = await self._request("GET", "/fapi/v1/exchangeInfo")
        rules: dict[str, SymbolRules] = {}
        for symbol_payload in payload.get("symbols", []):
            if symbol_payload.get("contractType") != "PERPETUAL":
                continue
            if symbol_payload.get("status") != "TRADING":
                continue
            symbol = str(symbol_payload.get("symbol") or "")
            filters = {item.get("filterType"): item for item in symbol_payload.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            min_notional_filter = filters.get("MIN_NOTIONAL", {})
            rules[symbol] = SymbolRules(
                symbol=symbol,
                tick_size=_decimal(price_filter.get("tickSize"), "0.01"),
                step_size=_decimal(lot_filter.get("stepSize"), "0.001"),
                min_qty=_decimal(lot_filter.get("minQty"), "0"),
                min_notional=_decimal(min_notional_filter.get("notional"), "5"),
            )
        self._exchange_info_cache = rules
        return dict(self._exchange_info_cache)

    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        rules = await self.get_exchange_info()
        if symbol not in rules:
            raise ValueError(f"Binance 선물 심볼을 찾을 수 없습니다: {symbol}")
        return rules[symbol]

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
        rows: list[dict[str, Any]] = []
        for item in payload:
            rows.append(
                {
                    "open_time": item[0],
                    "open": _decimal(item[1]),
                    "high": _decimal(item[2]),
                    "low": _decimal(item[3]),
                    "close": _decimal(item[4]),
                    "volume": _decimal(item[5]),
                }
            )
        return rows

    async def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        payload = await self._request("GET", "/fapi/v1/ticker/bookTicker", params={"symbol": symbol})
        return _decimal(payload.get("askPrice")), _decimal(payload.get("bidPrice"))

    async def get_mark_price(self, symbol: str) -> Decimal:
        payload = await self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})
        return _decimal(payload.get("markPrice"))

    async def get_account_snapshot(self) -> AccountSnapshot:
        payload = await self._request("GET", "/fapi/v3/balance", signed=True)
        usdt_row = next((row for row in payload if str(row.get("asset")) == "USDT"), None)
        if usdt_row is None:
            return AccountSnapshot(available_balance=Decimal("0"), wallet_balance=Decimal("0"))
        return AccountSnapshot(
            available_balance=_decimal(usdt_row.get("availableBalance")),
            wallet_balance=_decimal(usdt_row.get("balance")),
            max_withdraw_amount=_decimal(usdt_row.get("maxWithdrawAmount")),
        )

    async def get_account_config(self) -> FuturesAccountConfig:
        payload = await self._request("GET", "/fapi/v1/accountConfig", signed=True)
        return FuturesAccountConfig(
            can_trade=bool(payload.get("canTrade", False)),
            can_deposit=bool(payload.get("canDeposit", False)),
            can_withdraw=bool(payload.get("canWithdraw", False)),
            dual_side_position=bool(payload.get("dualSidePosition", False)),
            multi_assets_margin=bool(payload.get("multiAssetsMargin", False)),
        )

    async def get_api_restrictions(self) -> ApiRestrictions:
        payload = await self._request_to_base(
            "https://api.binance.com",
            "GET",
            "/sapi/v1/account/apiRestrictions",
            signed=True,
        )
        return ApiRestrictions(
            enable_reading=bool(payload.get("enableReading", False)),
            enable_futures=bool(payload.get("enableFutures", False)),
            enable_spot_and_margin_trading=bool(payload.get("enableSpotAndMarginTrading", False)),
            enable_margin=bool(payload.get("enableMargin", False)),
            ip_restrict=bool(payload.get("ipRestrict", False)),
            permits_universal_transfer=bool(payload.get("permitsUniversalTransfer", False)),
        )

    async def get_cross_margin_snapshot(self) -> CrossMarginSnapshot:
        payload = await self._request_to_base(
            "https://api.binance.com",
            "GET",
            "/sapi/v1/margin/account",
            signed=True,
        )
        usdt_row = next((row for row in payload.get("userAssets", []) if str(row.get("asset")) == "USDT"), None)
        return CrossMarginSnapshot(
            trade_enabled=bool(payload.get("tradeEnabled", False)),
            transfer_enabled=bool(payload.get("transferEnabled", False)),
            borrow_enabled=bool(payload.get("borrowEnabled", False)),
            usdt_free=_decimal((usdt_row or {}).get("free")),
            usdt_net_asset=_decimal((usdt_row or {}).get("netAsset")),
        )

    async def get_fee_burn_status(self) -> FuturesFeeBurnStatus:
        payload = await self._request("GET", "/fapi/v1/feeBurn", signed=True)
        return FuturesFeeBurnStatus(fee_burn=bool(payload.get("feeBurn", False)))

    async def get_commission_rate(self, symbol: str) -> FuturesCommissionRate:
        payload = await self._request("GET", "/fapi/v1/commissionRate", params={"symbol": symbol}, signed=True)
        return FuturesCommissionRate(
            symbol=str(payload.get("symbol") or symbol),
            maker_commission_rate=_decimal(payload.get("makerCommissionRate")),
            taker_commission_rate=_decimal(payload.get("takerCommissionRate")),
        )

    async def get_user_trades(self, symbol: str, start_time_ms: int | None = None, limit: int = 200) -> list[UserTrade]:
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        payload = await self._request("GET", "/fapi/v1/userTrades", params=params, signed=True)
        trades: list[UserTrade] = []
        for row in payload:
            trades.append(
                UserTrade(
                    symbol=str(row.get("symbol") or symbol),
                    trade_id=int(row.get("id") or 0),
                    order_id=str(row.get("orderId") or "0"),
                    side=str(row.get("side") or ""),
                    price=_decimal(row.get("price")),
                    quantity=_decimal(row.get("qty")),
                    quote_quantity=_decimal(row.get("quoteQty")),
                    realized_pnl=_decimal(row.get("realizedPnl")),
                    commission=_decimal(row.get("commission")),
                    commission_asset=str(row.get("commissionAsset") or ""),
                    trade_time_ms=int(row.get("time") or 0),
                    maker=bool(row.get("maker", False)),
                    buyer=bool(row.get("buyer", False)),
                )
            )
        return trades

    async def get_position_risks(self, symbols: list[str]) -> list[PositionRisk]:
        payload = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        symbol_set = set(symbols)
        positions: list[PositionRisk] = []
        for row in payload:
            symbol = str(row.get("symbol") or "")
            if symbol not in symbol_set:
                continue
            quantity = _decimal(row.get("positionAmt"))
            if quantity == 0:
                continue
            positions.append(
                PositionRisk(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=_decimal(row.get("entryPrice")),
                    mark_price=_decimal(row.get("markPrice")),
                    leverage=int(row.get("leverage") or 1),
                )
            )
        return positions

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request("POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": leverage}, signed=True)

    async def create_test_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal | None = None,
        reduce_only: bool = False,
    ) -> Any:
        rules = await self.get_symbol_rules(symbol)
        normalized_quantity = self.normalize_quantity(rules, quantity)
        if normalized_quantity <= 0:
            raise ValueError("테스트 주문 수량이 0 이하입니다.")

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET" if price is None else "LIMIT",
            "quantity": _normalize_decimal(normalized_quantity),
            "newOrderRespType": "RESULT",
            "reduceOnly": "true" if reduce_only else "false",
        }
        if price is not None:
            normalized_price = self.normalize_price(rules, price, side)
            params["timeInForce"] = "IOC"
            params["price"] = _normalize_decimal(normalized_price)

        return await self._request("POST", "/fapi/v1/order/test", params=params, signed=True)

    def normalize_price(self, rules: SymbolRules, price: Decimal, side: str) -> Decimal:
        step = rules.tick_size
        if step <= 0:
            return price
        units = price / step
        rounding = ROUND_CEILING if side == "BUY" else ROUND_DOWN
        return (units.quantize(Decimal("1"), rounding=rounding) * step).quantize(step)

    def normalize_quantity(self, rules: SymbolRules, quantity: Decimal) -> Decimal:
        step = rules.step_size
        if step <= 0:
            return quantity
        units = quantity / step
        normalized = (units.quantize(Decimal("1"), rounding=ROUND_DOWN) * step).quantize(step)
        return normalized

    async def calculate_order_quantity(self, symbol: str, price: Decimal, margin_usdt: Decimal, leverage: int) -> Decimal:
        rules = await self.get_symbol_rules(symbol)
        notional = margin_usdt * Decimal(leverage)
        quantity = self.normalize_quantity(rules, notional / price)
        if quantity < rules.min_qty:
            return Decimal("0")
        if quantity * price < rules.min_notional:
            return Decimal("0")
        return quantity

    async def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        reduce_only: bool,
        dry_run: bool,
    ) -> OrderExecution | None:
        rules = await self.get_symbol_rules(symbol)
        normalized_price = self.normalize_price(rules, price, side)
        normalized_quantity = self.normalize_quantity(rules, quantity)
        if normalized_quantity <= 0:
            return None

        if dry_run:
            return OrderExecution(
                order_id=f"dry-run-{symbol}-{side}-{int(time.time())}",
                status="FILLED",
                side=side,
                price=normalized_price,
                avg_price=normalized_price,
                orig_qty=normalized_quantity,
                executed_qty=normalized_quantity,
            )

        payload = await self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": side,
                "type": "LIMIT",
                "timeInForce": "IOC",
                "quantity": _normalize_decimal(normalized_quantity),
                "price": _normalize_decimal(normalized_price),
                "newOrderRespType": "RESULT",
                "reduceOnly": "true" if reduce_only else "false",
            },
            signed=True,
        )
        return OrderExecution(
            order_id=str(payload.get("orderId") or payload.get("clientOrderId") or "unknown"),
            status=str(payload.get("status") or "UNKNOWN"),
            side=str(payload.get("side") or side),
            price=_decimal(payload.get("price"), _normalize_decimal(normalized_price)),
            avg_price=_decimal(payload.get("avgPrice"), _normalize_decimal(normalized_price)),
            orig_qty=_decimal(payload.get("origQty"), _normalize_decimal(normalized_quantity)),
            executed_qty=_decimal(payload.get("executedQty"), "0"),
        )

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        reduce_only: bool,
        dry_run: bool,
    ) -> OrderExecution | None:
        rules = await self.get_symbol_rules(symbol)
        normalized_quantity = self.normalize_quantity(rules, quantity)
        if normalized_quantity <= 0:
            return None

        reference_price = await self.get_mark_price(symbol)
        if dry_run:
            return OrderExecution(
                order_id=f"dry-run-market-{symbol}-{side}-{int(time.time())}",
                status="FILLED",
                side=side,
                price=reference_price,
                avg_price=reference_price,
                orig_qty=normalized_quantity,
                executed_qty=normalized_quantity,
            )

        payload = await self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": _normalize_decimal(normalized_quantity),
                "newOrderRespType": "RESULT",
                "reduceOnly": "true" if reduce_only else "false",
            },
            signed=True,
        )
        return OrderExecution(
            order_id=str(payload.get("orderId") or payload.get("clientOrderId") or "unknown"),
            status=str(payload.get("status") or "UNKNOWN"),
            side=str(payload.get("side") or side),
            price=_decimal(payload.get("price"), _normalize_decimal(reference_price)),
            avg_price=_decimal(payload.get("avgPrice"), _normalize_decimal(reference_price)),
            orig_qty=_decimal(payload.get("origQty"), _normalize_decimal(normalized_quantity)),
            executed_qty=_decimal(payload.get("executedQty"), "0"),
        )

    async def create_aggressive_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        reduce_only: bool,
        dry_run: bool,
        max_attempts: int = 3,
        market_fallback: bool = False,
    ) -> OrderExecution | None:
        rules = await self.get_symbol_rules(symbol)
        normalized_quantity = self.normalize_quantity(rules, quantity)
        remaining_quantity = normalized_quantity
        if remaining_quantity <= 0:
            return None

        total_executed_qty = Decimal("0")
        total_notional = Decimal("0")
        last_price = Decimal("0")
        price_buffers = (
            (Decimal("0.0002"), Decimal("0.0003"), Decimal("0.0005"), Decimal("0.0007"), Decimal("0.0010"))
            if reduce_only
            else (Decimal("0.0002"), Decimal("0.0005"), Decimal("0.0010"))
        )
        max_attempts = min(max_attempts, len(price_buffers))

        for attempt in range(max_attempts):
            if remaining_quantity <= 0:
                break

            ask_price, bid_price = await self.get_book_ticker(symbol)
            reference_price = ask_price if side == "BUY" else bid_price
            if reference_price <= 0:
                reference_price = await self.get_mark_price(symbol)
            if reference_price <= 0:
                continue

            buffer = price_buffers[min(attempt, len(price_buffers) - 1)]
            limit_price = reference_price * (Decimal("1") + buffer) if side == "BUY" else reference_price * (Decimal("1") - buffer)
            execution = await self.create_limit_order(
                symbol=symbol,
                side=side,
                quantity=remaining_quantity,
                price=limit_price,
                reduce_only=reduce_only,
                dry_run=dry_run,
            )
            if execution is None or execution.executed_qty <= 0:
                if not dry_run and attempt < (max_attempts - 1):
                    await asyncio.sleep(0.15)
                continue

            executed_qty = min(remaining_quantity, execution.executed_qty)
            total_executed_qty += executed_qty
            total_notional += executed_qty * execution.avg_price
            last_price = execution.avg_price
            remaining_quantity = self.normalize_quantity(rules, remaining_quantity - executed_qty)

            if remaining_quantity > 0 and not dry_run and attempt < (max_attempts - 1):
                await asyncio.sleep(0.15)

        if remaining_quantity > 0 and market_fallback:
            self.logger.warning(
                "IOC 지정가 미체결 잔량을 시장가로 전환합니다: symbol=%s side=%s remaining_qty=%s reduce_only=%s",
                symbol,
                side,
                _normalize_decimal(remaining_quantity),
                reduce_only,
            )
            fallback_execution = await self.create_market_order(
                symbol=symbol,
                side=side,
                quantity=remaining_quantity,
                reduce_only=reduce_only,
                dry_run=dry_run,
            )
            if fallback_execution is not None and fallback_execution.executed_qty > 0:
                fallback_qty = min(remaining_quantity, fallback_execution.executed_qty)
                total_executed_qty += fallback_qty
                total_notional += fallback_qty * fallback_execution.avg_price
                last_price = fallback_execution.avg_price
                remaining_quantity = self.normalize_quantity(rules, remaining_quantity - fallback_qty)

        if total_executed_qty <= 0:
            return None

        average_price = total_notional / total_executed_qty if total_executed_qty > 0 else last_price
        return OrderExecution(
            order_id=f"ioc-limit-{symbol}-{side}-{int(time.time())}",
            status="FILLED" if remaining_quantity <= 0 else "PARTIALLY_FILLED",
            side=side,
            price=last_price,
            avg_price=average_price,
            orig_qty=normalized_quantity,
            executed_qty=total_executed_qty,
        )