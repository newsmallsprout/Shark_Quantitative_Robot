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
COPY ai/ ./ai/
COPY engine/ ./engine/
COPY api/ ./api/
COPY strategies/ ./strategies/

# Built frontend (from web-builder stage)
COPY --from=web-builder /app/web/dist ./web/dist

# Static assets (background images etc)
COPY web/public ./web/public

# Config
COPY .env .

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:80/api/health || exit 1

CMD ["python", "main.py"]
