# Build Frontend（生产令牌在构建时注入 VITE_*）
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
ARG VITE_SHARK_API_TOKEN=""
ENV VITE_SHARK_API_TOKEN=$VITE_SHARK_API_TOKEN
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Backend
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    libzmq3-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY backtest_runner.py .
COPY backtest_worker.py .
COPY event_replay_main.py .
COPY src/ ./src/
COPY config/ ./config/
# 仅公钥入镜像；私钥与 license.key 由运行时卷挂载（见 .dockerignore）
RUN mkdir -p /app/license
COPY license/public.pem /app/license/public.pem

COPY --from=frontend-builder /app/frontend/dist/ ./src/web/

COPY docker/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

RUN mkdir -p logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8002
EXPOSE 5555

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python3", "main.py"]
