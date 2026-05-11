# Shark 2.0 — Quantitative Execution & Risk Research Stack

**Choose documentation language · 点击下方标题展开对应语言全文（中英分块，互不混排）**

<details open>
<summary><strong>English</strong> — Institutional &amp; quant desk overview</summary>

## Executive summary

Shark 2.0 is a **crypto derivatives research and paper-trading workstation** built around **USDT-margined perpetual swaps**. It consumes **public reference data** from Gate.io—**contract specifications**, **funding-rate mechanics**, and **fee schedules**—so simulated execution reflects **venue-grade economics**: configurable **slippage**, **taker/maker fee** assumptions, **notional** and **max-leverage** bounds, and logic consistent with **perpetual funding** behaviour on live markets.

The runtime reconciles **discretionary overlays** with **systematic guardrails**: **dual-track capital allocation** (liquid majors vs **high-beta alt** sleeves), **regime / oscillation** filters, optional **multi-LLM alpha** pipelines for idea generation and **position sizing**, and a **priority-tier circuit breaker** layer (`engine/safety.py`) to contain **operational risk** during stress.

Delivery includes **FastAPI** REST endpoints for **liveness**, **aggregated book / P&amp;L snapshots**, and a paginated **trade blotter**; a **Vite + React** dashboard ingests **~1 Hz WebSocket** pushes for **mark-to-market** visibility—suitable for **research**, **sandboxed strategy acceptance**, and **middle-office-style** monitoring (non-production by default unless you harden networking and secrets).

---

## Value proposition for finance stakeholders

| Theme | What it addresses |
|--------|-------------------|
| **Execution realism** | **Cost of carry** awareness via funding; explicit fee and **market-impact** knobs—not a frictionless backtest toy. |
| **Risk architecture** | **Exposure caps**, cooldowns, **pyramid / trail** rules on majors, and **circuit breakers** with severities from **INFO** to **CRITICAL**. |
| **Portfolio construction** | **Sleeve separation**: e.g. BTC/ETH **swing** parameters vs selected **high-volatility** alt **scalp** sleeves (`dual_strategy.py`). |
| **Signal stack (optional)** | Cloud LLM APIs (env-only keys) can contribute **risk/reward**-aware targets; **signal fusion** and evolution tooling (`evolve_v2.py`, `multi_exchange.py`) extend **cross-venue** and **tactical** research—enable only what your governance allows. |
| **Observability** | **Request correlation** (`X-Request-ID` / `[rid=…]` logs) for audit-friendly tracing across HTTP and background ticks. |

---

## Architecture (technical)

- **Runtime:** `main.py` — single **ASGI** entry (`main:app`); **asyncio** concurrency for Uvicorn, market refresh, and strategy **tick** loop.
- **Data plane:** REST polling against Gate.io public APIs; dashboard clients subscribe to **`/ws`** for consolidated state.
- **Persistence (optional paths):** SQLAlchemy / Alembic / Redis modules under `persistence/` when configured.
- **Domains:** `engine/` (orchestrator, **paper engine**, rate limiting, safety), `strategies/`, `domain/trading/`, `ai/` heuristics layer.

See `adr/` for entrypoint and DDD decisions.

---

## Requirements

| Item | Version |
|------|---------|
| Python | 3.11+ recommended |
| Node.js | 20+ (local `web/` builds) |
| Container | Docker / Docker Compose |

---

## Quick start (Docker)

1. `cp .env.example .env` — set secrets **only** on the host; do not commit.
2. `docker compose up -d --build`
3. Dashboard: `http://localhost` (host maps container `80`; adjust with `SHARK_HTTP_PORT` as needed).
4. Host directories `./logs` and `./data` are mounted for **logs** and **state**.

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd web && npm ci && npm run build && cd ..
python main.py
```

Frontend hot reload: `cd web && npm ci && npm run dev` — align Vite proxy with `SHARK_HTTP_PORT` (see `web/vite.config.ts`).

---

## Configuration (summary)

- Authoritative template: **`.env.example`** → copy to `.env`.
- **HTTP:** `SHARK_HTTP_PORT`
- **Logging:** `SHARK_LOG_LEVEL`; stderr uses `[rid=…]` tied to `X-Request-ID`.
- **LLM (optional):** e.g. `DEEPSEEK_API_KEY`, `VOLC_KEY`, `QWEN_KEY` — non-empty only if that **provider path** is enabled in `ai_strategy.py`.
- **API hardening:** `SHARK_API_TOKEN` (Bearer for REST; `?token=` on WebSocket); mirror with `VITE_SHARK_API_TOKEN` in `web/`.
- **Licensing:** if your distribution uses checks, see `SKIP_LICENSE_CHECK`, `SHARK_LICENSE_FINGERPRINT` in `.env.example`.

---

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness |
| GET | `/api/status` | Aggregated runtime snapshot |
| GET | `/api/history` | Paginated trade history (`offset`, `limit`) |
| WS | `/ws` | ~1 Hz JSON push |

**Single canonical entry:** `main:app`. Legacy second app in `api/server.py` is explicitly deprecated; see `adr/0001-single-http-entrypoint.md`.

**Correlation:** Responses echo `X-Request-ID`; supply your own for **end-to-end** log alignment.

---

## Repository layout

```
Shark_Quantitative_Robot/
├── main.py
├── ai_strategy.py / ai_position.py
├── dual_strategy.py
├── evolve_v2.py         # Evolution / regime research (optional)
├── multi_exchange.py    # Cross-venue signals (optional)
├── backtest.py
├── ai/
├── engine/              # orchestrator, paper_engine, safety, rate_limiter
├── strategies/
├── persistence/
├── domain/
├── observability/
├── api/
├── adr/
├── web/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Security & operations

- **Secrets:** Never bake `.env` into images in production; inject via Compose / orchestrator **secret stores**. Rotate any leaked keys.
- **Network:** Default Compose exposes port **80**; use firewalls or an **authenticated reverse proxy** on untrusted networks. Treat **PnL** and **position** payloads as **confidential operational** data—apply **least-privilege** access consistent with **middle-office** hygiene.
- **Dependencies:** `requirements.txt` lists direct imports; optional lines are commented—pin when enabling.
- **CPU baseline (offline):** `python tools/profile_tick_baseline.py` (`PROFILE_TICKS`, `PROFILE_TOP`).

---

## Disclaimer

This software is provided for **education and research** only. **Cryptocurrency derivatives** are **highly volatile**; **paper** or **simulated** performance is **not** indicative of **live P&amp;L**. You are solely responsible for **regulatory compliance**, **exchange terms**, and **capital loss**. **No warranty** or **investment advice** is provided.

---

## License

Unless a `LICENSE` file is present, rights are reserved; follow the license shipped with your distribution.

</details>

<details>
<summary><strong>中文</strong> — 金融机构与量化团队导读</summary>

## 导读摘要

Shark 2.0 是一款面向 **USDT 本位永续合约**的 **加密衍生品投研与纸面交易工作台**。系统对接 Gate.io **公开参考数据**——**合约细则**、**资金费率机制**与**手续费结构**——使仿真成交具备 **贴近交易所层级的经济约束**：可配置 **滑点**、**吃单/挂单费率**假设、**名义本金**与**最大杠杆**边界，以及与市场惯例一致的 **永续资金费** 行为近似。

运行时在 **自由裁量策略叠加** 与 **系统性风控护栏** 之间做平衡：**双轨制资金配置**（高流动性主流品种 vs **高 Beta 山寨**子组合）、**市场状态 / 震荡** 识别过滤、可选 **多模型 LLM Alpha** 管线（ idea 生成与 **仓位管理**），以及基于优先级的 **熔断式** 安全层（`engine/safety.py`），用于在压力情景下收敛 **操作风险**。

交付形态为 **FastAPI** REST（**探活**、**聚合账本/盯市盈亏快照**、分页 **成交回报**）与 **Vite + React** 看板（约 **1Hz WebSocket** 推送 **盯市盈亏** 状态）——适用于 **策略研究**、**sandbox 验收** 与 **类中台风控** 可视监控（默认非生产级，需自行加固网络与密钥治理）。

---

## 面向金融客户的价值表述

| 维度 | 说明 |
|------|------|
| **执行层贴近度** | 通过资金费率与费率表体现 **持有成本**；显式 **冲击成本（滑点）** 与手续费模型——非零摩擦回测。 |
| **风险管理架构** | **名义敞口/杠杆** 约束、**冷却期**、主流币 **金字塔/移动止盈** 规则，以及 **INFO 至 CRITICAL** 分级的 **熔断** 机制。 |
| **组合构建** | **子策略分仓**：如 BTC/ETH **波段** 参数 vs 精选 **高波动** 山寨 **短线**（`dual_strategy.py`）。 |
| **信号栈（可选）** | 云侧 LLM（仅环境变量密钥）可输出带 **风险收益比** 考量的标的筛选；`evolve_v2.py`、`multi_exchange.py` 等扩展 **跨所** 与 **战术库** 研究——按贵司 **模型风险管理** 与合规要求逐项启用。 |
| **可审计性** | **请求关联 ID**（`X-Request-ID` / 日志 `[rid=…]`）便于跨 HTTP 与后台 **tick** 对齐排障与追溯。 |

---

## 架构说明（技术）

- **运行时：** `main.py` — 单一 **ASGI** 入口（`main:app`）；**asyncio** 并发承载 Uvicorn、行情刷新与策略 **节拍**。
- **数据面：** 对 Gate.io 公共 REST 轮询；前端经 **`/ws`** 订阅聚合状态。
- **持久化（按需）：** `persistence/` 下 SQLAlchemy、Alembic、Redis 等路径。
- **核心模块：** `engine/`（编排、**纸面撮合引擎**、限流、安全）、`strategies/`、`domain/trading/`、`ai/` 启发式层。

架构决策见 `adr/`。

---

## 环境要求

| 项目 | 版本 |
|------|------|
| Python | 建议 3.11+ |
| Node.js | 本地构建 `web/` 时 20+ |
| 容器 | Docker / Compose |

---

## 快速开始（Docker）

1. `cp .env.example .env` — 密钥仅在宿主机填写，**勿提交**仓库。
2. `docker compose up -d --build`
3. 浏览器访问 `http://localhost`（默认映射 80；可用 `SHARK_HTTP_PORT` 调整）。
4. 宿主机 `./logs`、`./data` 挂载用于 **日志** 与 **状态** 留存。

---

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd web && npm ci && npm run build && cd ..
python main.py
```

前端热更新：`cd web && npm ci && npm run dev` — 与 `SHARK_HTTP_PORT` 对齐代理（见 `web/vite.config.ts`）。

---

## 配置摘要

- 模板以 **`.env.example`** 为准，复制为 `.env`。
- **HTTP：** `SHARK_HTTP_PORT`
- **日志：** `SHARK_LOG_LEVEL`；标准错误含 `[rid=…]`，对应 `X-Request-ID`。
- **LLM（可选）：** 如启用 `ai_strategy.py` 对应厂商，配置 `DEEPSEEK_API_KEY`、`VOLC_KEY`、`QWEN_KEY` 等，**禁止**写入 Git。
- **鉴权：** `SHARK_API_TOKEN`（REST 用 Bearer；WebSocket 用 `?token=`）；前端 `web/.env.local` 中 `VITE_SHARK_API_TOKEN` 保持一致。
- **许可证：** 见 `.env.example` 中 `SKIP_LICENSE_CHECK`、`SHARK_LICENSE_FINGERPRINT`。

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 探活 |
| GET | `/api/status` | 聚合运行快照 |
| GET | `/api/history` | 历史成交分页 |
| WS | `/ws` | 约每秒 JSON 推送 |

**单一规范入口：** `main:app`。历史第二入口已废弃，说明见 `adr/0001-single-http-entrypoint.md`。

**关联 ID：** 响应回传 `X-Request-ID`；可自带该头做 **全链路** 日志对齐。

---

## 目录结构

```
Shark_Quantitative_Robot/
├── main.py
├── ai_strategy.py / ai_position.py
├── dual_strategy.py
├── evolve_v2.py         # 自进化 / 状态机研究（可选）
├── multi_exchange.py    # 跨所信号（可选）
├── backtest.py
├── ai/
├── engine/              # 编排、纸面引擎、安全、限流
├── strategies/
├── persistence/
├── domain/
├── observability/
├── api/
├── adr/
├── web/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## 安全与运维

- **密钥：** 生产环境勿将 `.env` `COPY` 入镜像；使用 Compose / 编排器 **密钥管理** 注入；泄露密钥须在云控制台 **轮换**。
- **网络：** 默认暴露宿主机 **80**；公网须 **防火墙** 或带鉴权的 **反向代理**。**盈亏与仓位** 类数据按 **敏感内部信息** 管控。
- **依赖：** `requirements.txt` 为当前路径直接依赖；文件末尾注释为可选包——启用时建议 **锁定版本**。
- **性能基线（无网络）：** `python tools/profile_tick_baseline.py`（`PROFILE_TICKS`、`PROFILE_TOP`）。

---

## 免责声明

本软件仅供 **学习与研究**。**加密衍生品** **波动剧烈**；**模拟/纸面** 表现 **不构成** **实盘收益** 预测。使用者须自行承担 **监管合规**、**交易所协议** 与 **资金损失** 责任。本项目 **不构成投资建议**，亦 **不提供任何担保**。

---

## 许可

若仓库根目录无 `LICENSE` 文件则权利保留；请以实际发行版附带许可为准。

</details>
