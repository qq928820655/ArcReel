"""视频汇总源与供应商可用态的读写。

见 ``docs/adr/0123-video-source-pool-as-virtual-provider.md``。写入口只做单事务的读-改-写，
不做业务判定（分配策略在 ``lib.config.source_pool`` 的纯函数里）。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.base import utc_now
from lib.db.models.source_pool import ProviderAvailabilityState, VideoSourcePool, VideoSourcePoolMember
from lib.db.repositories.base import BaseRepository


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

    async def list_all_members(self) -> list[VideoSourcePoolMember]:
        """一次取回全部成员（worker 装载内存池表用，避免按池 N 次查询）。"""
        stmt = select(VideoSourcePoolMember).order_by(VideoSourcePoolMember.pool_id, VideoSourcePoolMember.position)
        return list((await self.session.execute(stmt)).scalars().all())

    async def create_pool(self, *, name: str, enabled: bool = True) -> VideoSourcePool:
        pool = VideoSourcePool(name=name, enabled=enabled)
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
