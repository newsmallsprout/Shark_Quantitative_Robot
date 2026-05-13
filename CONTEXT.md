# Shark Quantitative Robot — Domain Glossary

> 领域词汇表。Agent 和开发者共享的语言。更新于 2026-05-11。

## 交易概念

**Tick（tick）**
主循环的一个迭代，约 1-6 秒。包含：刷新行情 → 预取AI信号 → 管理持仓 → 开仓评分 → 执行开仓。

**开仓信号（Signal）**
决定是否开仓及方向的数据来源。分两类：
- **AI信号**：三模型委员会投票通过的方向（优先级最高）
- **兜底信号**：多方因子投票机制（AI缺失时使用，5因子需≥2票）

**AI委员会（Committee）**
三个LLM模型投票决定方向：
- **主分析师** DeepSeek V3.2（权重1票，>900 tokens）
- **复核员** Qwen Max（权重1票，短路优化）
- **情绪员** 豆包 Seed 2.0 Pro（权重0.5票，仅情绪分析）
投票规则：总票数≥1.5通过，DeepSeek HOLD时短路跳过后续模型。

**行情类型（Regime）**
每个币对独立判定的市场状态，由 `market_regime.py` 的 `RegimeDetector` 计算：
- **强趋势**（strong_trend）：ADX>30 + DI方向明确 → 顺势加仓
- **弱趋势**（weak_trend）：ADX 20-30 → 轻仓试探
- **高波震荡**（high_vol_ranging）：ATR>2%，宽区间 → 高抛低吸
- **低波震荡**（low_vol_ranging）：ADX<20，窄区间 → 网格小刀
- **突破**（breakout）：量突变+价格破区间 → 追入
- **乱震**（choppy）：低ADX+高ATR → 蚊子仓试探
- **死水**（dead）：ADX<10+ATR<0.5% → 不开仓

**策略入场（Strategic Entry）**
根据行情类型计算最优入场价，而非市价盲入：
- 趋势市 → 回调到 EMA9
- 震荡市 → 贴近区间边界
- 突破市 → 追在突破点上方
由 `_strategic_entry()` 方法计算。

**持仓管理（Position Manager）**
持仓管理已收敛到 `StrategyRunner.tick()` 内的 RangePlan/动态止盈止损流程：
- 计划止损 → 保本保护 → 移动止盈 → 快速止盈 → 动态止损 → 震荡补仓
旧 `AIPositionManager` 已移除，避免形成第二套仓位状态机。

**金字塔加仓（Pyramid）**
主流币（BTC/ETH）在强趋势下允许分4层加仓。山寨禁止加仓。
每层保证金为前一层的一半。

**反思器（Reflector）**
每笔止损后自动诊断亏损原因的模块（`trade_reflector.py`）：
- 5维度：信号弱、止损过紧、入场位差、方向错误、微利亏损
- 累积5笔后触发战术调整
- 调整项：AI阈值、止损宽度、入场过滤、兜底冷却

**在线学习器（Online Learner）**
`online_learner.py`，包含两个子系统：
- **Q-Learning**：线性函数逼近，学习何时信任AI信号。18维特征向量→3动作(HOLD/LONG/SHORT)
- **进化策略(ES)**：种群大小12，优化7个元参数。每15笔评估一个个体，12×15=180笔完成一代

## 架构

**StrategyRunner**
核心类。持有所有运行时状态：
- `self.positions`：当前持仓 dict
- `self.balance`：可用余额
- `self._trade_history`：平仓记录
- `self._ai_signal_cache`：AI信号缓存
- `self._regime_cache`：行情检测缓存

**MarketDataFeed**
市场数据源（Gate.io REST API）。提供：
- `prices`：实时价格
- `volumes`：24h成交量
- `changes`：24h涨跌幅
- `funding_rates`：资金费率

**双轨策略（Dual Strategy）**
`dual_strategy.py` 定义两种策略模板：
- **主流币**（STABLE）：BTC/ETH，保证金4%余额，宽止损-15%，允许金字塔
- **山寨币**（VOLATILE）：高波动精选，保证金0.2%，紧止损-6%~-12%

行情检测器在此基础上叠加行情倍率（margin_mult）。

**进化引擎（Evolution Engine）**
`_check_evolution()` 每20 tick运行一次：
- 连亏检测 → 缩减保证金
- 止损率检测 → 放宽止损
- 连亏5+ → 暂停山寨
- 盈利恢复 → 逐步恢复参数

## 数据流

```
Gate.io API → MarketDataFeed.refresh()
    ↓
tick() 主循环:
  1. 预取AI信号 (asyncio.gather, 最多4币对并行)
  2. 检查持仓 (RangePlan/动态止损止盈/震荡补仓)
  3. 评分排序 (volume × funding_extreme)
  4. 逐币对处理:
     a. 行情检测 (RegimeDetector)
     b. 保证金计算 (RangePlan.position_size_pct，配置只能向下限额)
     c. 方向判定 (RangePlan.bias + PlanGate)
     d. 计划入场带门禁 (不再由 Python 改写计划入场)
     e. 实盘下单 (LiveEngine, 仅 SHARK_MODE=live)
     f. 创建持仓
  5. 更新状态 (_update_state → WebSocket推送 + REST API)
```

## 配置约定

- **余额公式**: `balance = initial_capital + gross_realized - total_fees`
- **初始资金**: $200（纸盘模拟），实盘由 `SHARK_INITIAL_CAPITAL` 环境变量控制
- **手续费**: `maker_fee`（挂单费率，可能为负=返佣），从合约规格实时读取
- **Quanto乘数**: BTC合约 `quanto=0.0001`（1张=0.0001BTC），其他合约 `quanto=1`
  - 所有金额计算必须乘 quanto，否则 BTC 费用爆炸100x+

## 文件职责

| 文件 | 职责 | 行数 |
|---|---|---|
| `main.py` | 核心调度：tick循环、持仓管理、开仓逻辑、API服务 | 1857 |
| `ai_strategy.py` | 三模型委员会：DeepSeek+Qwen+豆包，信号缓存 | 409 |
| `dual_strategy.py` | 双轨策略配置：主流/山寨参数 | 169 |
| `market_regime.py` | 行情检测器：每币对独立判定10种行情 | 258 |
| `kline_cache.py` | K线缓存+技术指标：RSI/ATR/ADX/EMA | 184 |
| `multi_exchange.py` | 多交易所价格聚合：Binance/Bybit/OKX/Gate | 275 |
| `trade_reflector.py` | 止损反思器：5维度诊断+自动调整 | 169 |
| `online_learner.py` | 在线学习：Q-Learning+进化策略 | 449 |
| `live_engine.py` | 实盘执行引擎：Gate.io API下单/平仓/对账 | 307 |
| `evolve_v2.py` | 自进化引擎v2：10战术库+模式识别 | 571 |
| `evolve.py` | 自进化引擎v1：批量模式（cron用） | 439 |
| `oscillation.py` | 震荡检测器（已废弃，被行情检测替代） | 197 |
| `backtest.py` | 三天回测脚本 | 275 |
| `battle_report.py` | 战报生成器：每10分钟推送到Slack | 166 |
| `dialogue_ammo.py` | Alpha角色台词弹药库 | 357 |
| `character_voice.py` | 角色语音引擎 | 157 |

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `SHARK_MODE` | 交易模式：paper/live | paper |
| `GATE_API_KEY` | Gate.io API Key（实盘必填） | — |
| `GATE_API_SECRET` | Gate.io API Secret | — |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | — |
| `QWEN_KEY` | 通义千问 API Key | — |
| `VOLC_KEY` | 火山引擎 ARK Key | — |
| `AI_COMMITTEE_FULL` | 启用长prompt（耗token多） | 0 |
| `SHARK_API_TOKEN` | 内部API鉴权 | — |
| `SHARK_INITIAL_CAPITAL` | 初始资金（实盘用） | 200 |

## 关键决策记录（ADR）

**ADR-1**: 开仓方向仅AI委员会决定，无震荡/费率兜底（v9）
→ 后被推翻，v11加入多方因子兜底。原因：AI频繁HOLD导致无单可开。

**ADR-2**: 余额 = 初始 + 毛利 - 手续费（v11 fix 2）
`gross_realized` 追踪纯毛利，`total_fees` 独立累计。两者分离。

**ADR-3**: 容器文件版本陷阱（v11强化）
`docker cp` 会用本地旧文件覆盖容器修复版。修复流程：先 `docker cp` 拉最新版→打补丁→推回。

**ADR-4**: 实盘引擎以"最小侵入"方式集成（v12）
`live_engine.py` 通过拦截 `self.positions` 的创建/销毁来路由实盘订单。
paper模式行为完全不变。
