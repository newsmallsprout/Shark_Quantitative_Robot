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
COPY core/ ./core/
COPY api/ ./api/
COPY market/ ./market/
COPY strategy/ ./strategy/
COPY character/ ./character/
COPY learning/ ./learning/
COPY utils/ ./utils/
COPY execution/ ./execution/
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
COPY web/video ./web/video

# 密钥与本地配置通过运行时注入（Compose env_file、K8s Secret、-e 等），禁止打入镜像层。

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:80/health || exit 1

CMD ["python", "main.py"]
