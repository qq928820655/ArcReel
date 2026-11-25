"""汇总源与供应商可用态的读写：成员排序、整表替换、可用态 upsert 与每日重置。"""

from __future__ import annotations

from datetime import date

import pytest

from lib.db.repositories.source_pool_repo import SourcePoolRepository


@pytest.mark.asyncio
class TestSourcePoolRepo:
    async def test_create_and_list_members_in_order(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(name="主池")
        await repo.replace_members(pool.id, [("custom-2", "seedance-2.0"), ("custom-5", "seedance-2.0")])
        await db_session.commit()

        members = await repo.list_members(pool.id)
        assert [(m.position, m.provider_id, m.model_id) for m in members] == [
            (0, "custom-2", "seedance-2.0"),
            (1, "custom-5", "seedance-2.0"),
        ]

    async def test_replace_members_overwrites_entirely(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(name="主池")
        await repo.replace_members(pool.id, [("custom-2", "m"), ("custom-5", "m")])
        await repo.replace_members(pool.id, [("custom-9", "m")])
        await db_session.commit()

        members = await repo.list_members(pool.id)
        assert [(m.position, m.provider_id) for m in members] == [(0, "custom-9")]

    async def test_list_all_members_across_pools(self, db_session):
        repo = SourcePoolRepository(db_session)
        p1 = await repo.create_pool(name="p1")
        p2 = await repo.create_pool(name="p2")
        await repo.replace_members(p1.id, [("a", "m")])
        await repo.replace_members(p2.id, [("b", "m")])
        await db_session.commit()

        rows = await repo.list_all_members()
        assert {(r.pool_id, r.provider_id) for r in rows} == {(p1.id, "a"), (p2.id, "b")}

    async def test_delete_pool_cascades_members(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(name="主池")
        await repo.replace_members(pool.id, [("a", "m")])
        await db_session.commit()

        await repo.delete_pool(pool.id)
        await db_session.commit()
        assert await repo.list_members(pool.id) == []


@pytest.mark.asyncio
class TestAvailabilityRepo:
    async def test_upsert_inserts_then_updates(self, db_session):
        repo = SourcePoolRepository(db_session)
        await repo.upsert_availability(
            provider_id="custom-2", media_type="video", quota_streak=1, network_streak=0, last_reset_on=None
        )
        await repo.upsert_availability(
            provider_id="custom-2", media_type="video", quota_streak=2, network_streak=1, last_reset_on=None
        )
        await db_session.commit()

        row = await repo.get_availability(provider_id="custom-2", media_type="video")
        assert row is not None
        assert (row.quota_streak, row.network_streak) == (2, 1)

    async def test_reset_clears_streaks_and_stamps_today(self, db_session):
        repo = SourcePoolRepository(db_session)
        await repo.upsert_availability(
            provider_id="custom-2", media_type="video", quota_streak=2, network_streak=1, last_reset_on=None
        )
        await db_session.commit()

        changed = await repo.reset_availability_before(media_type="video", today=date(2026, 1, 1))
        await db_session.commit()
        assert changed == 1
        row = await repo.get_availability(provider_id="custom-2", media_type="video")
        assert row is not None
        assert (row.quota_streak, row.network_streak) == (0, 0)
        assert row.last_reset_on == date(2026, 1, 1)

    async def test_reset_same_day_is_idempotent(self, db_session):
        repo = SourcePoolRepository(db_session)
        await repo.upsert_availability(
            provider_id="custom-2", media_type="video", quota_streak=0, network_streak=0, last_reset_on=date(2026, 1, 1)
        )
        await db_session.commit()

        changed = await repo.reset_availability_before(media_type="video", today=date(2026, 1, 1))
        assert changed == 0
