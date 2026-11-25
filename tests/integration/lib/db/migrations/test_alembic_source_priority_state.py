"""Alembic 迁移：新建多源优先级运行态表，降级时删除。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command


def _tables(db_path: Path) -> list[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        return sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_source_priority_state_table_created_and_dropped(
    alembic_cfg: tuple[Config, Path], migration_revisions: Callable[[str], tuple[str, str]]
):
    cfg, db_path = alembic_cfg
    revision, parent = migration_revisions("*_add_source_priority_state.py")

    command.upgrade(cfg, revision)
    assert "source_priority_state" in _tables(db_path)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO source_priority_state "
                    "(scope, project_name, media_type, active_index, quota_streak, last_reset_on, updated_at) "
                    "VALUES ('global', '', 'video', 0, 0, NULL, '2026-11-24')"
                )
            )
            row = conn.execute(
                sa.text("SELECT scope, media_type, active_index, quota_streak FROM source_priority_state")
            ).one()
        assert tuple(row) == ("global", "video", 0, 0)
    finally:
        engine.dispose()

    command.downgrade(cfg, parent)
    assert "source_priority_state" not in _tables(db_path)


def test_network_streak_column_added_and_dropped(
    alembic_cfg: tuple[Config, Path], migration_revisions: Callable[[str], tuple[str, str]]
):
    """网络不可达计数列：升级到本迁移后存在，降级回父版本后消失。"""
    cfg, db_path = alembic_cfg
    revision, parent = migration_revisions("*_add_source_priority_network_streak.py")

    command.upgrade(cfg, revision)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        cols = {c["name"] for c in sa.inspect(engine).get_columns("source_priority_state")}
    finally:
        engine.dispose()
    assert "network_streak" in cols

    command.downgrade(cfg, parent)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        cols = {c["name"] for c in sa.inspect(engine).get_columns("source_priority_state")}
    finally:
        engine.dispose()
    assert "network_streak" not in cols
