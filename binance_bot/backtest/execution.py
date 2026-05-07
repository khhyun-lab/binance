from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .data_loader import Candle


@dataclass(slots=True)
class PendingOrder:
    kind: str
    symbol: str
    side: str
    quantity: Decimal
    margin_usdt: Decimal
    created_at: int
    reason: str
    action: object | None = None


@dataclass(slots=True)
class SimulatedPosition:
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    margin_usdt: Decimal
    leverage: int
    entry_time: int
    entry_fee: Decimal
    entry_slippage_cost: Decimal
    take_profit_price: Decimal
    stop_loss_price: Decimal


@dataclass(slots=True)
class TradeRecord:
    entry_time: int
    exit_time: int
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    return_on_margin: Decimal
    exit_reason: str
    holding_seconds: int


@dataclass(slots=True)
class EquityPoint:
    timestamp: int
    balance: Decimal
    equity: Decimal
    drawdown_pct: Decimal


@dataclass(slots=True)
class ExecutionFill:
    kind: str
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    timestamp: int
    reason: str
    margin_usdt: Decimal
    action: object | None = None
    take_profit_price: Decimal = Decimal("0")
    stop_loss_price: Decimal = Decimal("0")
    trade: TradeRecord | None = None


class FuturesExecutionSimulator:
    def __init__(
        self,
        initial_balance: Decimal,
        leverage: int,
        margin_per_trade: Decimal,
        taker_fee: Decimal,
        maker_fee: Decimal,
        slippage_bps: Decimal,
        max_open_positions: int,
    ) -> None:
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.leverage = leverage
        self.margin_per_trade = margin_per_trade
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.slippage_bps = slippage_bps
        self.max_open_positions = max_open_positions
        self.pending_orders: list[PendingOrder] = []
        self.positions: dict[str, SimulatedPosition] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[EquityPoint] = []

    def queue_order(self, order: PendingOrder) -> None:
        self.pending_orders.append(order)

    def available_balance(self) -> Decimal:
        reserved_margin = sum((position.margin_usdt for position in self.positions.values()), Decimal("0"))
        return self.balance - reserved_margin

    def execute_pending_orders(self, candle_map: dict[str, Candle]) -> list[ExecutionFill]:
        fills: list[ExecutionFill] = []
        remaining_orders: list[PendingOrder] = []
        for order in self.pending_orders:
            candle = candle_map.get(order.symbol)
            if candle is None:
                remaining_orders.append(order)
                continue
            if order.kind in {"enter", "scale_in"}:
                if order.kind == "enter" and len(self.positions) >= self.max_open_positions:
                    continue
                fill = self._execute_entry_order(order, candle)
                if fill is not None:
                    fills.append(fill)
            elif order.kind == "exit":
                fill = self._execute_exit_order(order, candle)
                if fill is not None:
                    fills.append(fill)
        self.pending_orders = remaining_orders
        return fills

    def process_intrabar_triggers(self, candle_map: dict[str, Candle]) -> list[ExecutionFill]:
        fills: list[ExecutionFill] = []
        for symbol, position in list(self.positions.items()):
            candle = candle_map.get(symbol)
            if candle is None:
                continue
            trigger_price, reason = self._intrabar_trigger_price(position, candle)
            if trigger_price is None:
                continue
            fills.append(self._close_position(position, candle.open_time, trigger_price, reason, reference_price=trigger_price))
        return fills

    def record_equity(self, timestamp: int, candle_map: dict[str, Candle]) -> None:
        equity = self.balance
        for symbol, position in self.positions.items():
            candle = candle_map.get(symbol)
            if candle is None:
                continue
            mark_price = Decimal(str(candle.close))
            if position.side == "LONG":
                equity += (mark_price - position.entry_price) * position.quantity
            else:
                equity += (position.entry_price - mark_price) * position.quantity
        peak = max((point.equity for point in self.equity_curve), default=self.initial_balance)
        if equity > peak:
            peak = equity
        drawdown_pct = Decimal("0") if peak <= 0 else ((peak - equity) / peak) * Decimal("100")
        self.equity_curve.append(EquityPoint(timestamp=timestamp, balance=self.balance, equity=equity, drawdown_pct=drawdown_pct))

    def update_exit_lines(self, symbol: str, take_profit_price: Decimal, stop_loss_price: Decimal) -> None:
        position = self.positions.get(symbol)
        if position is None:
            return
        position.take_profit_price = take_profit_price
        position.stop_loss_price = stop_loss_price

    def _execute_entry_order(self, order: PendingOrder, candle: Candle) -> ExecutionFill | None:
        reference_price = Decimal(str(candle.open))
        execution_side = "BUY" if order.side == "LONG" else "SELL"
        fill_price = self._apply_slippage(execution_side, reference_price, is_entry=True)
        notional = fill_price * order.quantity
        fee = notional * self.taker_fee
        slippage_cost = (fill_price - reference_price).copy_abs() * order.quantity
        self.balance -= fee

        existing = self.positions.get(order.symbol)
        if existing is None:
            self.positions[order.symbol] = SimulatedPosition(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                entry_price=fill_price,
                margin_usdt=order.margin_usdt,
                leverage=self.leverage,
                entry_time=candle.open_time,
                entry_fee=fee,
                entry_slippage_cost=slippage_cost,
                take_profit_price=Decimal("0"),
                stop_loss_price=Decimal("0"),
            )
        else:
            total_quantity = existing.quantity + order.quantity
            weighted_entry = ((existing.entry_price * existing.quantity) + (fill_price * order.quantity)) / total_quantity
            existing.quantity = total_quantity
            existing.entry_price = weighted_entry
            existing.margin_usdt += order.margin_usdt
            existing.entry_fee += fee
            existing.entry_slippage_cost += slippage_cost

        return ExecutionFill(
            kind=order.kind,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            fill_price=fill_price,
            timestamp=candle.open_time,
            reason=order.reason,
            margin_usdt=order.margin_usdt,
            action=order.action,
        )

    def _execute_exit_order(self, order: PendingOrder, candle: Candle) -> ExecutionFill | None:
        position = self.positions.get(order.symbol)
        if position is None:
            return None
        reference_price = Decimal(str(candle.open))
        return self._close_position(position, candle.open_time, self._apply_slippage(order.side, reference_price, is_entry=False), order.reason, reference_price=reference_price)

    def _close_position(self, position: SimulatedPosition, timestamp: int, actual_exit_price: Decimal, reason: str, reference_price: Decimal) -> ExecutionFill:
        exit_side = "SELL" if position.side == "LONG" else "BUY"
        fee = actual_exit_price * position.quantity * self.taker_fee
        gross_pnl = self._gross_pnl(position.side, position.quantity, position.entry_price, actual_exit_price)
        slippage_cost = position.entry_slippage_cost + ((actual_exit_price - reference_price).copy_abs() * position.quantity)
        net_pnl = gross_pnl - position.entry_fee - fee
        self.balance += gross_pnl - fee
        trade = TradeRecord(
            entry_time=position.entry_time,
            exit_time=timestamp,
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=actual_exit_price,
            gross_pnl=gross_pnl,
            fees=position.entry_fee + fee,
            slippage_cost=slippage_cost,
            net_pnl=net_pnl,
            return_on_margin=Decimal("0") if position.margin_usdt <= 0 else (net_pnl / position.margin_usdt) * Decimal("100"),
            exit_reason=reason,
            holding_seconds=max(0, (timestamp - position.entry_time) // 1000),
        )
        self.trades.append(trade)
        self.positions.pop(position.symbol, None)
        return ExecutionFill(
            kind="exit",
            symbol=position.symbol,
            side=exit_side,
            quantity=position.quantity,
            fill_price=actual_exit_price,
            timestamp=timestamp,
            reason=reason,
            margin_usdt=position.margin_usdt,
            action=None,
            trade=trade,
        )

    def _intrabar_trigger_price(self, position: SimulatedPosition, candle: Candle) -> tuple[Decimal | None, str]:
        high = Decimal(str(candle.high))
        low = Decimal(str(candle.low))
        # 한 캔들 안에서 TP와 SL이 모두 닿으면 보수적으로 손실이 더 큰 쪽을 우선 체결로 간주한다.
        if position.side == "LONG":
            tp_hit = high >= position.take_profit_price > 0
            sl_hit = low <= position.stop_loss_price > 0
            if tp_hit and sl_hit:
                return position.stop_loss_price, "stop_loss"
            if sl_hit:
                return position.stop_loss_price, "stop_loss"
            if tp_hit:
                return position.take_profit_price, "take_profit"
            return None, ""
        tp_hit = low <= position.take_profit_price > 0
        sl_hit = high >= position.stop_loss_price > 0
        if tp_hit and sl_hit:
            return position.stop_loss_price, "stop_loss"
        if sl_hit:
            return position.stop_loss_price, "stop_loss"
        if tp_hit:
            return position.take_profit_price, "take_profit"
        return None, ""

    def _apply_slippage(self, side: str, reference_price: Decimal, is_entry: bool) -> Decimal:
        ratio = self.slippage_bps / Decimal("10000")
        if side == "BUY":
            return reference_price * (Decimal("1") + ratio)
        return reference_price * (Decimal("1") - ratio)

    def _gross_pnl(self, side: str, quantity: Decimal, entry_price: Decimal, exit_price: Decimal) -> Decimal:
        if side == "LONG":
            return (exit_price - entry_price) * quantity
        return (entry_price - exit_price) * quantity