#!/usr/bin/env bash
# 给客户：无需 license 即可打印机器指纹（用于发给你签发）。
# 在项目根执行；若在 Docker 里跑，指纹与宿主机可能不同，请以实际运行环境为准。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v docker >/dev/null 2>&1 && [ -f "${ROOT}/docker-compose.yml" ]; then
  if docker compose run --help >/dev/null 2>&1; then
    echo "=== Docker 容器内指纹（与 compose 运行环境一致）===" >&2
    exec docker compose run --rm --no-deps --entrypoint python3 shark-quant -m src.license_manager.fingerprint
  fi
fi

echo "=== 本机 Python 指纹（未使用 Docker；请与最终部署方式一致）===" >&2
export PYTHONPATH="${ROOT}"
exec python3 -m src.license_manager.fingerprint
