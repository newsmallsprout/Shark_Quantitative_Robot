# Build Frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Build Backend
FROM python:3.11-slim
WORKDIR /app

# Install system dependencies required for compilation (e.g., for ZeroMQ, cryptography)
RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    libzmq3-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source code
COPY main.py .
COPY backtest_runner.py .
COPY backtest_worker.py .
COPY event_replay_main.py .
COPY src/ ./src/
COPY config/ ./config/
COPY license/ ./license/

# Copy built frontend assets into the backend's static directory
COPY --from=frontend-builder /app/frontend/dist/ ./src/web/

# Ensure logs directory exists
RUN mkdir -p logs

# Expose FastAPI port
EXPOSE 8002
# Expose ZeroMQ internal port (optional, useful for debugging if needed outside)
EXPOSE 5555

# Define environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Command to run the bot
CMD ["python3", "main.py"]
