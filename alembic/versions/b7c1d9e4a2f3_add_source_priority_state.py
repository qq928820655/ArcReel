"""多源优先级的运行态表。

Revision ID: b7c1d9e4a2f3
Revises: f6d2a8c4b931

只建表、不迁数据：开关与有序列表存在既有设置通道里，这张表只承载「当前生效到第几行」。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c1d9e4a2f3"
down_revision: str | Sequence[str] | None = "f6d2a8c4b931"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_priority_state",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("project_name", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("active_index", sa.Integer(), nullable=False),
        sa.Column("quota_streak", sa.Integer(), nullable=False),
        sa.Column("last_reset_on", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "project_name", "media_type"),
    )
    op.create_index(
        "idx_source_priority_state_media_type",
        "source_priority_state",
        ["media_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_source_priority_state_media_type", table_name="source_priority_state")
    op.drop_table("source_priority_state")
