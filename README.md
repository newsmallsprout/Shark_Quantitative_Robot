# Shark 2.0 — 多策略量化交易系统

## 架构

Shark 2.0 是一个**多语言微服务**量化交易平台，Python策略大脑 + Go执行引擎 + Go进化引擎：

```
┌─────────────────────────────────────────────────────┐
│                  前端 (React)                        │
│              http://localhost:80                     │
└──────────────────────┬──────────────────────────────┘
                       │ REST / WebSocket
┌──────────────────────▼──────────────────────────────┐
│              shark2 (Python 3.11)                    │
│   策略大脑：AI信号、仓位管理、风控、行情检测           │
│   → 发布订单到 Redis                                 │
└──────┬────────────────┬────────────────┬────────────┘
       │                │                │
       ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│shark-executor│ │shark-matcher │ │shark-evolver │
│   (Go)       │ │   (Go)       │ │   (Go)       │
│              │ │              │ │              │
│实盘下单       │ │模拟撮合       │ │进化学习       │
│→ Gate.io API │ │→ Redis价格    │ │→ 策略优化     │
│              │ │  快照撮合     │ │→ 知识积累     │
└──────────────┘ └──────────────┘ └──────┬───────┘
       │                │                │
       └────────────────┼────────────────┘
                        ▼
               ┌────────────────┐
               │     Redis      │
               │  (消息总线)     │
               └────────────────┘
                        │
               ┌────────▼────────┐
               │   PostgreSQL    │
               │  (持久化存储)    │
               └─────────────────┘
```

### 容器拓扑

| 容器 | 语言 | 功能 |
|------|------|------|
| `shark2` | Python 3.11 | 策略大脑、REST API、WebSocket、前端 |
| `shark-executor` | Go | 实盘交易 — Gate.io 下单执行 |
| `shark-matcher` | Go | 模拟交易 — 订单撮合引擎 |
| `shark-evolver` | Go | 进化引擎 — RL训练、GA优化、知识学习、TradingView抓取 |
| `redis` | — | 消息总线、状态缓存 |
| `db` | PostgreSQL 16 | 交易历史、订单、余额日志 |

### 数据流

1. **Python** 运行主循环：获取行情 → 读取 RangePlan → PlanGate 决策与风控 → 发布到 `shark:orders:new`
2. **Go executor** 订阅 `shark:orders:new`（仅实盘）→ Gate.io 下单
3. **Go matcher** 订阅 `shark:orders:new`（模拟盘）→ 用Redis价格快照撮合
4. **Go evolver** 读取Redis交易历史 → DQN训练 + GA进化 + TradingView学习 → 发布 `shark:rl:action` + `shark:evo:pending`
5. **Python** 订阅 `shark:evo:pending` → 审批队列 → 前端通过/拒绝

### 核心功能

- **AI委员会**：DeepSeek + Qwen + 豆包 多模型信号验证
- **双轨策略**：主流币(BTC/ETH)和山寨币独立参数配置
- **行情检测**：10种市场状态分类，差异化止盈止损倍率
- **动态风控**：ATR动态止损（非固定百分比），保证金可配置
- **进化引擎**：Go实现DQN强化学习 + GA遗传算法种群进化
- **反思引擎**：AI驱动多维诊断，每笔亏损后DeepSeek分析+立即调整
- **TradingView学习**：每30分钟自动抓取公开策略/指标，喂入知识库
- **纸盘/实盘隔离**：SHARK_MODE环境变量控制，手动开关防误操作
- **许可证系统**：Redis验证 License Token，无许可证所有写操作返回猫图
- **模拟盘持久化**：重启不重置，状态保存至 Redis；手动重置按钮可清空数据并重置资金（默认500）

---

## 快速开始

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 GATE_API_KEY/SECRET 和 AI 密钥

# 2. 构建并启动所有服务
docker compose up -d --build

# 3. 生成许可证
docker exec -it shark2 python scripts/gen_license.py --user shark --expiry 2026-12-31
# 复制输出的 hex token

# 4. 打开面板
open http://localhost:80

# 5. 登录
# 点击顶栏「登录」按钮，粘贴许可证 token，验证通过后方可操作按钮
```

**交易默认关闭。** 点击顶栏"开始交易"开启模拟盘（5 tick预热后开仓）。

---

## 许可证系统

所有 POST/PUT/DELETE 写操作需要有效许可证。无许可证点击按钮返回猫图 + 无权限提示。

### 生成许可证

```bash
# 服务器上：
docker exec -it shark2 python scripts/gen_license.py --user shark --expiry 2026-12-31

# 本地：
SHARK_REDIS_URL=redis://localhost:6379/0 python scripts/gen_license.py --user shark --expiry 2026-12-31
```

### 前端登录

打开面板 → 点击右上角「登录」按钮 → 粘贴许可证 token → 验证通过。

许可证存储在浏览器 localStorage (`shark_license`)，每次请求携带 `X-Shark-License` header。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SHARK_LICENSE_ENABLED` | `1` | 是否启用许可证鉴权 |
| `SHARK_REDIS_URL` | `redis://redis:6379/0` | Redis（许可证存储） |

---

## 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SHARK_MODE` | `paper` | `paper` 模拟盘 或 `live` 实盘 |
| `GATE_API_KEY` | — | Gate.io API密钥 |
| `GATE_API_SECRET` | — | Gate.io API密钥 |
| `SHARK_API_TOKEN` | 可选 | REST/WS鉴权Bearer Token |
| `SHARK_REDIS_URL` | `redis://redis:6379/0` | Redis连接 |
| `DATABASE_URL` | `postgresql://shark:shark@db:5432/shark` | PostgreSQL连接 |
| `SHARK_HTTP_PORT` | `80` | HTTP监听端口 |
| `DEEPSEEK_API_KEY` | — | DeepSeek AI（可选） |
| `QWEN_KEY` | — | 通义千问（可选） |
| `VOLC_KEY` | — | 豆包AI（可选） |

---

## API接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/health` | — | 健康检查 |
| GET | `/api/status` | Bearer | 完整运行快照 |
| GET | `/api/paper/status` | Bearer | 模拟盘状态 |
| POST | `/api/paper/toggle` | Bearer + License | 切换模拟盘开关 |
| POST | `/api/paper/reset` | Bearer + License | 重置模拟盘（清空持仓/历史/DB，重置资金） |
| GET | `/api/live/status` | Bearer | 实盘状态 |
| POST | `/api/live/toggle` | Bearer + License | 切换实盘开关 |
| POST | `/api/shark/mode` | Bearer + License | 切换 paper/live 模式 |
| GET | `/api/evo/pending` | Bearer | 待审批进化建议 |
| POST | `/api/evo/approve/{id}` | Bearer + License | 审批通过 |
| POST | `/api/evo/reject/{id}` | Bearer + License | 拒绝 |
| GET | `/api/history` | Bearer | 分页交易历史 |
| GET | `/api/license/check` | — | 检查许可证是否有效 |
| POST | `/api/license/login` | — | 验证许可证 token |
| WS | `/ws` | `?token=` | 实时状态推送(~1Hz) |

### 进化引擎API（端口8081）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/tv/alert` | TradingView Webhook |
| GET | `/rl/status` | RL训练状态 |
| GET | `/rl/patterns` | 已学习交易模式 |
| GET | `/health` | 健康检查 |

---

## 项目结构

```
Shark_Quantitative_Robot/
├── main.py                 # 主入口（FastAPI + 策略循环）
├── license.py              # 许可证鉴权中间件
├── ai_strategy.py          # 多模型AI分析
├── dual_strategy.py        # 双轨资金配置
├── live_engine.py           # Gate.io实盘引擎
├── market_regime.py        # 行情检测器（10种类型）
├── trade_reflector.py      # 止损反思引擎（AI驱动）
├── online_learner.py       # 在线学习器
├── kline_cache.py          # K线缓存
├── multi_exchange.py       # 多所价格聚合
├── dialogue_ammo.py        # 角色台词库
├── character_voice.py      # LLM角色配音
│
├── executor/               # Go实盘执行器
├── matcher/                # Go模拟撮合器
├── rl/                     # Go进化引擎
│   ├── env.go              #   交易环境（Gym兼容）
│   ├── agent.go            #   DQN智能体
│   ├── ga.go               #   遗传算法种群进化
│   ├── backtest.go         #   回测引擎+模式提取
│   ├── webhook.go          #   TradingView Webhook+知识库
│   ├── tv_learn.go         #   TradingView策略抓取学习
│   └── cmd/main.go         #   主入口
│
├── persistence/            # SQLAlchemy + Redis桥接
├── observability/          # 中间件（请求ID）
├── scripts/                # 工具脚本
│   └── gen_license.py      #   许可证生成器
├── web/                    # React前端 (Vite + TypeScript)
├── tests/
├── alembic/                # 数据库迁移
├── tools/                  # 诊断工具
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## 开发

```bash
# Python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py

# 前端（热更新）
cd web && npm ci && npm run dev

# 测试
docker exec shark2 python3 -m pytest tests/ -v

# 生成许可证（本地）
SHARK_REDIS_URL=redis://localhost:6379/0 python scripts/gen_license.py --user shark --expiry 2026-12-31
```

---

## 免责声明

本软件仅供**学习与研究**。加密货币衍生品波动剧烈，模拟表现不构成实盘收益预测。使用者自行承担监管合规、交易所协议与资金损失责任。不构成投资建议，不提供任何担保。
