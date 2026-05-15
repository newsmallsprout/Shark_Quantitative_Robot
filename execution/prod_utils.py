"""生产可靠性工具：启动等待、健康检查、状态快照"""
import time
import os
import json
from typing import Optional


def wait_for_redis(redis_url: str, timeout: int = 60) -> bool:
    """阻塞等待 Redis 就绪，超时返回 False"""
    import redis as sync_redis
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = sync_redis.from_url(redis_url, decode_responses=True,
                                     socket_connect_timeout=3, socket_timeout=3)
            if r.ping():
                print(f"[启动] Redis 就绪 ({redis_url})", flush=True)
                return True
        except Exception:
            pass
        time.sleep(2)
    print(f"[启动] Redis 连接超时 ({timeout}s)", flush=True)
    return False


def wait_for_postgres(pg_url: str, timeout: int = 30) -> bool:
    """阻塞等待 Postgres 就绪"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import psycopg2
            conn = psycopg2.connect(pg_url, connect_timeout=3)
            conn.close()
            print("[启动] Postgres 就绪", flush=True)
            return True
        except Exception:
            pass
        time.sleep(2)
    print(f"[启动] Postgres 连接超时 ({timeout}s)，继续无 DB 模式", flush=True)
    return False


def build_health_check(plan_gate=None, positions=None) -> dict:
    """构建健康检查响应"""
    health = {
        "status": "ok",
        "ts": time.time(),
        "redis": "disconnected",
        "plans": 0,
        "positions": 0,
    }
    if plan_gate:
        try:
            plans = plan_gate.get_all_plans()
            health["plans"] = len(plans)
            health["redis"] = "connected"
        except Exception:
            health["redis"] = "error"
    if positions is not None:
        health["positions"] = positions if isinstance(positions, int) else len(positions)
    return health


def save_snapshot(state: dict, path: str = "/app/data/snapshot.json"):
    """保存关键状态到磁盘（崩溃恢复用）"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        snapshot = {
            "ts": time.time(),
            "balance": state.get("balance"),
            "equity": state.get("equity"),
            "initial_capital": state.get("initial_capital"),
            "realized_pnl": state.get("realized_pnl"),
            "gross_realized": state.get("gross_realized"),
            "total_fees": state.get("total_fees"),
            "positions_count": state.get("positions"),
        }
        with open(path, "w") as f:
            json.dump(snapshot, f)
    except Exception:
        pass


def load_snapshot(path: str = "/app/data/snapshot.json") -> Optional[dict]:
    """从磁盘恢复上次状态"""
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return None
