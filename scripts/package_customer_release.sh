#!/usr/bin/env bash
# 将商业混淆包打成 zip，便于 GitHub Releases 上传
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/dist/commercial_obfuscated"
if [[ ! -d "$SRC" ]]; then
  echo "请先运行: python scripts/build_commercial_release.py" >&2
  exit 1
fi
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${ROOT}/dist/shark-quant-commercial-${STAMP}.zip"
mkdir -p "${ROOT}/dist"
( cd "${ROOT}/dist" && zip -r "shark-quant-commercial-${STAMP}.zip" "$(basename "$SRC")" )
echo "已生成: ${ROOT}/dist/shark-quant-commercial-${STAMP}.zip"
