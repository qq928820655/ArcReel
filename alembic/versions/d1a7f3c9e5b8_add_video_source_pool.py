"""视频汇总源：池、成员与成员可用态三张表。

Revision ID: d1a7f3c9e5b8
Revises: c8e4f1a6b2d5

汇总源以虚拟 provider ``pool-<id>`` 对外，成员指向已配置的视频模型；worker 认领时按容量展开。
成员可用态与 ``source_priority_state`` 同语义（额度/网络 streak + 每日重置），独立成表避免回写配置。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1a7f3c9e5b8"
down_revision: str | Sequence[str] | None = "c8e4f1a6b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "video_source_pool",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "video_source_pool_member",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pool_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pool_id"], ["video_source_pool.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pool_id", "position", name="uq_video_source_pool_member_position"),
    )
    op.create_index("idx_video_source_pool_member_pool", "video_source_pool_member", ["pool_id"])
    op.create_table(
        "provider_availability_state",
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("quota_streak", sa.Integer(), nullable=False),
        sa.Column("network_streak", sa.Integer(), nullable=False),
        sa.Column("last_reset_on", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_id", "media_type"),
    )


def downgrade() -> None:
    op.drop_table("provider_availability_state")
    op.drop_index("idx_video_source_pool_member_pool", table_name="video_source_pool_member")
    op.drop_table("video_source_pool_member")
    op.drop_table("video_source_pool")
