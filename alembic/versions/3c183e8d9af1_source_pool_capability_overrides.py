"""汇总源：可编辑的能力覆盖。

Revision ID: 3c183e8d9af1
Revises: fc29e05ce3a1

池是虚拟 provider，参考成员只提供「原值」，用户可在其上整维覆盖（与自定义供应商模型同一套
``capability_overrides`` 语义）。此列稀疏存储：键缺席即该维度跟随参考成员，出现即整维替换。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3c183e8d9af1"
down_revision: str | Sequence[str] | None = "fc29e05ce3a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("video_source_pool") as batch:
        batch.add_column(sa.Column("capability_overrides", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("video_source_pool") as batch:
        batch.drop_column("capability_overrides")
