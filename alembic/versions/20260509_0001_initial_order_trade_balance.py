"""initial orders trades balance_logs

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("size", sa.Numeric(24, 10), nullable=False),
        sa.Column("margin_usd", sa.Numeric(24, 10), nullable=False),
        sa.Column("leverage", sa.Numeric(12, 4), nullable=False),
        sa.Column("fee_open_usd", sa.Numeric(24, 10), nullable=False),
        sa.Column("opened_at", sa.Numeric(20, 6), nullable=False),
        sa.Column("closed_at", sa.Numeric(20, 6), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_symbol", "orders", ["symbol"], unique=False)
    op.create_index("ix_orders_status", "orders", ["status"], unique=False)

    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("exit_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("size", sa.Numeric(24, 10), nullable=False),
        sa.Column("leverage", sa.Numeric(12, 4), nullable=False),
        sa.Column("margin_usd", sa.Numeric(24, 10), nullable=False),
        sa.Column("gross_pnl_usd", sa.Numeric(24, 10), nullable=False),
        sa.Column("fee_open_usd", sa.Numeric(24, 10), nullable=False),
        sa.Column("fee_close_usd", sa.Numeric(24, 10), nullable=False),
        sa.Column("realized_pnl_usd", sa.Numeric(24, 10), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(14, 6), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.Numeric(20, 6), nullable=False),
        sa.Column("closed_at", sa.Numeric(20, 6), nullable=False),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trades_order_id", "trades", ["order_id"], unique=False)
    op.create_index("ix_trades_symbol", "trades", ["symbol"], unique=False)

    op.create_table(
        "balance_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("delta_free_cash", sa.Numeric(24, 10), nullable=False),
        sa.Column("free_cash_after", sa.Numeric(24, 10), nullable=False),
        sa.Column("total_balance_after", sa.Numeric(24, 10), nullable=False),
        sa.Column("equity_after", sa.Numeric(24, 10), nullable=False),
        sa.Column("margin_locked_after", sa.Numeric(24, 10), nullable=False),
        sa.Column("unrealized_after", sa.Numeric(24, 10), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trade_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_balance_logs_created_at", "balance_logs", ["created_at"], unique=False)
    op.create_index("ix_balance_logs_event_type", "balance_logs", ["event_type"], unique=False)
    op.create_index("ix_balance_logs_order_id", "balance_logs", ["order_id"], unique=False)
    op.create_index("ix_balance_logs_trade_id", "balance_logs", ["trade_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_balance_logs_trade_id", table_name="balance_logs")
    op.drop_index("ix_balance_logs_order_id", table_name="balance_logs")
    op.drop_index("ix_balance_logs_event_type", table_name="balance_logs")
    op.drop_index("ix_balance_logs_created_at", table_name="balance_logs")
    op.drop_table("balance_logs")
    op.drop_index("ix_trades_symbol", table_name="trades")
    op.drop_index("ix_trades_order_id", table_name="trades")
    op.drop_table("trades")
    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_index("ix_orders_symbol", table_name="orders")
    op.drop_table("orders")
