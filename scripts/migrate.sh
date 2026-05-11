#!/usr/bin/env sh
# 在仓库根目录执行 Postgres 结构迁移（需 DATABASE_URL 与已安装 alembic/psycopg2）
set -e
cd "$(dirname "$0")/.."
export DATABASE_URL="${DATABASE_URL:?set DATABASE_URL e.g. postgresql://shark:shark@localhost:5432/shark}"
exec alembic upgrade head
