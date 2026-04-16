<div align="center">

# Shark Quantitative Robot

###  Gate.io USDT 永续

*分层风控 · 可配置编排 · 可观测状态*

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/Dashboard-React-61DAFB?logo=react&logoColor=black)](https://react.dev/)

</div>

---

## 产品定位

**Shark Quantitative Robot（鲨鱼量化机器人）** 面向 USDT 本位永续合约，采用「行情接入 → 策略决策 → 风控闸门 → 执行网关」分层架构，可选接入 LLM 做盘面体制判别、打分与 L1/L2 调参。设计目标：在 **资本保全优先** 前提下，实现行为可复现、参数可审计、运行可观测。

## 架构概览

```mermaid
flowchart LR
  subgraph ingest [接入]
    GW[GateFuturesGateway]
    WS[WebSocket / REST]
  end
  subgraph brain [决策]
    SE[StrategyEngine]
    ST[Strategies]
    AI[MarketAnalyzer + LLM]
  end
  subgraph guard [风控]
    RE[RiskEngine]
    SM[StateMachine]
  end
  subgraph surface [交互]
    API[FastAPI :8002]
    UI[React 战术终端]
  end
  GW --> SE
  SE --> ST
  AI -->|ZMQ IPC| SE
  ST --> RE
  RE --> GW
  API --> SM
  UI --> API
```

## 核心能力

| 层级 | 能力 |
|------|------|
| **执行** | 纸面 / 实盘网关抽象、订单生命周期、合约规格同步 |
| **策略** | 可插拔引擎（如 Beta 中性高频、弹射、微观战术等），由 `config/settings.yaml` 编排 |
| **风控** | 回撤、名义、单腿限制与状态机协同 |
| **智能（可选）** | 体制打分、L1/L2 调参；独立进程经 ZeroMQ 与主循环解耦 |
| **观测** | 结构化日志、REST + WebSocket API、前端指挥舱 |

## 环境要求

- Python **3.10+**（生产建议 3.11）
- Node **18+**（前端工程）
- 全栈 Docker 场景可配合 **Redis**（见 `docker-compose.yml`）

## 快速启动 — 后端

```bash
pip install -r requirements.txt
cp config/settings.model.yaml config/settings.yaml
# 编辑 settings.yaml：填入交易所密钥、品种池、策略与风控参数
export SHARK_CONFIG_PATH="${PWD}/config/settings.yaml"
python main.py
```

默认 API：**http://127.0.0.1:8002**

## 快速启动 — 前端

```bash
cd frontend
npm ci
npm run dev
```

开发模式下 Vite 将 `/api`、`/ws` 代理至 `127.0.0.1:8002`，需先启动后端。

## 配置文件约定

| 文件 | 是否入库 | 说明 |
|------|----------|------|
| `config/settings.model.yaml` | **是** | 字段齐全的 **默认模板**（密钥位留空），可安全提交。 |
| `config/settings.yaml` | **否**（`.gitignore`） | **实际运行配置**：API Key、品种池、策略与风控参数。 |

若缺少 `settings.yaml`，配置管理器会回退加载模板并记录日志。**切勿**将真实密钥提交公共仓库。

## Docker（推荐：Nginx + 应用 + Redis）

架构：**宿主机 → Nginx:80 → `shark-quant:8002`（FastAPI + 静态前端）**；Python 不映射到宿主机端口，仅内网 `expose`。Redis 仅容器网络内可访问。

```bash
cp .env.example .env
# 编辑 .env：生产务必设置 SHARK_API_TOKEN（openssl rand -hex 32），SKIP_LICENSE_CHECK=0
cp config/settings.model.yaml config/settings.yaml
# 编辑 settings.yaml：交易所密钥等
docker compose up -d --build
```

- 构建镜像需要 **`license/public.pem`** 存在于项目目录（私钥勿提交；见 `.dockerignore`）。
- **许可证指纹**：在**宿主机**签发的 `license.key` 与 **Docker 容器内**算出的设备指纹不同。解决：在 `.env` 设置 **`SHARK_LICENSE_FINGERPRINT=`** 为 `license.key` 里 **`machine_fingerprint` 字段**（与签发时机器一致），或改用 **`SKIP_LICENSE_CHECK=1`** 仅作开发。
- 浏览器访问：**http://localhost**（或 `.env` 里 `SHARK_HTTP_PORT` 指定的端口），**不要**再访问 `:8002`。
- 日志：`docker logs -f shark-quant-bot`；宿主机 `./logs/` 已映射。
- **修改 `SHARK_API_TOKEN` 后需重新 `docker compose build --no-cache` 再 up**（令牌同时打进前端）。
- 可选：在 `docker/nginx/default.conf` 上自行增加 `listen 443 ssl` 与证书挂载（HTTPS）。

## 商业发行（混淆 / 强制许可证）

仓库默认 **`src/shark_build_profile.py` 中 `COMMERCIAL_DISTRIBUTION=True`**：运行主程序时**不承认 `SKIP_LICENSE_CHECK`**（单测在 `tests/conftest.py` 内临时改回 `False`，仅 pytest 进程）。

**完整交付说明**见 **`docs/客户交付手册.md`**（发行方与客户分工、GitHub Releases、许可证签发）。本地检查清单：**`delivery/本地发行检查清单.md`**。

**生成混淆包**（需 PyArmor 正式许可或受试用限制；大项目试用可能报 `out of license`）：

```bash
pip install -r requirements-obfuscate.txt
# 确保 PATH 含 pyarmor 可执行文件
python scripts/build_commercial_release.py -O dist/commercial_obfuscated
bash scripts/package_customer_release.sh
```

**Docker 一体镜像**：

```bash
docker build -f Dockerfile.commercial -t shark-quant:commercial .
```

说明：混淆与强校验无法做到绝对不可破解；私钥仅用于签发，切勿随镜像泄露。

## 本地 API 与安全

- **裸跑** `python main.py` 时默认仅绑定 **127.0.0.1:8002**；**Docker** 内由 **`SHARK_API_HOST=0.0.0.0`** 监听，对外仅经 **Nginx**。
- **`GET /api/config`** 响应中的交易所密钥、LLM Key 等已 **脱敏**，不会返回明文。
- 可选加固：设置 **`SHARK_API_TOKEN`**，并在前端配置 **`VITE_SHARK_API_TOKEN`**（与后端一致）。设置后 **所有 `/api/*`**（除 **`GET /api/health`** 探活）需在请求头携带 `Authorization: Bearer <token>`；WebSocket **`/ws/market_data`** 通过查询参数 **`?token=`** 传递同一令牌（由前端 `initWebSocket` 自动拼接）。
- **`GET /api/health`**：无需鉴权，供负载均衡或探活使用。

## 策略控制面 — 术语与主参数

以下与 `config/settings.model.yaml` 对齐，便于理解各模块职责与调参入口；完整默认值以仓库内该文件为准。

| 模块 | 职责 | 典型参数 |
|------|------|----------|
| `strategy.active_strategies` / `allocations` | 多引擎 **资金权重** 与 **单品种单仓** 约束 | `allocations`、`single_open_per_symbol`、`regime_switch_anchor_symbol` |
| `strategy.params` | **Core** 均值回归 / 追击腿 阈值与 **括号止盈止损** | `neutral_rsi_*`、`attack_ai_threshold`、`*_cooldown_sec`、`core_entry_tp_bps` / `core_entry_sl_bps`、高波动下 **ATR** 放宽 `core_atr_sl_widen_mult`、保本 `core_breakeven_arm_r` |
| `beta_neutral_hf` | **配对统计套利**：价差 z-score、相关性、截面筛选 | `entry_zscore`、`exit_zscore`、`min_correlation`、`pair_leverage`、`max_hold_sec`、`leg_micro_take_usdt` |
| `playbook` | **体制路由**（矩阵 / 游击）按权益与波动切换 | `matrix_capital_threshold_usdt`、`guerrilla_leverage`、`position_ttl_minutes` |
| `market_oracle` | **跨所** 拥挤度、崩盘锚、OBI 否决 | `crowded_funding_rate_min`、`crash_max_anchor_return_pct` |
| `risk` | **硬风控**：回撤、结构风险、狂暴模式门槛 | `daily_drawdown_limit`、`berserker_obi_threshold`、`drawdown_cool_down_sec` |
| `paper_engine` | 纸面 **费率 / 滑点 / 强平** 与可选 **开仓括号** | `taker_fee_rate`、`require_entry_tp_sl_limits` |
| `execution` | 订单风格与 **Kelly** 类规模上限 | `kelly_fraction`、`max_allowed_leverage`、狙击单 TTL 等 |

## 风险提示

数字资产衍生品交易可能导致 **本金全部损失**。本软件仅供研究与学习参考；合规、资金安全与运维责任由使用者自行承担。历史回测或纸面表现 **不构成** 未来收益承诺。

## 开源许可

若仓库包含 `LICENSE` 文件则从其约定；否则在明确许可前请仅作内部评估使用。

---

<div align="center">

**Shark Quantitative Robot** · 谋定而后动

</div>
