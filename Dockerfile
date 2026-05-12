# ── Stage 1: Build React frontend ──
FROM node:20-alpine AS web-builder
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm install --legacy-peer-deps
COPY web/ ./
RUN npm run build

# ── Stage 2: Python runtime ──
FROM python:3.11-slim
WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY main.py .
COPY ai_strategy.py .
COPY dual_strategy.py .
COPY ai_position.py .
COPY oscillation.py .
COPY api/ ./api/
COPY multi_exchange.py .
COPY kline_cache.py .
COPY market_regime.py .
COPY trade_reflector.py .
COPY online_learner.py .
COPY live_engine.py .
COPY signal_engine.py .
COPY battle_report.py .
COPY tests/ ./tests/
COPY CONTEXT.md .
COPY ARCHITECTURE.md .
COPY dialogue_ammo.py .
COPY character_voice.py .
COPY observability/ ./observability/
COPY persistence/ ./persistence/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY scripts/ ./scripts/
COPY static/ ./static/

# Built frontend (from web-builder stage)
COPY --from=web-builder /app/web/dist ./web/dist

# Static assets (background images etc)
COPY web/public ./web/public

# 密钥与本地配置通过运行时注入（Compose env_file、K8s Secret、-e 等），禁止打入镜像层。

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:80/api/health || exit 1

CMD ["python", "main.py"]
