"""台词弹药 Postgres 仓储：同步连接（psycopg2），供策略线程内 pop_line 随机读取。"""

from __future__ import annotations

import os
from typing import Dict, List, Mapping, Optional, Sequence

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from persistence.models import DialogueLine


def resolve_sync_psycopg_url(raw: Optional[str] = None) -> Optional[str]:
    u = (raw or os.environ.get("DATABASE_URL") or "").strip()
    if not u:
        return None
    if u.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg2://" + u[len("postgresql+asyncpg://") :]
    if u.startswith("postgresql://"):
        return "postgresql+psycopg2://" + u[len("postgresql://") :]
    if u.startswith("postgres://"):
        return "postgresql+psycopg2://" + u[len("postgres://") :]
    return u


class DialogueStore:
    def __init__(self, url: Optional[str]) -> None:
        self._engine = create_engine(url, pool_pre_ping=True) if url else None
        self._session_factory = (
            sessionmaker(bind=self._engine, expire_on_commit=False, autoflush=False)
            if self._engine
            else None
        )

    def enabled(self) -> bool:
        return self._session_factory is not None

    def count_for_category(self, category: str) -> int:
        if not self._session_factory:
            return 0
        with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(DialogueLine)
                .where(DialogueLine.category == category)
            )
            return int(session.scalar(stmt) or 0)

    def counts(self) -> Dict[str, int]:
        if not self._session_factory:
            return {}
        with self._session_factory() as session:
            stmt = select(DialogueLine.category, func.count()).group_by(DialogueLine.category)
            rows = session.execute(stmt).all()
        return {str(c): int(n) for c, n in rows}

    def random_line(self, category: str) -> Optional[str]:
        if not self._session_factory:
            return None
        with self._session_factory() as session:
            stmt = (
                select(DialogueLine.line)
                .where(DialogueLine.category == category)
                .order_by(func.random())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            return str(row).strip() if row else None

    def insert_unique(self, category: str, line: str) -> bool:
        """插入一条；若 (category,line) 已存在则跳过。返回是否新插入。"""
        if not self._session_factory or not line:
            return False
        stmt = (
            insert(DialogueLine)
            .values(category=category, line=line)
            .on_conflict_do_nothing(constraint="uq_dialogue_lines_category_line")
        )
        with self._session_factory() as session:
            with session.begin():
                res = session.execute(stmt)
                rc = getattr(res, "rowcount", None)
                return rc is not None and rc > 0

    def ingest_from_batch(
        self, categories: Sequence[str], obj: Mapping[str, object]
    ) -> int:
        """从 LLM JSON 批量入库（去重）。"""
        if not self._session_factory:
            return 0
        added = 0
        with self._session_factory() as session:
            with session.begin():
                for cat in categories:
                    raw = obj.get(cat)
                    if not isinstance(raw, list):
                        continue
                    for item in raw:
                        line = str(item).strip()[:64]
                        if not line:
                            continue
                        stmt = (
                            insert(DialogueLine)
                            .values(category=cat, line=line)
                            .on_conflict_do_nothing(constraint="uq_dialogue_lines_category_line")
                        )
                        r = session.execute(stmt)
                        rc = getattr(r, "rowcount", None)
                        if rc is not None and rc > 0:
                            added += 1
        return added

    def seed_category_if_empty(self, category: str, lines: List[str]) -> int:
        if not self._session_factory:
            return 0
        if self.count_for_category(category) > 0:
            return 0
        added = 0
        with self._session_factory() as session:
            with session.begin():
                for raw in lines:
                    line = str(raw).strip()[:64]
                    if not line:
                        continue
                    stmt = (
                        insert(DialogueLine)
                        .values(category=category, line=line)
                        .on_conflict_do_nothing(constraint="uq_dialogue_lines_category_line")
                    )
                    r = session.execute(stmt)
                    rc = getattr(r, "rowcount", None)
                    if rc is not None and rc > 0:
                        added += 1
        return added
