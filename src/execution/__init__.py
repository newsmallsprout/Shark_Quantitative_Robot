"""
执行层：OrderIntent / OrderManager（Hummingbot 式生命周期与对账）的归宿。

迁移完成前，StrategyEngine 仍可直接调用网关；启用 OrderManager 后由配置开关接入。
"""

from src.execution.order_types import OrderIntent, OrderRecord, OrderState
from src.execution.order_manager import OrderManager

__all__ = ["OrderIntent", "OrderRecord", "OrderState", "OrderManager"]
