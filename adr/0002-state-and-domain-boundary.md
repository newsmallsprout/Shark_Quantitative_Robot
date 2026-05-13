# ADR-0002: `_state`（看板 DTO）与领域模型边界

## 状态

已采纳（骨架阶段，2026-05-09）

## 上下文

- `main.py` 中全局 `_state: dict` 被 WebSocket/REST 直接序列化给前端，字段随迭代增长，**无版本化 schema**。
- `StrategyRunner` 内部同时使用 `dict` 持仓、`list[dict]` 成交历史，与 `engine.paper_engine` 的 `Order`/`Position` **概念重叠**。
- 目标：资金相关不变量（数量非负、状态合法跃迁）应落在**领域层**；HTTP 仅消费**只读投影**。

## 决策

1. **目标边界**：后续若继续领域层迁移，再新增 `domain/trading` 包定义 `Side`、`OrderStatus`、`TradeOrder` 及合法状态跃迁。
2. **当前落地状态**：本仓库当前尚未包含 `domain/` 包；短期内以 `execution/order_command.py` 和 Go 边界校验先收敛 Redis 订单契约。
3. **`_state` 定位**：在迁移完成前，仍为**进程内缓存 DTO**；不作为复式记账或审计的唯一来源。长期目标是由应用服务从领域聚合根**投影**出 `_state` 或独立 Read Model。
4. **幂等**：订单命令已预留边界校验和 token；真正幂等门闩（Redis/DB）属基础设施，后续按实盘需要补齐。

## 后果

- **正面**：订单状态可单测；非法跃迁可在服务层显式失败。
- **负面**：领域包尚未落地，短期内 Python 与 Go 仍需分别维护边界校验。

## 参考

- `execution/order_command.py`
- `main.py` `_state` 与 `StrategyRunner._trade_history`
