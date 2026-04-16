#!/usr/bin/env bash
# 在项目根执行：对方把机器指纹 hex 发你，你本地一键生成 license.key
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 tools/issue_license.py "$@"
