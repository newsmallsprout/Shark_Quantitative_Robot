"""Async DB 会话工厂与 URL 解析。"""

from __future__ import annotations

import os
from typing import Any, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def resolve_database_url(raw: Optional[str] = None) -> Optional[str]:
    """
    返回 async SQLAlchemy URL（postgresql+asyncpg）。
    若未配置 DATABASE_URL 则返回 None。
    """
    url = (raw or os.environ.get("DATABASE_URL", "")).strip()
    if not url:
        return None
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


def create_engine_and_sessionmaker(
    database_url: Optional[str] = None,
) -> Tuple[Optional[AsyncEngine], Any]:
    url = resolve_database_url(database_url)
    if not url:
        return None, None
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, factory
