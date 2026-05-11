"""dialogue_lines ammo table

Revision ID: 0002_dialogue_lines
Revises: 0001_initial
Create Date: 2026-05-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_dialogue_lines"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dialogue_lines",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("line", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category", "line", name="uq_dialogue_lines_category_line"),
    )
    op.create_index("ix_dialogue_lines_created_at", "dialogue_lines", ["created_at"], unique=False)
    op.create_index("ix_dialogue_lines_category", "dialogue_lines", ["category"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_dialogue_lines_category", table_name="dialogue_lines")
    op.drop_index("ix_dialogue_lines_created_at", table_name="dialogue_lines")
    op.drop_table("dialogue_lines")
