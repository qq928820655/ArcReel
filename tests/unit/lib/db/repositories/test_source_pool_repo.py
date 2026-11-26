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

    async def test_capability_member_defaults_to_first_position(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(name="主池")
        await repo.replace_members(pool.id, [("custom-2", "seedance-2.0"), ("custom-5", "seedance-2.0")])
        await db_session.commit()

        assert await repo.capability_member(pool.id) == ("custom-2", "seedance-2.0")

    async def test_capability_member_prefers_explicit_reference(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(
            name="主池",
            capability_provider_id="custom-5",
            capability_model_id="seedance-2.0",
        )
        await repo.replace_members(pool.id, [("custom-2", "seedance-2.0")])
        await db_session.commit()

        assert await repo.capability_member(pool.id) == ("custom-5", "seedance-2.0")

    async def test_capability_member_none_when_pool_empty(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(name="空池")
        await db_session.commit()

        assert await repo.capability_member(pool.id) is None


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


@pytest.mark.asyncio
class TestPoolTaskMonitor:
    async def _seed_task(self, db_session, task_id, *, source_pool_id, provider_id, payload_model):
        from datetime import UTC, datetime

        from lib.db.models.task import Task

        now = datetime.now(UTC)
        db_session.add(
            Task(
                task_id=task_id,
                project_name="demo",
                task_type="video",
                media_type="video",
                resource_id="E1S01",
                status="succeeded",
                queued_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
                provider_id=provider_id,
                source_pool_id=source_pool_id,
                payload_json=f'{{"video_provider_i2v": "{provider_id}/{payload_model}"}}',
            )
        )

    async def _seed_call(self, db_session, *, task_id, status):
        from datetime import UTC, datetime

        from lib.db.models.api_call import ApiCall

        now = datetime.now(UTC)
        db_session.add(
            ApiCall(
                project_name="demo",
                call_type="video",
                model="m",
                status=status,
                provider="custom-2",
                task_id=task_id,
                started_at=now,
                finished_at=now,
            )
        )

    async def test_lists_pool_tasks_with_real_member_and_call_aggregates(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(name="主池")
        await self._seed_task(db_session, "t1", source_pool_id=pool.id, provider_id="custom-5", payload_model="seedance-2.0")
        await self._seed_task(db_session, "t2", source_pool_id=None, provider_id="custom-2", payload_model="x")
        await self._seed_call(db_session, task_id="t1", status="success")
        await self._seed_call(db_session, task_id="t1", status="failed")
        await db_session.commit()

        result = await repo.list_pool_tasks()

        # 只有带 source_pool_id 的任务入选
        assert result["total"] == 1
        item = result["items"][0]
        assert item["task_id"] == "t1"
        assert item["pool_name"] == "主池"
        assert item["provider_id"] == "custom-5"
        assert item["model_id"] == "seedance-2.0"
        assert (item["call_count"], item["success_count"], item["failed_count"]) == (2, 1, 1)

    async def test_filters_by_pool(self, db_session):
        repo = SourcePoolRepository(db_session)
        p1 = await repo.create_pool(name="p1")
        p2 = await repo.create_pool(name="p2")
        await self._seed_task(db_session, "t1", source_pool_id=p1.id, provider_id="custom-2", payload_model="m")
        await self._seed_task(db_session, "t2", source_pool_id=p2.id, provider_id="custom-5", payload_model="m")
        await db_session.commit()

        result = await repo.list_pool_tasks(pool_id=p2.id)
        assert result["total"] == 1
        assert result["items"][0]["task_id"] == "t2"

    async def test_queued_task_without_calls_has_zero_counts(self, db_session):
        repo = SourcePoolRepository(db_session)
        pool = await repo.create_pool(name="主池")
        await self._seed_task(db_session, "t1", source_pool_id=pool.id, provider_id="custom-2", payload_model="m")
        await db_session.commit()

        result = await repo.list_pool_tasks()
        item = result["items"][0]
        assert (item["call_count"], item["success_count"], item["failed_count"]) == (0, 0, 0)
