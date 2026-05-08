# Shark 2.0 · Quantitative Trading Robot  
# Shark 2.0 · 量化交易机器人

**English:** A research-oriented crypto futures stack that combines live public market data (Gate.io USDT perpetuals) with optional multi-model AI signals, risk controls, and a real-time web dashboard.  
**中文：** 面向研究与演练的加密货币合约系统：对接 Gate.io 公开 USDT 永续行情与合约参数，可选多模型 AI 信号、风控与组合模块，并提供 WebSocket 实时看板。

---

## Table of contents · 目录

1. [Highlights · 特性](#highlights--特性)  
2. [Architecture · 架构](#architecture--架构)  
3. [Requirements · 环境要求](#requirements--环境要求)  
4. [Quick start (Docker) · 快速开始](#quick-start-docker--快速开始-docker)  
5. [Local development · 本地开发](#local-development--本地开发)  
6. [Configuration · 配置说明](#configuration--配置说明)  
7. [HTTP API · 接口](#http-api--接口)  
8. [Repository layout · 目录结构](#repository-layout--目录结构)  
9. [Security & operations · 安全与运维](#security--operations--安全与运维)  
10. [Disclaimer · 免责声明](#disclaimer--免责声明)

---

## Highlights · 特性

**English**

- **Market realism:** Contract specs, funding, and fee assumptions aligned with Gate.io public APIs; configurable slippage, cooldowns, and exposure caps in the core loop.  
- **Modular strategies:** Optional components include AI targets (`ai_strategy.py`), AI position sizing (`ai_position.py`), oscillation detection, dual-track capital rules, and a circuit-breaker-aware risk layer.  
- **Dashboard:** FastAPI serves REST endpoints and a WebSocket feed; production images bundle a Vite + React + Tailwind UI (`web/`).  
- **Automation-friendly:** Docker multi-stage build (Node build → Python runtime); `docker compose` mounts `./data` and `./logs` for persistence.

**中文**

- **贴近实盘参数：** 从 Gate.io 公共接口拉取合约规格、资金费率等，主循环内可配置滑点、冷却与总敞口上限。  
- **策略与风控可插拔：** AI 标的、AI 仓位、震荡识别、双轨资金、熔断式风控等可按依赖与配置启用。  
- **可视化：** FastAPI 提供 REST 与 WebSocket；镜像内包含前端构建产物，亦可本地单独开发 `web/`。  
- **部署：** 多阶段 Dockerfile；Compose 持久化数据与日志目录。

---

## Architecture · 架构

**English**

- **Runtime:** `main.py` runs an asyncio event loop: Uvicorn (FastAPI) concurrently with market refresh and strategy ticks.  
- **Data:** REST polling against `api.gateio.ws` for tickers and contract metadata; WebSocket `/ws` pushes aggregated state to the UI.  
- **AI path (optional):** `ai_strategy.py` calls cloud LLM HTTP APIs using keys supplied **only** via environment variables (never commit secrets).

**中文**

- **运行模型：** `main.py` 在单进程内用 asyncio 并行跑 HTTP 服务、行情刷新与策略节拍。  
- **数据：** 对 Gate.io 公共 REST 轮询；前端通过 `/ws` 获取聚合状态。  
- **AI（可选）：** `ai_strategy.py` 仅通过环境变量读取各云厂商 API Key，密钥不得写入仓库。

---

## Requirements · 环境要求

| Item · 项目 | English | 中文 |
|-------------|---------|------|
| Python | 3.11+ recommended | 建议 3.11+ |
| Node.js | 20+ (for local `web/` builds) | 本地构建 `web/` 时用 20+ |
| Container | Docker / Docker Compose | Docker / Compose |

---

## Quick start (Docker) · 快速开始 (Docker)

**English**

1. Copy environment template and edit **private** values (keys, tokens) locally:  
   `cp .env.example .env`  
2. Build and start:  
   `docker compose up -d --build`  
3. Open the dashboard: `http://localhost` (host port maps container `80`; override with `SHARK_HTTP_PORT` as needed).  
4. Logs and state: host directories `./logs` and `./data` are mounted into the container.

**中文**

1. 复制环境模板并仅在本地 `.env` 中填写密钥（勿提交）：`cp .env.example .env`  
2. 构建并启动：`docker compose up -d --build`  
3. 浏览器访问 `http://localhost`（默认映射 80 端口，可用 `SHARK_HTTP_PORT` 调整）。  
4. `./logs`、`./data` 挂载到容器内，便于排障与留存。

---

## Local development · 本地开发

**English**

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Optional: build the React dashboard so /assets is available
cd web && npm ci && npm run build && cd ..
python main.py
```

For frontend hot reload during UI work:

```bash
cd web && npm ci && npm run dev
# Vite proxies /api and /ws. Default proxy target is 127.0.0.1:8002 (web/vite.config.ts);
# if you run `python main.py` with SHARK_HTTP_PORT=80, either set SHARK_HTTP_PORT=8002
# or change the proxy to match your backend port.
```

**中文**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd web && npm ci && npm run build && cd ..   # 可选：生成 dist 供 FastAPI 挂载
python main.py
```

前端热更新：

```bash
cd web && npm ci && npm run dev
# 默认代理到 127.0.0.1:8002（见 web/vite.config.ts）。若 `main.py` 监听 80 端口，
# 请将 SHARK_HTTP_PORT=8002 或修改 Vite 代理与后端一致。
```

---

## Configuration · 配置说明

**English**

- Primary reference: **`.env.example`**. After copying to `.env`, set variables required for your deployment.  
- **HTTP:** `SHARK_HTTP_PORT` (default 80 inside container when using the provided Compose file).  
- **LLM (optional AI path):** e.g. `DEEPSEEK_API_KEY`, `VOLC_KEY`, `QWEN_KEY` — must be non-empty only if you enable those call paths in `ai_strategy.py`.  
- **API hardening (when implemented in your fork):** `SHARK_API_TOKEN` and matching frontend token for Bearer authentication.  
- **License / fingerprint:** If your distribution uses license checks, follow comments in `.env.example` for `SKIP_LICENSE_CHECK` and `SHARK_LICENSE_FINGERPRINT`.

**中文**

- 以 **`.env.example`** 为准，复制为 `.env` 后在本地填写。  
- **HTTP：** `SHARK_HTTP_PORT`（与 Compose 端口映射配合）。  
- **LLM：** 如启用 `ai_strategy.py` 中对应提供方，需在环境中配置 `DEEPSEEK_API_KEY`、`VOLC_KEY`、`QWEN_KEY` 等，且**不得**写入 Git。  
- **鉴权：** 若分支中实现了 Bearer 校验，需与前端 `VITE_*` 等变量保持一致。  
- **许可证：** 若使用许可证校验，参见 `.env.example` 中 `SKIP_LICENSE_CHECK`、`SHARK_LICENSE_FINGERPRINT` 说明。

---

## HTTP API · 接口

**English** — Endpoints exposed by `main.py` (typical deployment):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness JSON payload |
| GET | `/api/status` | Aggregated runtime state snapshot |
| GET | `/api/history` | Paginated trade history (`offset`, `limit`) |
| WS | `/ws` | ~1 Hz JSON push of dashboard state |

**中文** — `main.py` 默认路由：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 探活 |
| GET | `/api/status` | 聚合状态 |
| GET | `/api/history` | 历史成交分页 |
| WS | `/ws` | 约每秒推送 JSON 状态 |

An alternate FastAPI app under `api/server.py` exists for orchestrator-centric deployments; integrate it only if your process wiring depends on it.

---

## Repository layout · 目录结构

```
Shark_Quantitative_Robot/
├── main.py              # Entry: FastAPI + trading loop
├── ai_strategy.py       # Optional multi-LLM strategy pipeline
├── ai_position.py       # Optional AI position manager
├── risk_manager.py      # Risk / circuit-breaker helpers
├── dual_strategy.py     # Dual-track symbol & capital rules
├── backtest.py          # Historical experiment script
├── ai/                  # Brain / heuristics (engineered AI layer)
├── engine/              # Orchestrator, safety, paper engine, rate limiting
├── strategies/          # Strategy implementations
├── api/                 # Standalone API module (optional integration)
├── web/                 # React (Vite) dashboard source + dist after build
├── Dockerfile           # Multi-stage image
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Security & operations · 安全与运维

**English**

- **Secrets:** Keep `.env` out of version control; rotate any key previously exposed in logs or chat. Do not bake `.env` into images in production—inject at runtime (Compose `env_file`, orchestrator secrets, etc.).  
- **Network exposure:** Default Compose publishes port 80 on the host. Restrict firewalls or place an authenticated reverse proxy in untrusted networks. REST and WebSocket data can include PnL and positions—treat as sensitive.  
- **Dependencies:** `requirements.txt` uses lower bounds (`>=`); for reproducible builds, pin versions or adopt a lockfile in regulated environments.  
- **Observability:** Application logs go under `./logs` when using the bundled Compose volume mapping; Uvicorn access logging may be reduced in code for noise control—verify your observability stack separately.

**中文**

- **密钥：** `.env` 不提交；曾泄露的 Key 应在云控制台轮换；生产环境勿将 `.env` `COPY` 进镜像，应在运行时注入。  
- **网络：** 默认映射宿主机 80 端口，公网部署请加防火墙或前置带鉴权的反向代理；`/api/*` 与 `/ws` 可能包含仓位与盈亏，按敏感数据处理。  
- **依赖：** `requirements.txt` 为下限版本；合规或生产可锁定版本或使用锁文件。  
- **可观测性：** Compose 挂载 `./logs`；代码中可能关闭部分访问日志以降低噪音——生产请用独立监控与告警。

---

## Disclaimer · 免责声明

**English:** This software is provided for educational and research purposes. Cryptocurrency derivatives are highly volatile; simulated or paper behaviour does not guarantee live performance. You are solely responsible for compliance with applicable laws, exchange terms, and for any capital loss. No warranty or investment advice is given.

**中文：** 本软件仅供学习与策略研究。加密衍生品波动极大，模拟或纸面表现不代表实盘结果。使用者须自行遵守法律法规与交易所条款，并承担全部资金与合规风险。本项目不构成投资建议，亦不提供任何担保。

---

## License · 许可

**English:** Unless a `LICENSE` file is present in this repository, all rights are reserved by the project authors. Add or follow the license file shipped with your distribution.

**中文：** 若仓库根目录未包含 `LICENSE` 文件，则权利保留；请以实际随发行版提供的许可文件为准。
