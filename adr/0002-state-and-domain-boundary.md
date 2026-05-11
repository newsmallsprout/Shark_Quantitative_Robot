# ADR-0002: `_state`（看板 DTO）与领域模型边界

## 状态

已采纳（骨架阶段，2026-05-09）

## 上下文

- `main.py` 中全局 `_state: dict` 被 WebSocket/REST 直接序列化给前端，字段随迭代增长，**无版本化 schema**。
- `StrategyRunner` 内部同时使用 `dict` 持仓、`list[dict]` 成交历史，与 `engine.paper_engine` 的 `Order`/`Position` **概念重叠**。
- 目标：资金相关不变量（数量非负、状态合法跃迁）应落在**领域层**；HTTP 仅消费**只读投影**。

## 决策

1. 新增包 **`domain/trading`**：定义 **`Side`、`OrderStatus`、`TradeOrder`** 及 **`OrderStatus` 合法跃迁表**（见 `transitions.py`）。
2. **`_state` 定位**：在迁移完成前，仍为**进程内缓存 DTO**；不作为复式记账或审计的唯一来源。长期目标是由应用服务从领域聚合根**投影**出 `_state` 或独立 Read Model。
3. **`engine.paper_engine`**：保持现有行为；新逻辑优先引用 `domain.trading` 类型，再通过适配器写入引擎或 `_state`（绞杀者模式，按需 PR）。
4. **幂等**：`TradeOrder.idempotency_key` 字段预留；真正幂等门闩（Redis/DB）属基础设施，不在本 ADR 范围。

## 后果

- **正面**：订单状态可单测；非法跃迁可在服务层显式失败。
- **负面**：短期内双轨类型并存，需约定「新代码走 domain」直至旧路径删除。

## 参考

- `domain/trading/order.py`
- `main.py` `_state` 与 `StrategyRunner._trade_history`
