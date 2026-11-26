"""视频汇总源与供应商可用态的读写。

见 ``docs/adr/0123-video-source-pool-as-virtual-provider.md``。写入口只做单事务的读-改-写，
不做业务判定（分配策略在 ``lib.config.source_pool`` 的纯函数里）。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.backends.providers import CallStatus
from lib.db.base import utc_now
from lib.db.models.api_call import ApiCall
from lib.db.models.source_pool import ProviderAvailabilityState, VideoSourcePool, VideoSourcePoolMember
from lib.db.models.task import Task
from lib.db.repositories.base import BaseRepository


def _pinned_model(payload_json: str | None) -> str | None:
    """从 payload 桶键取池展开时钉住的真实 model；取不到返回 None。"""
    if not payload_json:
        return None
    try:
        payload = json.loads(payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("video_provider_i2v", "video_provider_r2v"):
        pair = payload.get(key)
        if isinstance(pair, str) and "/" in pair:
            return pair.split("/", 1)[1]
    return None


class SourcePoolRepository(BaseRepository):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ---------------- 池与成员 ----------------

    async def list_pools(self) -> list[VideoSourcePool]:
        stmt = select(VideoSourcePool).order_by(VideoSourcePool.id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_pool(self, pool_id: int) -> VideoSourcePool | None:
        return await self.session.get(VideoSourcePool, pool_id)

    async def list_members(self, pool_id: int) -> list[VideoSourcePoolMember]:
        stmt = (
            select(VideoSourcePoolMember)
            .where(VideoSourcePoolMember.pool_id == pool_id)
            .order_by(VideoSourcePoolMember.position)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def capability_member(self, pool_id: int) -> tuple[str, str] | None:
        """池的能力参考成员 ``(provider_id, model_id)``：显式配置优先，否则取 position=0 的首个成员。

        池是虚拟 provider，能力解析无从收敛；执行期成员由 worker 按容量分配，与能力展示无关，
        故取一个稳定的参考成员代表整池的能力边界。返回 ``None`` 表示池不存在或没有成员。
        """
        pool = await self.get_pool(pool_id)
        if pool is None:
            return None
        if pool.capability_provider_id and pool.capability_model_id:
            return pool.capability_provider_id, pool.capability_model_id
        members = await self.list_members(pool_id)
        if not members:
            return None
        return members[0].provider_id, members[0].model_id

    async def capability_spec(self, pool_id: int) -> tuple[tuple[str, str], dict[str, object]] | None:
        """池的能力参考成员与池自身的能力覆盖 ``((provider_id, model_id), overrides)``。

        参考成员给原值，``overrides`` 是用户在池上整维覆盖的稀疏字典（键缺席即跟随成员）。
        池不存在或无成员时返回 ``None``。
        """
        pool = await self.get_pool(pool_id)
        if pool is None:
            return None
        member = await self.capability_member(pool_id)
        if member is None:
            return None
        overrides = pool.capability_overrides if isinstance(pool.capability_overrides, dict) else {}
        return member, overrides

    async def list_all_members(self) -> list[VideoSourcePoolMember]:
        """一次取回全部成员（worker 装载内存池表用，避免按池 N 次查询）。"""
        stmt = select(VideoSourcePoolMember).order_by(VideoSourcePoolMember.pool_id, VideoSourcePoolMember.position)
        return list((await self.session.execute(stmt)).scalars().all())

    async def create_pool(
        self,
        *,
        name: str,
        model_name: str | None = None,
        capability_provider_id: str | None = None,
        capability_model_id: str | None = None,
        capability_overrides: dict[str, object] | None = None,
        enabled: bool = True,
    ) -> VideoSourcePool:
        pool = VideoSourcePool(
            name=name,
            model_name=model_name,
            capability_provider_id=capability_provider_id,
            capability_model_id=capability_model_id,
            capability_overrides=capability_overrides,
            enabled=enabled,
        )
        self.session.add(pool)
        await self.session.flush()
        return pool

    async def replace_members(self, pool_id: int, members: list[tuple[str, str]]) -> None:
        """整表替换某池的成员：``members`` 是 ``[(provider_id, model_id), ...]``，顺序即 position。"""
        await self.session.execute(delete(VideoSourcePoolMember).where(VideoSourcePoolMember.pool_id == pool_id))
        now = utc_now()
        for position, (provider_id, model_id) in enumerate(members):
            self.session.add(
                VideoSourcePoolMember(
                    pool_id=pool_id,
                    position=position,
                    provider_id=provider_id,
                    model_id=model_id,
                    created_at=now,
                )
            )
        await self.session.flush()

    async def delete_pool(self, pool_id: int) -> None:
        # 显式删成员再删池：不依赖数据库的 FK CASCADE（SQLite 默认不强制外键，且不同后端行为不一），
        # 保证删池后不残留孤儿成员行。
        await self.session.execute(delete(VideoSourcePoolMember).where(VideoSourcePoolMember.pool_id == pool_id))
        await self.session.execute(delete(VideoSourcePool).where(VideoSourcePool.id == pool_id))

    # ---------------- 监控：任务分配与调用情况 ----------------

    async def list_pool_tasks(
        self,
        *,
        pool_id: int | None = None,
        status: str | None = None,
        project_name: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """分页取「经汇总源分配」的任务，附池名、真实成员与实际调用聚合。

        一条池任务在 ``pin_pool_execution_identity`` 时被记下 ``source_pool_id`` 与真实成员
        ``provider_id``；执行期可能因重试产生多条 ``api_calls``，这里按 ``task_id`` 聚合出
        调用次数、成功/失败数与最近一次错误，供监控页回答「分到哪个源、调了几次、成没成、多久」。
        """
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        offset = (page - 1) * page_size

        filters: list[ColumnElement[bool]] = [Task.source_pool_id.is_not(None)]
        if pool_id is not None:
            filters.append(Task.source_pool_id == pool_id)
        if status:
            filters.append(Task.status == status)
        if project_name:
            filters.append(Task.project_name == project_name)

        count_stmt = select(func.count()).select_from(Task).where(*filters)
        total = (await self.session.execute(self._scope_query(count_stmt, Task))).scalar() or 0

        # 先取当页任务（含池名），再只对这几个 task_id 聚合调用——绝不对全表 api_calls 做 group by，
        # 否则大库上每次翻页都是全表扫。调用聚合限定在 ≤page_size 个 task_id 上，走 idx_api_calls_task_id。
        items_stmt = (
            select(Task, VideoSourcePool.name.label("pool_name"))
            .outerjoin(VideoSourcePool, VideoSourcePool.id == Task.source_pool_id)
            .where(*filters)
            .order_by(Task.updated_at.desc(), Task.queued_at.desc())
            .limit(page_size)
            .offset(offset)
        )
        rows = (await self.session.execute(self._scope_query(items_stmt, Task))).all()

        task_ids = [task.task_id for task, _ in rows]
        aggregates = await self._call_aggregates(task_ids)
        items = [
            self._pool_task_row(task, pool_name, aggregates.get(task.task_id))
            for task, pool_name in rows
        ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def _call_aggregates(self, task_ids: list[str]) -> dict[str, tuple[int, int, int, object]]:
        """对给定 task_id 集合聚合调用：``{task_id: (总, 成功, 失败, 最近结束时刻)}``。

        空集合直接返回，不发查询；否则 ``WHERE task_id IN (...)`` 走索引，只碰这几条任务的调用行。
        """
        if not task_ids:
            return {}
        stmt = (
            select(
                ApiCall.task_id,
                func.count(ApiCall.id),
                func.sum(case((ApiCall.status == CallStatus.SUCCESS, 1), else_=0)),
                func.sum(case((ApiCall.status == CallStatus.FAILED, 1), else_=0)),
                func.max(ApiCall.finished_at),
            )
            .where(ApiCall.task_id.in_(task_ids))
            .group_by(ApiCall.task_id)
        )
        result = await self.session.execute(stmt)
        return {
            task_id: (int(total or 0), int(success or 0), int(failed or 0), last_finished)
            for task_id, total, success, failed, last_finished in result.all()
        }

    @staticmethod
    def _pool_task_row(
        task: Task,
        pool_name: str | None,
        aggregate: tuple[int, int, int, object] | None,
    ) -> dict[str, Any]:
        call_count, success_count, failed_count, last_call_finished_at = aggregate or (0, 0, 0, None)
        return {
            "task_id": task.task_id,
            "project_name": task.project_name,
            "task_type": task.task_type,
            "resource_id": task.resource_id,
            "source_pool_id": task.source_pool_id,
            "pool_name": pool_name,
            # 真实成员：池展开后写入 provider_id 列，即实际执行的供应商；模型取自 payload 桶键。
            "provider_id": task.provider_id,
            "model_id": _pinned_model(task.payload_json),
            "status": task.status,
            "error_message": task.error_message,
            "queued_at": task.queued_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "call_count": call_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "last_call_finished_at": last_call_finished_at,
        }

    # ---------------- 供应商可用态 ----------------

    async def list_availability(self, media_type: str) -> list[ProviderAvailabilityState]:
        stmt = select(ProviderAvailabilityState).where(ProviderAvailabilityState.media_type == media_type)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_availability(self, *, provider_id: str, media_type: str) -> ProviderAvailabilityState | None:
        return await self.session.get(ProviderAvailabilityState, (provider_id, media_type))

    async def upsert_availability(
        self,
        *,
        provider_id: str,
        media_type: str,
        quota_streak: int,
        network_streak: int,
        last_reset_on: date | None,
    ) -> None:
        row = await self.get_availability(provider_id=provider_id, media_type=media_type)
        if row is None:
            self.session.add(
                ProviderAvailabilityState(
                    provider_id=provider_id,
                    media_type=media_type,
                    quota_streak=quota_streak,
                    network_streak=network_streak,
                    last_reset_on=last_reset_on,
                )
            )
        else:
            row.quota_streak = quota_streak
            row.network_streak = network_streak
            row.last_reset_on = last_reset_on
        await self.session.flush()

    async def reset_availability_before(self, *, media_type: str, today: date) -> int:
        """把 ``last_reset_on`` 早于今天的行清零（每日重置）。返回受影响行数。"""
        rows = await self.list_availability(media_type)
        count = 0
        for row in rows:
            if (row.last_reset_on is None or row.last_reset_on < today) and (
                row.quota_streak or row.network_streak or row.last_reset_on != today
            ):
                row.quota_streak = 0
                row.network_streak = 0
                row.last_reset_on = today
                count += 1
        if count:
            await self.session.flush()
        return count
