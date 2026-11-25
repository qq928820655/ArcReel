"""多源优先级的每日重置：北京零点把所有运行态行推回首行。

需求里的「每日重置」是**日期粒度**的幂等动作，不是「每隔 24 小时」。进程不保证零点在线，
故两条路径都走同一个 ``reset_due``：

- 启动时先补一次：停机跨过了零点，运行态仍停在下移后的行，此时应当已经回到首行。
- 常驻循环等到下一个北京零点再跑，之后每天一次。

``reset_if_due`` 以 ``last_reset_on`` 与今天的日期比较，同一天重复调用不会重复推进，
所以启动补偿与循环即便落在同一秒也不会互相干扰。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.config.source_priority import SourcePriorityStore, beijing_today, next_beijing_midnight

logger = logging.getLogger(__name__)


async def reset_due(session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None) -> int:
    """把到期的运行态行重置回首行，并把供应商可用态清零，返回受影响行数。失败只记日志，不抛给调用方。"""
    today = beijing_today(now)
    try:
        async with session_factory() as session:
            changed = await SourcePriorityStore(session).reset_if_due(today=today)
            await session.commit()
        # 供应商可用态（汇总源成员分配用）同一天粒度清零：额度/网络 streak 归零、last_reset_on 推到今天。
        from lib.db.repositories.source_pool_repo import SourcePoolRepository

        async with session_factory() as session:
            await SourcePoolRepository(session).reset_availability_before(media_type="video", today=today)
            await session.commit()
        return changed
    except Exception:
        logger.warning("多源优先级每日重置失败", exc_info=True)
        return 0


async def _reset_loop(session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        # 先算下一个零点再睡：睡眠时长按当前时刻现算，跨天不会累积漂移。
        delay = (next_beijing_midnight() - datetime.now(UTC)).total_seconds()
        await asyncio.sleep(max(delay, 1.0))
        await reset_due(session_factory)


def start_source_priority_reset(
    session_factory: async_sessionmaker[AsyncSession],
) -> asyncio.Task[None]:
    """启动每日重置循环；循环本身先补一次启动补偿，再等零点。"""

    async def _run() -> None:
        await reset_due(session_factory)
        await _reset_loop(session_factory)

    return asyncio.get_running_loop().create_task(_run(), name="source-priority-reset")


async def stop_source_priority_reset(task: asyncio.Task[None] | None) -> None:
    """取消重置循环，等它收尾完再返回。"""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
