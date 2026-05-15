# Shark Quantitative Robot — 架构文档

版本: 2026-05-15 | 最后更新: 模块拆分 + 循环依赖修复

---

## 一、系统概览

Shark 是一个 Gate.io 合约量化交易机器人，Python 核心 + Go 辅助服务，通过 Redis Pub/Sub 串联。

**双层循环架构**：
- **Slow Loop** (Go evolver, 30m)：生成 RangePlan 计划 → Redis
- **Fast Loop** (Python runner, 每 tick)：读取计划 → PlanGate 门禁 → 开仓/平仓

**当前状态**：模拟盘 (paper trading)，资金 ~500 USDT，胜率 ~88%

---

## 二、目录结构

```
Shark_Quantitative_Robot/
│
├── main.py                     # 11行入口，asyncio.run(main())
├── docker-compose.yml          # 6个服务：db, redis, shark, executor, matcher, evolver
├── Dockerfile                  # Python 运行时镜像
├── requirements.txt
├── .env                        # API密钥、配置
│
├── core/                       # ★ 中枢引擎
│   ├── engine.py               # 759行 — 主事件循环、price feed、trading loop、启动流程
│   └── live.py                 # Live trading stub（实盘预留）
│
├── strategy/                   # ★ 策略决策层（9文件）
│   ├── runner.py               # 1479行 — StrategyRunner，tick 主循环，继承所有 Mixin
│   ├── dual.py                 # 双轨策略配置（稳定币/山寨币分类、资金上限、交易轨道）
│   ├── plans.py                # 328行 — RangePlan 构建、AI 转换、入场区/方向判定
│   ├── risk.py                 # 125行 — 杠杆/保证金计算、入场风控
│   ├── close.py                # 224行 — 平仓逻辑、PnL计算、止损熔断
│   ├── session.py              # 232行 — 持久化/重置/开关模式/强制平仓
│   ├── state.py                # 139行 — 权益重算、状态同步
│   ├── ai.py                   # 144行 — 多模型委员会 (DeepSeek/Qwen/Volc)
│   └── __init__.py             # Mixin 导出
│
│   StrategyRunner 继承链：
│   class StrategyRunner(SessionMixin, PlanMixin, RiskMixin, CloseMixin, StateMixin)
│
├── api/                        # HTTP/WS 网关
│   └── routes.py               # 436行 — FastAPI路由、WebSocket、静态文件挂载
│
├── market/                     # 行情数据
│   ├── data.py                 # 191行 — ContractSpec、手续费常量、交易对发现
│   ├── exchange.py             # Gate.io API 封装（余额、持仓、下单）
│   ├── kline.py                # K线数据拉取
│   └── regime.py               # 市场状态分类（波动率、趋势判断）
│
├── execution/                  # 订单生成与门禁
│   ├── plan_gate.py            # PlanGate — 双方向入场门禁（计划带命中判断）
│   ├── order_command.py        # 开仓/平仓指令生成
│   ├── prod_utils.py           # 生产环境工具（资金费率、标记价格）
│   ├── prod_alert.py           # 风控告警推送
│   └── redis_manager.py        # Redis 连接管理
│
├── character/                  # 看板娘台词系统
│   ├── voice.py                 # loli speech 生成 + LLM 调用 + 台词调度
│   └── dialogue.py             # 台词池（pop_line、分类匹配）
│
├── learning/                   # 在线学习（stub，Go evolver 接管主循环）
│   ├── online.py               # 19行 — OnlineLearner stub
│   └── reflector.py            # 交易复盘 stub
│
├── persistence/                # 数据持久化
│   ├── models.py               # SQLAlchemy ORM 模型（orders, trades, balance_logs）
│   ├── repository.py           # CRUD 封装
│   ├── session.py              # DB 会话管理
│   ├── bridge.py               # Redis ↔ PostgreSQL 桥接
│   ├── dialogue_store.py       # 台词持久化
│   └── redis_rate_limit.py     # API 限流
│
├── observability/              # 可观测性
│   ├── context.py              # 上下文传递
│   └── device_lock.py          # IP 白名单访问控制
│
├── utils/
│   └── license.py              # 180行 — Redis 许可证验证中间件
│
├── alembic/                    # 数据库迁移（PostgreSQL）
│   └── versions/
│
├── rl/                         # ★ Go 进化引擎 (shark-evolver)
│   ├── cmd/main.go             # 480行 — HTTP 服务入口 (8081端口)
│   ├── agent.go                # 328行 — DQN 强化学习 Agent
│   ├── ga.go                   # 234行 — 遗传算法参数优化
│   ├── env.go                  # 282行 — 交易环境模拟（回测用）
│   ├── backtest.go             # 434行 — 回测引擎
│   ├── tv_learn.go             # 595行 — TradingView 信号学习
│   ├── webhook.go              # 347行 — Webhook 接收 + 管理 API
│   └── planning/               # RangePlan 生成器
│       ├── planner.go          # 1170行 — 主规划器
│       ├── ai.go               # 346行 — AI 辅助规划
│       ├── book.go             # 174行 — 订单簿分析
│       ├── macro.go            # 287行 — 宏观经济因子
│       ├── news.go             # 123行 — 新闻情绪
│       ├── scheduler.go        # 106行 — 定时调度
│       └── types.go            # 211行 — 数据类型定义
│
├── executor/                   # ★ Go 实盘执行器
│   └── main.go                 # 349行 — Webhook 接收 + Gate.io 实盘下单
│
├── matcher/                    # ★ Go 模拟盘撮合
│   └── main.go                 # 121行 — Paper trading 订单验证/状态返回
│
├── web/                        # React 前端 Dashboard
│   ├── src/                    # TypeScript 源码
│   ├── dist/                   # 构建产物
│   └── public/                 # 静态资源
│
├── scripts/
│   ├── gen_license.py          # 许可证生成
│   └── migrate.sh              # 数据库迁移脚本
│
├── data/                       # 运行时数据（volume挂载）
└── logs/                       # 日志（volume挂载）
```

---

## 三、服务拓扑

```
                    ┌──────────────┐
                    │   React UI   │  :80 (FastAPI 挂载)
                    └──────┬───────┘
                           │ WS / REST
                    ┌──────▼───────┐
                    │  shark (Py)  │  core/engine.py 主循环
                    │  :80         │  strategy/runner.py tick
                    └──┬───┬───┬──┘
                       │   │   │
              Redis Pub/Sub (所有服务通过 shard:plan/replan 频道通信)
                       │   │   │
        ┌──────────────┼───┼──────────────┐
        │              │   │              │
   ┌────▼─────┐  ┌────▼───▼──┐  ┌───────▼──────┐
   │ evolver  │  │ executor  │  │   matcher    │
   │ (Go)     │  │ (Go)      │  │   (Go)       │
   │ :8081    │  │ webhook   │  │   paper模式  │
   │ 计划生成 │  │ Gate.io   │  │   订单状态   │
   └──────────┘  │ 实盘下单  │  │   返回       │
                 └───────────┘  └──────────────┘

   ┌──────────┐  ┌──────────┐
   │PostgreSQL│  │  Redis   │
   │ orders,  │  │ 状态缓存 │
   │ trades,  │  │ Pub/Sub  │
   │ balances │  │ 许可证   │
   └──────────┘  └──────────┘
```

---

## 四、数据流

### 开仓链路
```
evolver planner → RangePlan (Redis) → runner 读取
  → PlanGate 判断入场带 → AI 委员会评分
  → RiskMixin 杠杆/保证金计算 → build_order_command
  → Redis Pub order:new → executor/matcher 执行
  → 结果 Redis Pub order:filled → runner._open_position 记录
```

### 平仓链路
```
runner tick → 检查止盈止损/ATR熔断/微利止盈
  → CloseMixin._close_position → build_order_command(close)
  → Redis Pub order:close → executor/matcher 执行
  → 结果 → runner._close_position 计算 PnL → balance_logs
```

### 状态同步
```
每次 tick:
  _update_state(prices) → 重算权益、未实现盈亏 → Redis + get_state()
  每60秒: _save_paper_state → Redis 持久化持仓快照
```

---

## 五、已知问题与迭代方向

### 5.1 当前痛点

| 问题 | 影响 | 优先级 |
|------|------|--------|
| 跨模块 import 容易漏（如 `_schedule_loli_speech`） | 运行时 NameError | P0 |
| `core/engine.py` 仍有 fallback stubs（`except ImportError: def is_stable...`） | 掩盖真实导入错误 | P0 |
| `_character_event_seq` 在两个模块各自维护 | 序号不一致 | P1 |
| AI 委员会评分只有 3 个币对（正常 10+） | 开仓机会少 | P1 |
| 山寨币价格偶尔不刷新 | 持仓标记价不准 | P1 |
| Go evolover 的 RangePlan 生成逻辑不透明 | 策略调试困难 | P2 |
| 无集成测试，每次改完靠 docker logs 肉眼验证 | 迭代效率低 | P2 |

### 5.2 短期动作（本周）

1. **消除 fallback stubs**：删掉 `core/engine.py:70-79` 的 `except ImportError` dummy，让真实错误暴露
2. **补充语法门禁**：添加 `python -m py_compile` 预检脚本，提交前扫全部 .py 文件
3. **`_character_event_seq` 统一到一个模块**：删掉 close.py 的副本，从 runner.py 导入
4. **山寨币价格刷新修复**：追踪 market/data.py 中 alt coin 的 fetch 逻辑

### 5.3 中期动作（本月）

1. **配置集中化**：将散落各处的 `os.environ.get()` 收敛到 `core/config.py`，单例读取
2. **日志结构化**：统一 JSON 格式日志，包含 `timestamp`、`symbol`、`event`、`duration_ms`
3. **Go evolover 文档化**：为 `rl/planning/planner.go` 补充中文注释，说明计划生成规则


