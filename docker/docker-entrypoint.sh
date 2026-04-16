#!/bin/sh
set -e
# 若挂载的 settings.yaml 不存在，用模板生成（首次 docker compose 前可免手动 cp）
if [ ! -f /app/config/settings.yaml ]; then
  echo "[entrypoint] config/settings.yaml 不存在，从 settings.model.yaml 复制"
  cp /app/config/settings.model.yaml /app/config/settings.yaml
fi
exec "$@"
