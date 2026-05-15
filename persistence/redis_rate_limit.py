"""基于 Redis INCR 的固定窗口限流（异步）。"""

from __future__ import annotations

import time

import redis.asyncio as redis


async def fixed_window_allow(
    client: redis.Redis,
    *,
    name: str,
    limit: int,
    window_sec: int,
    key_prefix: str = "shark:rl",
) -> bool:
    """在 window_sec 窗口内最多允许 limit 次。返回 True 表示准许当前请求。"""
    if limit <= 0:
        return True
    w = max(int(window_sec), 1)
    bucket = int(time.time()) // w
    key = f"{key_prefix}:{name}:{bucket}"
    n = await client.incr(key)
    if n == 1:
        await client.expire(key, w + 1)
    return n <= limit
