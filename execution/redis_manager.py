"""Redis 连接管理器：自动重连 + 优雅降级"""
import time
import redis as sync_redis
from typing import Optional


class RedisManager:
    """Redis 同步客户端封装，断连自动重试，所有操作不抛异常"""

    def __init__(self, url: str = "redis://redis:6379/0", max_retries: int = 3):
        self._url = url
        self._max_retries = max_retries
        self._client: Optional[sync_redis.Redis] = None
        self._last_error = ""
        self._connect()

    def _connect(self) -> bool:
        try:
            self._client = sync_redis.from_url(self._url, decode_responses=True,
                                                socket_connect_timeout=3,
                                                socket_timeout=3,
                                                retry_on_timeout=True)
            self._client.ping()
            self._last_error = ""
            return True
        except Exception as e:
            self._last_error = str(e)[:80]
            self._client = None
            return False

    @property
    def ok(self) -> bool:
        return self._client is not None

    @property
    def error(self) -> str:
        return self._last_error

    def _ensure(self) -> Optional[sync_redis.Redis]:
        """确保连接可用，失败返回None"""
        if self._client:
            try:
                self._client.ping()
                return self._client
            except Exception:
                pass
        for _ in range(self._max_retries):
            if self._connect():
                return self._client
            time.sleep(1)
        return None

    def get(self, key: str) -> Optional[str]:
        r = self._ensure()
        if not r:
            return None
        try:
            return r.get(key)
        except Exception:
            return None

    def set(self, key: str, value, ex: int = None) -> bool:
        r = self._ensure()
        if not r:
            return False
        try:
            return bool(r.set(key, value, ex=ex))
        except Exception:
            return False

    def delete(self, *keys) -> bool:
        r = self._ensure()
        if not r:
            return False
        try:
            return bool(r.delete(*keys))
        except Exception:
            return False

    def publish(self, channel: str, message: str) -> bool:
        r = self._ensure()
        if not r:
            return False
        try:
            return bool(r.publish(channel, message))
        except Exception:
            return False

    def scan_iter(self, match: str, count: int = 100):
        r = self._ensure()
        if not r:
            return iter([])
        try:
            return r.scan_iter(match=match, count=count)
        except Exception:
            return iter([])

    def keys(self, pattern: str):
        r = self._ensure()
        if not r:
            return []
        try:
            return r.keys(pattern)
        except Exception:
            return []
