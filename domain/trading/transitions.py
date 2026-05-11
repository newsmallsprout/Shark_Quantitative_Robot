from __future__ import annotations

from typing import Dict, FrozenSet

from .enums import OrderStatus

_ALLOWED: Dict[OrderStatus, FrozenSet[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


def can_transition_order_status(current: OrderStatus, target: OrderStatus) -> bool:
    """是否允许从 `current` 跃迁到 `target`。"""
    return target in _ALLOWED.get(current, frozenset())


def assert_order_status_transition(current: OrderStatus, target: OrderStatus) -> None:
    """非法跃迁抛出 ValueError，便于在应用服务层拦截。"""
    if not can_transition_order_status(current, target):
        raise ValueError(f"invalid OrderStatus transition: {current.value} -> {target.value}")
