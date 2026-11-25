"""多源优先级每日重置：启动补偿、循环调度、失败不外抛。"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.config import source_priority_reset
from lib.config.source_priority import SourcePriorityStore


async def _move_to_second_row(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        store = SourcePriorityStore(session)
        await store.apply_outcome(
            scope="project",
            project_name="p",
            media_type="video",
            entry_count=3,
            quota_exhausted=True,
            succeeded=False,
        )
        await store.apply_outcome(
            scope="project",
            project_name="p",
            media_type="video",
            entry_count=3,
            quota_exhausted=True,
            succeeded=False,
        )
        await session.commit()


async def _read_index(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        row = await SourcePriorityStore(session).read_state(scope="project", project_name="p", media_type="video")
        return row.active_index


async def test_reset_due_returns_rows_back_to_the_first_entry(file_db_factory):
    await _move_to_second_row(file_db_factory)
    assert await _read_index(file_db_factory) == 1

    changed = await source_priority_reset.reset_due(file_db_factory)

    assert changed == 1
    assert await _read_index(file_db_factory) == 0


async def test_reset_due_is_idempotent_within_the_same_day(file_db_factory):
    await _move_to_second_row(file_db_factory)

    assert await source_priority_reset.reset_due(file_db_factory) == 1
    assert await source_priority_reset.reset_due(file_db_factory) == 0


async def test_reset_due_marks_the_beijing_date(file_db_factory):
    await _move_to_second_row(file_db_factory)

    # 北京 2026-11-25 01:00 对应 UTC 前一天 17:00：标记按北京日期落，不按 UTC 日期。
    moment = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)
    await source_priority_reset.reset_due(file_db_factory, now=moment)

    async with file_db_factory() as session:
        row = await SourcePriorityStore(session).read_state(scope="project", project_name="p", media_type="video")
    assert row.last_reset_on == date(2026, 11, 25)


async def test_reset_due_swallows_database_errors(db_session: AsyncSession):
    """重置失败不该拖垮调用方：循环照常进入下一轮，启动也不因此中止。"""

    class _BrokenFactory:
        def __call__(self):
            raise RuntimeError("db unavailable")

    assert await source_priority_reset.reset_due(_BrokenFactory()) == 0


async def test_start_runs_startup_catch_up_before_waiting(file_db_factory, monkeypatch):
    """启动补偿：进程停机跨过零点后，起来立刻把停在下移行的状态推回首行。"""
    await _move_to_second_row(file_db_factory)

    slept: list[float] = []

    async def _record_sleep(delay: float) -> None:
        slept.append(delay)
        # 第一次睡眠即结束循环，避免测试挂住。
        raise RuntimeError("stop loop")

    monkeypatch.setattr(source_priority_reset.asyncio, "sleep", _record_sleep)

    task = source_priority_reset.start_source_priority_reset(file_db_factory)
    with contextlib.suppress(RuntimeError):
        await task

    assert await _read_index(file_db_factory) == 0
    assert slept, "循环应当在补偿之后进入等待"


async def test_loop_sleeps_until_the_next_beijing_midnight(file_db_factory, monkeypatch):
    """等待时长取自「下一个北京零点 - 现在」，而不是固定 24 小时。"""
    slept: list[float] = []

    async def _record_sleep(delay: float) -> None:
        slept.append(delay)
        raise RuntimeError("stop loop")

    monkeypatch.setattr(source_priority_reset.asyncio, "sleep", _record_sleep)
    # 固定在 UTC 15:00（北京 23:00）：距下一个北京零点还有 1 小时。
    frozen = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(source_priority_reset, "datetime", _FrozenDatetime)
    monkeypatch.setattr(source_priority_reset, "next_beijing_midnight", lambda now=None: frozen + timedelta(hours=1))

    with contextlib.suppress(RuntimeError):
        await source_priority_reset._reset_loop(file_db_factory)

    assert slept
    assert abs(slept[0] - 3600.0) < 1.0
