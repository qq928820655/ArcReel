import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from lib.config.resolver import ConfigResolver
from lib.db.repositories.source_pool_repo import SourcePoolRepository


@pytest.mark.asyncio
async def test_pool_capabilities_from_reference_member(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        repo = SourcePoolRepository(s)
        pool = await repo.create_pool(name="p", model_name="my-pool")
        await repo.replace_members(pool.id, [("gemini-vertex", "veo-3.1-generate-001")])
        await s.commit()
        pid = pool.id

    resolver = ConfigResolver(factory)
    caps = await resolver.video_capabilities_for_model(f"pool-{pid}", "aggregate", None, generation_type="i2v")
    # 对外身份仍是池
    assert caps["provider_id"] == f"pool-{pid}"
    assert caps["model"] == "aggregate"
    # 能力来自参考成员
    assert caps["supported_durations"], caps
    assert caps["source"] == "registry"


@pytest.mark.asyncio
async def test_pool_capability_explicit_reference(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        repo = SourcePoolRepository(s)
        pool = await repo.create_pool(
            name="p", capability_provider_id="gemini-vertex", capability_model_id="veo-3.1-generate-001"
        )
        await repo.replace_members(pool.id, [("grok", "grok-imagine-video")])
        await s.commit()
        pid = pool.id

    resolver = ConfigResolver(factory)
    caps = await resolver.video_capabilities_for_model(f"pool-{pid}", "aggregate", None, generation_type="i2v")
    assert caps["provider_id"] == f"pool-{pid}"
    assert caps["supported_durations"]


@pytest.mark.asyncio
async def test_pool_bucket_gate_uses_reference_member(db_engine):
    """桶能力闸对池取参考成员的能力；报错以池身份给出，不暴露成员身份。"""
    from lib.config.resolver import VideoBucketCapabilityError

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        repo = SourcePoolRepository(s)
        pool = await repo.create_pool(name="p")
        await repo.replace_members(pool.id, [("gemini-vertex", "veo-3.1-generate-001")])
        await s.commit()
        pid = pool.id

    resolver = ConfigResolver(factory)
    async with resolver._open_session() as (session, _svc):
        # veo-3.1 支持 i2v（首帧），闸应通过
        from lib.config.resolver import ProviderModel

        await resolver._ensure_video_bucket_capability(session, ProviderModel(f"pool-{pid}", "aggregate"), "i2v")

        # 池没有成员时，闸以池身份报错
        await SourcePoolRepository(session).delete_pool(pid)
        await session.commit()
        with pytest.raises(VideoBucketCapabilityError) as exc:
            await resolver._ensure_video_bucket_capability(session, ProviderModel(f"pool-{pid}", "aggregate"), "i2v")
        assert exc.value.provider_id == f"pool-{pid}"


@pytest.mark.asyncio
async def test_pool_overrides_apply_on_top_of_reference(db_engine):
    """池自己的 capability_overrides 整维覆盖参考成员的能力；未覆盖维度沿用参考成员。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        repo = SourcePoolRepository(s)
        pool = await repo.create_pool(
            name="p",
            capability_overrides={"durations": [5, 10], "resolutions": ["720p"], "audio_track": "always_on"},
        )
        await repo.replace_members(pool.id, [("gemini-vertex", "veo-3.1-generate-001")])
        await s.commit()
        pid = pool.id

    resolver = ConfigResolver(factory)
    caps = await resolver.video_capabilities_for_model(f"pool-{pid}", "aggregate", None, generation_type="i2v")
    assert caps["provider_id"] == f"pool-{pid}"
    # 覆盖生效
    assert caps["supported_durations"] == [5, 10]
    assert caps["max_duration"] == 10
    assert caps["resolutions"] == ["720p"]
    assert caps["has_audio"] is True
    # 未覆盖维度沿用参考成员（veo 支持首帧）
    assert caps["first_frame"] is True


@pytest.mark.asyncio
async def test_pool_override_durations_narrow_via_constraints(db_engine):
    """池覆盖的时长联动约束经 video_duration_constraints 生效（请求事实据此收窄）。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        repo = SourcePoolRepository(s)
        pool = await repo.create_pool(
            name="p",
            capability_overrides={
                "durations": [4, 6, 8],
                "duration_constraints": {"by_resolution": {"1080p": [4, 6]}, "with_reference_images": []},
            },
        )
        await repo.replace_members(pool.id, [("gemini-vertex", "veo-3.1-generate-001")])
        await s.commit()
        pid = pool.id

    resolver = ConfigResolver(factory)
    constraints = await resolver.video_duration_constraints(f"pool-{pid}", "aggregate")
    assert constraints is not None
    assert constraints.by_resolution.get("1080p") == (4, 6)
