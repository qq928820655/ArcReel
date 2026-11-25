"""多源优先级服务：任务终态 → 当前源推进。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.config.source_priority import (
    NETWORK_STREAK_THRESHOLD,
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


async def _state3(db_session: AsyncSession) -> tuple[int, int, int]:
    store = SourcePriorityStore(db_session)
    row = await store.read_state(scope="project", project_name="demo", media_type="video")
    return row.active_index, row.quota_streak, row.network_streak


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


# ---------------------------------------------------------------------------
# 网络不可达
# ---------------------------------------------------------------------------


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused", request=httpx.Request("POST", "https://api.example.com"))


async def test_five_network_failures_switch_to_next_row(db_session: AsyncSession, project_with_priority):
    """网络不可达阈值是 5 次（比额度的 2 次宽，避免一次抖动就换源）。"""
    service = await _service(db_session)
    exc = _connect_error()
    for expected in range(1, NETWORK_STREAK_THRESHOLD):
        await service(_task(), succeeded=False, exc=exc, provider_id="ark")
        assert await _state3(db_session) == (0, 0, expected)
    # 第 5 次达到阈值：下移一行并清零
    await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert await _state3(db_session) == (1, 0, 0)


async def test_quota_and_network_failures_count_separately(db_session: AsyncSession, project_with_priority):
    """一次额度 + 一次断网不会凑够阈值换行；两者各自保留在各自计数里。"""
    service = await _service(db_session)
    await service(_task(), succeeded=False, exc=_http_error(402), provider_id="ark")
    await service(_task(), succeeded=False, exc=_connect_error(), provider_id="ark")
    assert await _state3(db_session) == (0, 1, 1)


async def test_network_failure_on_last_row_stops(db_session: AsyncSession, project_with_priority):
    service = await _service(db_session)
    exc = _connect_error()
    # 三行列表：推两次换行到末行（每次需要 NETWORK_STREAK_THRESHOLD 次连续失败）
    for _ in range(NETWORK_STREAK_THRESHOLD * 2):
        await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert (await _state3(db_session))[0] == 2
    # 末行继续失败：停在末行，计数保持
    for _ in range(NETWORK_STREAK_THRESHOLD):
        await service(_task(), succeeded=False, exc=exc, provider_id="vidu")
    index, _quota, network = await _state3(db_session)
    assert index == 2
    assert network == NETWORK_STREAK_THRESHOLD


async def test_network_failure_task_outcome_flags(db_session: AsyncSession, project_with_priority):
    from lib.config.source_priority_service import task_outcome

    outcome = task_outcome(_task(), succeeded=False, exc=_connect_error(), provider_id="ark")
    assert outcome is not None
    assert outcome.network_unreachable is True
    assert outcome.quota_exhausted is False


async def test_non_network_non_quota_failure_ignored(db_session: AsyncSession, project_with_priority):
    service = await _service(db_session)
    await service(_task(), succeeded=False, exc=_http_error(400, "bad prompt"), provider_id="ark")
    assert await _state3(db_session) == (0, 0, 0)


async def test_switching_source_invokes_switch_callback(db_session: AsyncSession, project_with_priority):
    """切源时调用注入的切源回调，交给装配层清空排队任务的 provider 记录。"""
    calls: list[tuple[str, str | None]] = []

    async def _on_switch(media_type: str, project_name: str | None) -> None:
        calls.append((media_type, project_name))

    factory = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    service = SourcePriorityService(factory, on_source_switched=_on_switch)
    exc = _http_error(402)
    await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert calls == []  # 还没到阈值，不切
    await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert await _state(db_session) == (1, 0)
    assert calls == [("video", "demo")]


async def test_no_switch_does_not_invoke_switch_callback(db_session: AsyncSession, project_with_priority):
    """没发生切源时不动回调：单次失败不换源，排队任务保持原样。"""
    calls: list[tuple[str, str | None]] = []

    async def _on_switch(media_type: str, project_name: str | None) -> None:
        calls.append((media_type, project_name))

    factory = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    service = SourcePriorityService(factory, on_source_switched=_on_switch)
    await service(_task(), succeeded=False, exc=_http_error(402), provider_id="ark")
    assert await _state(db_session) == (0, 1)
    assert calls == []


async def test_switch_callback_failure_is_swallowed(db_session: AsyncSession, project_with_priority):
    """切源回调抛错不影响推进本身：状态已落库，回调只记日志。"""

    async def _boom(media_type: str, project_name: str | None) -> None:
        raise RuntimeError("boom")

    factory = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    service = SourcePriorityService(factory, on_source_switched=_boom)
    exc = _http_error(402)
    await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    await service(_task(), succeeded=False, exc=exc, provider_id="ark")
    assert await _state(db_session) == (1, 0)
