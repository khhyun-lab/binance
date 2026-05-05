from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(slots=True)
class Position:
    symbol: str
    side: str
    quantity: str
    entry_price: str
    order_id: str
    opened_at: str
    leverage: int
    margin_usdt: str = "0"
    notional_usdt: str = "0"
    take_profit_price: str = "0"
    stop_loss_price: str = "0"
    take_profit_pct: str = "0"
    stop_loss_pct: str = "0"
    realized_pnl_usdt: str = "0"
    commission_usdt: str = "0"
    last_exit_update_at: str = ""
    last_trade_sync_at: str = ""
    trend_follow_armed: bool = False
    entry_count: int = 1
    exit_count: int = 0

    @property
    def quantity_decimal(self) -> Decimal:
        return Decimal(self.quantity)

    @property
    def entry_price_decimal(self) -> Decimal:
        return Decimal(self.entry_price)

    @property
    def margin_usdt_decimal(self) -> Decimal:
        return Decimal(self.margin_usdt)

    @property
    def notional_usdt_decimal(self) -> Decimal:
        return Decimal(self.notional_usdt)

    @property
    def take_profit_price_decimal(self) -> Decimal:
        return Decimal(self.take_profit_price)

    @property
    def stop_loss_price_decimal(self) -> Decimal:
        return Decimal(self.stop_loss_price)

    @property
    def take_profit_pct_decimal(self) -> Decimal:
        return Decimal(self.take_profit_pct)

    @property
    def stop_loss_pct_decimal(self) -> Decimal:
        return Decimal(self.stop_loss_pct)

    @property
    def realized_pnl_usdt_decimal(self) -> Decimal:
        return Decimal(self.realized_pnl_usdt)

    @property
    def commission_usdt_decimal(self) -> Decimal:
        return Decimal(self.commission_usdt)

    @property
    def has_exit_lines(self) -> bool:
        return self.take_profit_price != "0" and self.stop_loss_price != "0"


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.positions: dict[str, Position] = {}
        self.metadata: dict[str, str] = {}

    def load(self) -> None:
        if not self.path.exists():
            self.positions = {}
            self.metadata = {}
            return

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.positions = {
            symbol: Position(**data)
            for symbol, data in payload.get("positions", {}).items()
        }
        self.metadata = {str(key): str(value) for key, value in payload.get("metadata", {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "positions": {symbol: asdict(position) for symbol, position in self.positions.items()},
            "metadata": self.metadata,
        }
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def get(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def set(self, position: Position) -> None:
        self.positions[position.symbol] = position
        self.save()

    def remove(self, symbol: str) -> None:
        self.positions.pop(symbol, None)
        self.save()

    def set_metadata(self, key: str, value: str) -> None:
        self.metadata[key] = value
        self.save()

    def get_metadata(self, key: str) -> str | None:
        return self.metadata.get(key)