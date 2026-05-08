from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(slots=True)
class StrategyOrderPlan:
    kind: str
    symbol: str
    position_side: str
    order_side: str
    quantity: Decimal
    margin_usdt: Decimal
    reason: str
    detail_reasons: list[str] = field(default_factory=list)
    score_display: str = ""
    metadata: dict[str, str | int | bool | list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class PlanDecision:
    allowed: bool
    reason: str
    detail_reasons: list[str] = field(default_factory=list)
    plan: StrategyOrderPlan | None = None
    metadata: dict[str, str | int | bool | list[str]] = field(default_factory=dict)