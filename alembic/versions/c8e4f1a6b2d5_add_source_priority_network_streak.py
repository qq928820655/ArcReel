"""多源优先级运行态加网络不可达计数。

Revision ID: c8e4f1a6b2d5
Revises: b7c1d9e4a2f3

切换的触发从「仅额度耗尽」扩到「额度耗尽或网络不可达」。两类失败分开计数、互不干扰，
故运行态表多一列 ``network_streak``。存量行补 0，不迁数据。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8e4f1a6b2d5"
down_revision: str | Sequence[str] | None = "b7c1d9e4a2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_priority_state",
        sa.Column("network_streak", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("source_priority_state", "network_streak")
