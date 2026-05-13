# Architecture Review — 2026-05-11

> 基于 Matt Pocock `improve-codebase-architecture` 技能的方法论。
> 术语：模块=接口+实现。深度=小接口+大实现。浅层=接口≈实现。

## 发现的浅层模块

### 1. `tick()` — 678 行上帝方法

**问题**：一个方法承担了 5 个职责，全部内联。
调用者（trading_loop）只知道"调 tick"，但 tick 内部有大量不可独立测试的逻辑。

**删除测试**：
- 删除 `_score_pairs` 逻辑 → 排序逻辑分散到每个调用点
- 删除方向判定 → AI/兜底逻辑在开仓前无法独立验证
- 删除开仓执行 → 每个币对的开仓条件散落在 continue/skip 链中

**深化方案**：拆为 4 个子方法

```python
async def tick(self, prices, volumes, changes, funding_rates, mark_prices):
    # 1. 持仓管理（已有）
    await self._manage_positions(prices, changes, funding_rates, mark_prices)
    # 2. 预取 AI 信号（已有）
    await self._prefetch_signals(prices, funding_rates, changes, volumes)
    # 3. 开仓循环
    await self._open_loop(prices, volumes, changes, funding_rates, mark_prices)

def _open_loop(self, ...):
    scored = self._score_pairs(prices, volumes, changes, funding_rates)
    for sym, score, vol, chg in scored:
        if not self._can_open(sym, ...):  # 所有 continue 条件
            continue
        side, signal = self._determine_direction(sym, ...)
        if not side:
            continue
        self._execute_open(sym, side, signal, ...)
```

**收益**：
- 每个子方法可独立测试
- 方向判定可 mock AI 缓存
- 开仓条件可在测试中逐个验证

### 2. `_close_position()` — 161 行

**问题**：实盘平仓、纸盘记账、手续费计算、交易历史、反射、学习更新全部耦合。

**深化方案**：

```python
def _close_position(self, sym, px, reason, pnl_pct, prices=None):
    pos = self.positions[sym]  # 先拿数据，不 pop
    self._close_live(sym, pos)  # 实盘平仓
    self._account_close(pos, px)  # 纸盘记账
    self._record_history(sym, pos, px, reason, pnl_pct)  # 交易历史
    self._reflect_and_learn(sym, pos, px, reason, pnl_pct)  # 反思+学习
    self.positions.pop(sym)  # 最后才删除
```

### 3. RangePlan 方向判定 — 内联在 tick() 中

**问题**：RangePlan 的方向判定和订单构建曾直接在 tick 循环内，难以单测。

**深化方案**：保留 RangePlan 为唯一信号来源，把可复用边界逻辑提取到 `execution/` 下的独立 helper。

```python
from execution.order_command import build_order_command
```

## 保持深度的模块（不变）

| 模块 | 深度评估 | 理由 |
|---|---|---|
| `ai_strategy.py` | ✅ 深 | 三模型委员会封装良好，接口简单 |
| `market_regime.py` | ✅ 深 | detect()→regime+diag 接口清晰 |
| `kline_cache.py` | ✅ 深 | RSI/ADX/ATR 计算封装，外部只调方法 |
| `online_learner.py` | ✅ 深 | extract→act→learn 管道完整 |
| `live_engine.py` | ✅ 深 | Gate.io API 全部封装在类内 |
| `dual_strategy.py` | ✅ 深 | 纯配置，零逻辑 |

## 实施优先级

1. **P0**: 拆 `tick()` → 测试能跑起来
2. **P1**: 拆 `_close_position()` → 实盘/纸盘分离
3. **P2**: 继续收敛 RangePlan 方向判定 → 方向与订单边界可独立测试
4. **P3**: `get_mark_prices()` 中的 HTML 渲染移到 web 层

## 不做的事

- 不拆 `_check_evolution()` — 54行，深度合理
- 不拆 `MarketDataFeed` — 接口清晰
- 不动 AI 委员会 — 已是深模块

---

*下一次评审：50 笔实盘交易后。*
