"""多源优先级服务：任务终态 → 当前源推进。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.config.source_priority import (
    SourcePriorityConfig,
    SourcePriorityEntry,
    SourcePriorityStore,
    write_project_config,
)
from lib.config.source_priority_service import SourcePriorityService, task_outcome
from lib.project.project_manager import get_project_manager


def _http_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/submit")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _task(*, media_type: str = "video", project_name: str = "demo", task_type: str = "video") -> dict:
    return {"task_id": "t1", "media_type": media_type, "task_type": task_type, "project_name": project_name}


# ---------------------------------------------------------------------------
# task_outcome
# ---------------------------------------------------------------------------


def test_task_outcome_maps_video_lane():
    outcome = task_outcome(_task(), succeeded=True, exc=None, provider_id="ark")
    assert outcome is not None
    assert outcome.media_type == "video"
    assert outcome.provider_id == "ark"
    assert outcome.succeeded is True
    assert outcome.quota_exhausted is False


def test_task_outcome_ignores_audio():
    outcome = task_outcome(_task(media_type="audio", task_type="tts"), succeeded=False, exc=None, provider_id="x")
    assert outcome is None


def test_task_outcome_requires_project():
    outcome = task_outcome(_task(project_name=""), succeeded=True, exc=None, provider_id="ark")
    assert outcome is None


def test_task_outcome_marks_quota_exhaustion_only_on_failure():
    exc = _http_error(402)
    failed = task_outcome(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert failed is not None
    assert failed.quota_exhausted is True
    # 成功时即便带着异常也不计额度
    succeeded = task_outcome(_task(), succeeded=True, exc=exc, provider_id="ark")
    assert succeeded is not None
    assert succeeded.quota_exhausted is False


def test_task_outcome_other_failure_is_not_quota():
    outcome = task_outcome(_task(), succeeded=False, exc=_http_error(400, "bad prompt"), provider_id="ark")
    assert outcome is not None
    assert outcome.quota_exhausted is False


def test_task_outcome_falls_back_to_task_type_for_media():
    task = {"task_id": "t", "task_type": "reference_video", "project_name": "demo"}
    outcome = task_outcome(task, succeeded=True, exc=None, provider_id="ark")
    assert outcome is not None
    assert outcome.media_type == "video"


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_priority():
    """一个开着视频多源的项目，三行 A/b、A/a、C/c（与需求例子一致）。"""
    pm = get_project_manager()
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", "narration")
    project = pm.load_project("demo")
    write_project_config(
        project,
        "video",
        SourcePriorityConfig(
            enabled=True,
            entries=(
                SourcePriorityEntry("ark", "b"),
                SourcePriorityEntry("ark", "a"),
                SourcePriorityEntry("vidu", "c"),
            ),
        ),
    )
    pm.save_project("demo", project)
    return pm


async def _service(db_session: AsyncSession) -> SourcePriorityService:
    factory = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    return SourcePriorityService(factory)


async def _state(db_session: AsyncSession) -> tuple[int, int]:
    store = SourcePriorityStore(db_session)
    row = await store.read_state(scope="project", project_name="demo", media_type="video")
    return row.active_index, row.quota_streak


async def test_two_quota_failures_switch_to_next_row(db_session: AsyncSession, project_with_priority):
    service = await _service(db_session)
    exc = _http_error(402)
    await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert await _state(db_session) == (0, 1)
    await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert await _state(db_session) == (1, 0)


async def test_success_clears_streak(db_session: AsyncSession, project_with_priority):
    service = await _service(db_session)
    await service(_task(), succeeded=False, exc=_http_error(402), provider_id="ark")
    assert await _state(db_session) == (0, 1)
    await service(_task(), succeeded=True, exc=None, provider_id="ark")
    assert await _state(db_session) == (0, 0)


async def test_last_row_quota_exhaustion_stops(db_session: AsyncSession, project_with_priority):
    service = await _service(db_session)
    exc = _http_error(402)
    # 连推到末行
    for _ in range(2):
        await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    for _ in range(2):
        await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert await _state(db_session) == (2, 0)
    # 末行再用完：停在末行
    await service(_task(), succeeded=False, exc=exc, provider_id="vidu")
    await service(_task(), succeeded=False, exc=exc, provider_id="vidu")
    assert await _state(db_session) == (2, 2)


async def test_non_quota_failure_does_not_move(db_session: AsyncSession, project_with_priority):
    service = await _service(db_session)
    await service(_task(), succeeded=False, exc=_http_error(400, "bad prompt"), provider_id="ark")
    assert await _state(db_session) == (0, 0)


async def test_inactive_config_is_ignored(db_session: AsyncSession):
    pm = get_project_manager()
    pm.create_project("plain")
    pm.create_project_metadata("plain", "Plain", "Anime", "narration")
    service = await _service(db_session)
    await service(
        {"task_id": "t", "media_type": "video", "task_type": "video", "project_name": "plain"},
        succeeded=False,
        exc=_http_error(402),
        provider_id="ark",
    )
    store = SourcePriorityStore(db_session)
    row = await store.read_state(scope="project", project_name="plain", media_type="video")
    assert row.active_index == 0
    assert row.quota_streak == 0


async def test_global_config_used_when_project_has_none(db_session: AsyncSession):
    """项目没配多源时回落到全局配置，状态记在全局作用域。"""
    pm = get_project_manager()
    pm.create_project("gproj")
    pm.create_project_metadata("gproj", "G", "Anime", "narration")

    store = SourcePriorityStore(db_session)
    await store.write_global_config(
        "video",
        SourcePriorityConfig(
            enabled=True,
            entries=(SourcePriorityEntry("ark", "b"), SourcePriorityEntry("vidu", "c")),
        ),
    )
    await db_session.commit()

    service = await _service(db_session)
    exc = _http_error(402)
    await service(
        {"task_id": "t", "media_type": "video", "task_type": "video", "project_name": "gproj"},
        succeeded=False,
        exc=exc,
        provider_id="ark",
    )
    await service(
        {"task_id": "t", "media_type": "video", "task_type": "video", "project_name": "gproj"},
        succeeded=False,
        exc=exc,
        provider_id="ark",
    )
    row = await SourcePriorityStore(db_session).read_state(scope="global", project_name="", media_type="video")
    assert row.active_index == 1


async def test_image_is_not_advanced_yet(db_session: AsyncSession):
    """图片配置可保存，但解析未接入，故不推进——否则界面显示与实际执行会不一致。"""
    pm = get_project_manager()
    pm.create_project("iproject")
    pm.create_project_metadata("iproject", "I", "Anime", "narration")
    project = pm.load_project("iproject")
    write_project_config(
        project,
        "image",
        SourcePriorityConfig(
            enabled=True, entries=(SourcePriorityEntry("ark", "img"), SourcePriorityEntry("grok", "g"))
        ),
    )
    pm.save_project("iproject", project)

    service = await _service(db_session)
    exc = _http_error(402)
    for _ in range(2):
        await service(
            {"task_id": "t", "media_type": "image", "task_type": "image", "project_name": "iproject"},
            succeeded=False,
            exc=exc,
            provider_id="ark",
        )
    row = await SourcePriorityStore(db_session).read_state(scope="project", project_name="iproject", media_type="image")
    assert row.active_index == 0


async def test_listener_never_raises_on_internal_failure(db_session: AsyncSession, monkeypatch):
    """服务层自身出错不能冒泡——任务结果已落定，观察者失败只记日志。"""
    service = await _service(db_session)

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_apply", _boom)
    await service(_task(), succeeded=True, exc=None, provider_id="ark")
