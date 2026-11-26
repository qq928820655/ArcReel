"""多源优先级 API：三类媒体各自的有序 ``(provider, model)`` 列表、开关、当前生效行与手动切换。

配置落两处：全局在 ``system_setting``（键 ``source_priority_<media>``），项目在 ``project.json`` 的
``source_priority`` 字段。运行态（当前第几行）单独存表，这里只读它与手动切行。

路径分两组：

- ``/system/source-priority``：全局配置与全局运行态。
- ``/projects/{name}/source-priority``：项目配置与项目运行态。

项目没有自己的配置时回落全局；两层都未开启时生成链路走既有四级骨架，与本功能关闭完全等价。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lib.config.source_priority import (
    SOURCE_PRIORITY_MEDIA_TYPES,
    MediaType,
    SourcePriorityConfig,
    SourcePriorityEntry,
    SourcePriorityStore,
    clamp_active_index,
    read_project_config,
    state_scope,
    write_project_config,
)
from lib.db import get_async_session
from lib.db.repositories.task_repo import TaskRepository
from lib.infra.async_thread import run_sync_transaction
from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import get_project_manager

router = APIRouter(tags=["多源优先级"])


class SourcePriorityEntryPayload(BaseModel):
    provider_id: str
    model_id: str


class SourcePriorityChannelPayload(BaseModel):
    """一条通道的配置：开关 + 有序列表。"""

    enabled: bool = False
    entries: list[SourcePriorityEntryPayload] = Field(default_factory=list)


class SourcePriorityChannelView(BaseModel):
    """一条通道的读视图：配置 + 当前生效行（夹取到合法范围后）+ 该层是否生效。"""

    enabled: bool
    entries: list[SourcePriorityEntryPayload]
    active_index: int
    #: 当前是否由本层配置驱动生成（开关打开且有至少一行）。
    active: bool
    #: 这份数据来自哪一层（``project`` / ``global``）。项目层未配置该通道而回落全局时标 ``global``，
    #: 前端据此判断「点行启用」是否要先落本项目配置，避免直接打 select 端点拿到 409。
    source: str


class SourcePriorityResponse(BaseModel):
    """三类通道的配置与运行态；``scope`` 标明这份数据来自哪一层。"""

    scope: str
    video: SourcePriorityChannelView
    image: SourcePriorityChannelView
    text: SourcePriorityChannelView


def _channel_view(config: SourcePriorityConfig, active_index: int, *, source: str) -> SourcePriorityChannelView:
    count = len(config.entries)
    index = clamp_active_index(active_index, count) if count else 0
    return SourcePriorityChannelView(
        enabled=config.enabled,
        entries=[SourcePriorityEntryPayload(**entry.to_payload()) for entry in config.entries],
        active_index=index,
        active=config.is_active,
        source=source,
    )


def _parse_channel(payload: SourcePriorityChannelPayload | None) -> SourcePriorityConfig | None:
    """把请求体转成配置；``None`` 表示这次不更新该通道。

    列表里出现结构不合法的行时直接 422，不做静默丢弃：用户提交的是刚编辑过的列表，
    悄悄少一行比报错更难发现。
    """
    if payload is None:
        return None
    entries: list[SourcePriorityEntry] = []
    for raw in payload.entries:
        entry = SourcePriorityEntry.from_payload(raw.model_dump())
        if entry is None:
            raise HTTPException(status_code=422, detail="多源优先级的每一行都需要非空的 provider_id 与 model_id")
        entries.append(entry)
    return SourcePriorityConfig(enabled=payload.enabled, entries=tuple(entries))


# ---------------------------------------------------------------------------
# 全局
# ---------------------------------------------------------------------------


@router.get("/system/source-priority", response_model=SourcePriorityResponse)
async def get_global_source_priority(
    session: AsyncSession = Depends(get_async_session),
) -> SourcePriorityResponse:
    store = SourcePriorityStore(session)
    configs = await store.read_global_configs()
    scope, scope_project = state_scope(None)
    views: dict[str, SourcePriorityChannelView] = {}
    for media in SOURCE_PRIORITY_MEDIA_TYPES:
        state = await store.read_state(scope=scope, project_name=scope_project, media_type=media)
        views[media] = _channel_view(configs[media], state.active_index, source="global")
    return SourcePriorityResponse(scope="global", **views)


class SourcePriorityPatchRequest(BaseModel):
    """全局配置补丁：只更新请求里出现的通道，未出现的保持原样。"""

    video: SourcePriorityChannelPayload | None = None
    image: SourcePriorityChannelPayload | None = None
    text: SourcePriorityChannelPayload | None = None


@router.patch("/system/source-priority", response_model=SourcePriorityResponse)
async def patch_global_source_priority(
    req: SourcePriorityPatchRequest,
    session: AsyncSession = Depends(get_async_session),
) -> SourcePriorityResponse:
    store = SourcePriorityStore(session)
    for media in SOURCE_PRIORITY_MEDIA_TYPES:
        config = _parse_channel(getattr(req, media))
        if config is not None:
            await store.write_global_config(media, config)
    await session.commit()
    return await get_global_source_priority(session)


class SourcePrioritySelectRequest(BaseModel):
    media_type: MediaType
    active_index: int = Field(ge=0)


@router.post("/system/source-priority/select", response_model=SourcePriorityResponse)
async def select_global_source_priority(
    req: SourcePrioritySelectRequest,
    session: AsyncSession = Depends(get_async_session),
) -> SourcePriorityResponse:
    """手动把当前源切到列表里的某一行；不清配置，只改运行态下标。"""
    store = SourcePriorityStore(session)
    config = (await store.read_global_configs())[req.media_type]
    if req.active_index >= len(config.entries):
        raise HTTPException(status_code=422, detail="所选行超出该通道的列表长度")
    scope, scope_project = state_scope(None)
    await store.set_active_index(
        scope=scope, project_name=scope_project, media_type=req.media_type, active_index=req.active_index
    )
    # 手动切源同样要让排队中的任务跟随：不清掉旧 provider 记录，它们还会按旧源排队。
    await TaskRepository(session).clear_queued_provider_records(media_type=req.media_type)
    await session.commit()
    return await get_global_source_priority(session)


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------


async def _load_project_or_404(name: str) -> dict:
    def _read() -> dict:
        return get_project_manager().load_project(name)

    try:
        return await asyncio.to_thread(_read)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"项目不存在：{name}") from exc


@router.get("/projects/{name}/source-priority", response_model=SourcePriorityResponse)
async def get_project_source_priority(
    name: str,
    session: AsyncSession = Depends(get_async_session),
) -> SourcePriorityResponse:
    """项目层配置；某通道项目层未配置时，视图回落展示全局那份，``scope`` 逐通道标注来源。

    运行态始终按项目作用域读：项目层未配置而回落到全局时，生成用的是全局那份配置，
    但它的运行态记在全局作用域下——故回落的通道直接读全局运行态，避免展示与执行不一致。
    """
    project = await _load_project_or_404(name)
    store = SourcePriorityStore(session)
    global_configs = await store.read_global_configs()
    project_scope, project_scope_name = state_scope(name)
    global_scope, global_scope_name = state_scope(None)

    views: dict[str, SourcePriorityChannelView] = {}
    scopes: set[str] = set()
    for media in SOURCE_PRIORITY_MEDIA_TYPES:
        project_config = read_project_config(project, media)
        if project_config.is_active:
            state = await store.read_state(scope=project_scope, project_name=project_scope_name, media_type=media)
            views[media] = _channel_view(project_config, state.active_index, source="project")
            scopes.add("project")
        else:
            state = await store.read_state(scope=global_scope, project_name=global_scope_name, media_type=media)
            views[media] = _channel_view(global_configs[media], state.active_index, source="global")
            scopes.add("global")
    scope_label = "project" if scopes == {"project"} else ("global" if scopes == {"global"} else "mixed")
    return SourcePriorityResponse(scope=scope_label, **views)


@router.patch("/projects/{name}/source-priority", response_model=SourcePriorityResponse)
async def patch_project_source_priority(
    name: str,
    req: SourcePriorityPatchRequest,
    session: AsyncSession = Depends(get_async_session),
) -> SourcePriorityResponse:
    """写项目层配置。关闭且清空某通道时移除该字段，让该通道回落全局。"""
    parsed: dict[MediaType, SourcePriorityConfig | None] = {
        media: _parse_channel(getattr(req, media)) for media in SOURCE_PRIORITY_MEDIA_TYPES
    }
    if all(config is None for config in parsed.values()):
        return await get_project_source_priority(name, session)

    def _mutate(project: dict) -> None:
        for media, config in parsed.items():
            if config is not None:
                write_project_config(project, media, config)

    def _write() -> None:
        # 标记放在线程函数体内：contextvar 不会跨 asyncio.to_thread 自动传播。
        # 写 project.json 属于 Web 端创作内容变更，带上来源后变更提示才能按 WebUI 广播。
        with project_change_source("webui"):
            get_project_manager().update_project(name, _mutate)

    await run_sync_transaction(_write)
    return await get_project_source_priority(name, session)


@router.post("/projects/{name}/source-priority/select", response_model=SourcePriorityResponse)
async def select_project_source_priority(
    name: str,
    req: SourcePrioritySelectRequest,
    session: AsyncSession = Depends(get_async_session),
) -> SourcePriorityResponse:
    """手动切项目层的当前行。项目层未配置该通道时拒绝：此时该通道由全局驱动，切项目运行态没有效果。"""
    project = await _load_project_or_404(name)
    config = read_project_config(project, req.media_type)
    if not config.is_active:
        raise HTTPException(status_code=409, detail="该通道在项目层未配置，当前由全局配置驱动，请在全局层切换")
    if req.active_index >= len(config.entries):
        raise HTTPException(status_code=422, detail="所选行超出该通道的列表长度")
    store = SourcePriorityStore(session)
    scope, scope_project = state_scope(name)
    await store.set_active_index(
        scope=scope, project_name=scope_project, media_type=req.media_type, active_index=req.active_index
    )
    # 只清本项目的排队任务：项目层切源不影响其它项目。
    await TaskRepository(session).clear_queued_provider_records(media_type=req.media_type, project_name=name)
    await session.commit()
    return await get_project_source_priority(name, session)
