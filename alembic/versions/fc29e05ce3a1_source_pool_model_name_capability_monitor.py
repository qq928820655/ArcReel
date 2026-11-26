"""汇总源：统一模型名、能力参考成员与任务归属。

Revision ID: fc29e05ce3a1
Revises: d1a7f3c9e5b8

三处增量：

- ``video_source_pool.model_name``：对外展示的统一模型名（候选下拉与默认模型页），空则回退池名。
- ``video_source_pool.capability_provider_id`` / ``capability_model_id``：能力参考成员。池本身是虚拟
  provider，能力解析无从收敛，取此成员的能力回填；空则取 position=0 的首个成员。
- ``tasks.source_pool_id``：任务经哪个池分配。池展开时 ``provider_id`` 被覆盖成真实成员，池来源
  丢失，监控页据此按池聚合。仅池任务有值，单源任务为 NULL。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "fc29e05ce3a1"
down_revision: str | Sequence[str] | None = "d1a7f3c9e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("video_source_pool") as batch:
        batch.add_column(sa.Column("model_name", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("capability_provider_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("capability_model_id", sa.String(length=128), nullable=True))

    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("source_pool_id", sa.Integer(), nullable=True))
        batch.create_index("idx_tasks_source_pool_id", ["source_pool_id"])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("idx_tasks_source_pool_id")
        batch.drop_column("source_pool_id")

    with op.batch_alter_table("video_source_pool") as batch:
        batch.drop_column("capability_model_id")
        batch.drop_column("capability_provider_id")
        batch.drop_column("model_name")
