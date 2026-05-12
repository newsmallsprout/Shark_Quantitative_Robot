# Shark 2.0 — Multi-Strategy Quantitative Trading System

## Architecture

Shark 2.0 is a **polyglot microservices** trading platform with hardware-accelerated components:

```
┌─────────────────────────────────────────────────────┐
│                  Frontend (React)                    │
│              http://localhost:80                     │
└──────────────────────┬──────────────────────────────┘
                       │ REST / WebSocket
┌──────────────────────▼──────────────────────────────┐
│              shark2 (Python 3.11)                    │
│   Strategy Brain: AI signals, position mgmt,        │
│   risk control, market regime detection             │
│   → publishes orders to Redis                       │
└──────┬────────────────┬────────────────┬────────────┘
       │                │                │
       ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│shark-executor│ │shark-matcher │ │shark-evolver │
│   (Go)       │ │   (Go)       │ │   (C++)      │
│              │ │              │ │              │
│Live orders   │ │Paper matching│ │Evolution     │
│→ Gate.io API │ │→ Redis price │ │analysis      │
│              │ │  snapshots   │ │→ pending     │
│              │ │              │ │  changes     │
└──────────────┘ └──────────────┘ └──────┬───────┘
       │                │                │
       └────────────────┼────────────────┘
                        ▼
               ┌────────────────┐
               │     Redis      │
               │  (message bus) │
               └────────────────┘
                        │
               ┌────────▼────────┐
               │   PostgreSQL    │
               │  (persistence)  │
               └─────────────────┘
```

### Container Topology

| Container | Language | Role |
|-----------|----------|------|
| `shark2` | Python 3.11 | Strategy brain, REST API, WebSocket, frontend |
| `shark-executor` | Go | Live trading — Gate.io order execution |
| `shark-matcher` | Go | Paper trading — order matching engine |
| `shark-evolver` | C++ | Evolution engine — trade analysis, parameter tuning |
| `redis` | — | Message bus and state cache |
| `db` | PostgreSQL 16 | Trade history, orders, balance logs |

### Data Flow

1. **Python** runs the main tick loop: fetches market data → AI committee analyzes → SignalEngine decides direction → publishes to `shark:orders:new`
2. **Go executor** subscribes to `shark:orders:new` (live mode only) → places orders on Gate.io
3. **Go matcher** subscribes to `shark:orders:new` (paper mode) → matches against Redis price snapshots
4. **C++ evolver** reads trade history from Redis → analyzes patterns → publishes `shark:evo:pending`
5. **Python** subscribes to `shark:evo:pending` → adds to approval queue → frontend UI for approve/reject

### Key Features

- **AI Committee**: DeepSeek + Qwen + Doubao multi-model signal validation
- **Dual Strategy**: Separate parameters for majors (BTC/ETH) vs high-volatility alts
- **Market Regime Detection**: 10 regime types with differentiated stop-loss/take-profit multipliers
- **Dynamic Risk**: ATR-based stop-loss (not fixed percentage), configurable margin allocation
- **Evolution Engine**: C++ analysis with frontend approval workflow
- **Paper/Live Isolation**: SHARK_MODE env var, manual toggle required for live trading
- **Device Lock**: MAC-address-based API authentication

---

## Quick Start

```bash
# 1. Configure environment
cp .env.example .env

# 2. Build and start all services
docker compose up -d --build

# 3. Open dashboard
open http://localhost:80
```

**Trading is OFF by default.** Click "开始交易" in the top bar to enable paper trading (5-tick warmup before first position).

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SHARK_MODE` | `paper` | `paper` or `live` |
| `GATE_API_KEY` | — | Gate.io API key (live mode) |
| `GATE_API_SECRET` | — | Gate.io API secret (live mode) |
| `SHARK_API_TOKEN` | required | Bearer token for REST/WS auth |
| `SHARK_REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `DATABASE_URL` | `postgresql://shark:shark@db:5432/shark` | PostgreSQL connection |
| `SHARK_HTTP_PORT` | `80` | HTTP listen port |
| `DEEPSEEK_API_KEY` | — | DeepSeek AI model (optional) |
| `QWEN_KEY` | — | Qwen AI model (optional) |
| `VOLC_KEY` | — | Doubao AI model (optional) |

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | — | Liveness check |
| GET | `/api/status` | Bearer | Full runtime snapshot |
| GET | `/api/paper/status` | Bearer | Paper trading state |
| POST | `/api/paper/toggle` | Bearer | Toggle paper trading |
| GET | `/api/live/status` | Bearer | Live trading state |
| POST | `/api/live/toggle` | Bearer | Toggle live trading |
| POST | `/api/shark/mode` | Bearer | Switch paper/live mode |
| GET | `/api/evo/pending` | Bearer | Pending evolution changes |
| POST | `/api/evo/approve/{id}` | Bearer | Approve evolution change |
| POST | `/api/evo/reject/{id}` | Bearer | Reject evolution change |
| GET | `/api/history` | Bearer | Paginated trade history |
| WS | `/ws` | `?token=` | Real-time state push (~1Hz) |

---

## Project Structure

```
Shark_Quantitative_Robot/
├── main.py                 # Single entrypoint (FastAPI + strategy loop)
├── signal_engine.py        # Signal decision (AI committee + fallback)
├── ai_strategy.py          # Multi-model AI analysis
├── ai_position.py          # AI position management
├── dual_strategy.py        # Dual-track capital allocation config
├── oscillation.py          # Oscillation detector
├── live_engine.py           # Gate.io live trading engine
├── market_regime.py        # Market regime detector (10 types)
├── trade_reflector.py      # Stop-loss reflection engine
├── online_learner.py       # Online learner (stub, C++ evolver handles)
├── kline_cache.py          # K-line data cache
├── multi_exchange.py       # Multi-exchange price aggregation
├── dialogue_ammo.py        # Character dialogue (123 lines)
├── character_voice.py      # LLM character voice generation
├── battle_report.py         # Battle report generator (cron)
├── backtest.py             # 3-day backtest utility
│
├── executor/               # Go live executor (Dockerfile.executor)
├── matcher/                # Go paper matcher (Dockerfile.matcher)
├── evolver/                # C++ evolution engine (Dockerfile)
│
├── persistence/            # SQLAlchemy + Redis bridge
│   ├── bridge.py
│   ├── dialogue_store.py
│   ├── repository.py
│   ├── session.py
│   ├── redis_rate_limit.py
│   └── models.py
│
├── observability/          # Middleware
│   ├── context.py          # Request ID correlation
│   └── device_lock.py      # MAC-based API auth
│
├── web/                    # React frontend (Vite + TypeScript)
│   └── src/
│       ├── App.tsx
│       ├── components/
│       └── store/
│
├── tests/
│   └── test_core.py        # 21 regression tests
│
├── alembic/                # Database migrations
├── tools/                  # Diagnostic utilities
├── adr/                    # Architecture Decision Records
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Development

```bash
# Python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py

# Frontend (hot reload)
cd web && npm ci && npm run dev

# Tests
docker exec shark2 python3 -m pytest tests/ -v
```

---

## License & Disclaimer

This software is for **education and research**. Cryptocurrency derivatives are highly volatile. Paper/simulated performance does not indicate live P&L. No warranty or investment advice provided.
