#!/usr/bin/env python3
"""
Shark 许可证生成器（Redis 版本）。

生成一个随机 license token，存入 Redis `shark:license:active`，
同时记录元信息（用户、过期时间）。

用法:
  # 本地/dev（需要 Redis 可访问）
  python scripts/gen_license.py --user shark --expiry 2026-12-31

  # 服务器/Docker
  docker exec -it shark2 python scripts/gen_license.py --user shark --expiry 2026-12-31

  # 指定 Redis 地址
  REDIS_URL=redis://localhost:6379/0 python scripts/gen_license.py --user shark --expiry 2026-12-31

输出: 许可证 token，用户复制后在前端登录弹窗中输入。
"""

import argparse
import json
import os
import secrets
import sys
import time


def generate_token() -> str:
    return secrets.token_hex(32)


def main():
    parser = argparse.ArgumentParser(description="Shark 许可证生成器（Redis 版）")
    parser.add_argument("--user", required=True, help="用户标识")
    parser.add_argument("--expiry", required=True, help="过期日期，格式 YYYY-MM-DD")
    parser.add_argument("--redis-url", default="", help="Redis 连接地址（默认用 SHARK_REDIS_URL 环境变量或 redis://redis:6379/0）")
    args = parser.parse_args()

    redis_url = args.redis_url or os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0")

    token = generate_token()
    meta = {"user": args.user, "exp": args.expiry, "created_at": int(time.time())}

    try:
        import redis
        r = redis.from_url(redis_url, decode_responses=True)
        r.set("shark:license:active", token)
        r.set("shark:license:meta", json.dumps(meta))
        r.ping()
        print(f"✅ 已写入 Redis ({redis_url})")
    except Exception as e:
        print(f"❌ 无法连接 Redis ({redis_url}): {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"# Shark License — {args.user} (expires {args.expiry})")
    print(f"# 复制下面这行在前端登录弹窗中输入：")
    print(token)
    print()
    print("# 前端 js console 直接设置（跳过登录）：")
    print(f"localStorage.setItem('shark_license', '{token}')")


if __name__ == "__main__":
    main()
