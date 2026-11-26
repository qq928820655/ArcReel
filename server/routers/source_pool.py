"""视频汇总源 API：CRUD 汇总源与其成员。

汇总源对外以虚拟 provider ``pool-<id>`` 出现，可像普通供应商一样被默认视频模型、i2v/r2v 桶与多源优先级
列表引用；具体成员由 worker 认领任务时按容量分配（见 ADR 0123）。

成员只允许指向已配置的视频模型：写入时逐个校验「供应商存在、模型存在、启用、媒体类型为 video」，避免
存进一个永远不会被分配的空引用。写入后 reload worker 的容量与池表，让分配立即看到新配置。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lib.catalog import query as catalog
from lib.config.registry import PROVIDER_REGISTRY
from lib.config.source_pool import make_pool_provider_id
from lib.custom_provider import is_custom_provider, parse_provider_id
from lib.db import get_async_session
from lib.db.base import dt_to_iso
from lib.db.models.source_pool import VideoSourcePool
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.db.repositories.source_pool_repo import SourcePoolRepository
from lib.infra.api_errors import BadRequestError, NotFoundError

router = APIRouter(tags=["视频汇总源"])


class PoolMemberPayload(BaseModel):
    provider_id: str
    model_id: str


class PoolPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # 统一模型名：候选下拉与默认模型页展示用；空则回退池名。虚拟 model_id 固定 aggregate。
    model_name: str | None = Field(default=None, max_length=128)
    # 能力参考成员：空则取 position=0 的首个成员。
    capability_provider_id: str | None = None
    capability_model_id: str | None = None
    enabled: bool = True
    members: list[PoolMemberPayload] = Field(default_factory=list)


class PoolMemberView(BaseModel):
    position: int
    provider_id: str
    model_id: str


class PoolView(BaseModel):
    id: int
    provider_id: str
    name: str
    model_name: str | None
    capability_provider_id: str | None
    capability_model_id: str | None
    enabled: bool
    members: list[PoolMemberView]


async def _validate_member(session: AsyncSession, provider_id: str, model_id: str) -> None:
    """成员必须指向已配置、启用、媒体类型为 video 的模型；否则 400。"""
    if not provider_id or not model_id:
        raise BadRequestError("pool_member_invalid")
    if is_custom_provider(provider_id):
        try:
            db_pid = parse_provider_id(provider_id)
        except ValueError as exc:
            raise BadRequestError("pool_member_invalid") from exc
        model = await CustomProviderRepository(session).get_model_by_ids(db_pid, model_id)
        if model is None or not model.is_enabled:
            raise BadRequestError("pool_member_not_found", provider=provider_id, model=model_id)
        # 自定义模型的媒体类型由端点定义决定，目录查询会返回其 media_type。
        info = catalog.get_model(provider_id, model_id)
        if info is not None and info.media_type != "video":
            raise BadRequestError("pool_member_not_video", provider=provider_id, model=model_id)
        return
    if provider_id not in PROVIDER_REGISTRY:
        raise BadRequestError("pool_member_unknown_provider", provider=provider_id)
    info = catalog.get_model(provider_id, model_id)
    if info is None:
        raise BadRequestError("pool_member_not_found", provider=provider_id, model=model_id)
    if info.media_type != "video":
        raise BadRequestError("pool_member_not_video", provider=provider_id, model=model_id)


async def _view(
    repo: SourcePoolRepository,
    pool: VideoSourcePool,
) -> PoolView:
    members = await repo.list_members(pool.id)
    return PoolView(
        id=pool.id,
        provider_id=make_pool_provider_id(pool.id),
        name=pool.name,
        model_name=pool.model_name,
        capability_provider_id=pool.capability_provider_id,
        capability_model_id=pool.capability_model_id,
        enabled=pool.enabled,
        members=[PoolMemberView(position=m.position, provider_id=m.provider_id, model_id=m.model_id) for m in members],
    )


async def _validate_capability_member(session: AsyncSession, body: PoolPayload) -> None:
    """能力参考成员若显式给出，须是已配置的视频模型（与成员同一校验），否则 400。"""
    if body.capability_provider_id is None and body.capability_model_id is None:
        return
    if not body.capability_provider_id or not body.capability_model_id:
        raise BadRequestError("pool_capability_member_invalid")
    await _validate_member(session, body.capability_provider_id, body.capability_model_id)


@router.get("/system/source-pools")
async def list_source_pools(session: AsyncSession = Depends(get_async_session)) -> list[PoolView]:
    repo = SourcePoolRepository(session)
    pools = await repo.list_pools()
    return [await _view(repo, p) for p in pools]


@router.post("/system/source-pools")
async def create_source_pool(body: PoolPayload, session: AsyncSession = Depends(get_async_session)) -> PoolView:
    for member in body.members:
        await _validate_member(session, member.provider_id, member.model_id)
    await _validate_capability_member(session, body)
    repo = SourcePoolRepository(session)
    pool = await repo.create_pool(
        name=body.name,
        model_name=body.model_name,
        capability_provider_id=body.capability_provider_id,
        capability_model_id=body.capability_model_id,
        enabled=body.enabled,
    )
    await repo.replace_members(pool.id, [(m.provider_id, m.model_id) for m in body.members])
    await session.commit()
    return await _view(repo, pool)


@router.put("/system/source-pools/{pool_id}")
async def update_source_pool(
    pool_id: int, body: PoolPayload, session: AsyncSession = Depends(get_async_session)
) -> PoolView:
    for member in body.members:
        await _validate_member(session, member.provider_id, member.model_id)
    await _validate_capability_member(session, body)
    repo = SourcePoolRepository(session)
    pool = await repo.get_pool(pool_id)
    if pool is None:
        raise NotFoundError("source_pool_not_found", pool_id=pool_id)
    pool.name = body.name
    pool.model_name = body.model_name
    pool.capability_provider_id = body.capability_provider_id
    pool.capability_model_id = body.capability_model_id
    pool.enabled = body.enabled
    await repo.replace_members(pool_id, [(m.provider_id, m.model_id) for m in body.members])
    await session.commit()
    return await _view(repo, pool)


@router.delete("/system/source-pools/{pool_id}")
async def delete_source_pool(pool_id: int, session: AsyncSession = Depends(get_async_session)) -> dict[str, bool]:
    repo = SourcePoolRepository(session)
    pool = await repo.get_pool(pool_id)
    if pool is None:
        raise NotFoundError("source_pool_not_found", pool_id=pool_id)
    await repo.delete_pool(pool_id)
    await session.commit()
    return {"deleted": True}


class PoolTaskView(BaseModel):
    """一条经汇总源分配的任务在监控页的投影：池 → 真实成员 → 调用聚合。"""

    task_id: str
    project_name: str
    task_type: str
    resource_id: str
    source_pool_id: int
    pool_name: str | None = None
    # 实际执行的供应商（池展开后的真实成员）与其模型。
    provider_id: str | None = None
    model_id: str | None = None
    status: str
    error_message: str | None = None
    queued_at: str
    started_at: str | None = None
    finished_at: str | None = None
    # 该任务在供应商侧的实际调用：次数、成功、失败、最近一次调用结束时刻。
    call_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    last_call_finished_at: str | None = None


class PoolTaskPage(BaseModel):
    items: list[PoolTaskView]
    total: int
    page: int
    page_size: int


@router.get("/system/source-pools/monitor", response_model=PoolTaskPage)
async def monitor_source_pool_tasks(
    session: AsyncSession = Depends(get_async_session),
    pool_id: int | None = Query(None, description="只看某个池；缺省看全部池"),
    status: str | None = Query(None, description="任务状态 (queued/running/succeeded/failed/cancelled)"),
    project_name: str | None = Query(None, description="项目名筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> PoolTaskPage:
    """汇总源监控：列出经池分配的任务，带池名、真实成员与调用情况。

    数据来自 ``tasks.source_pool_id``（池展开时补记的归属）与 ``api_calls``（按 ``task_id`` 聚合）。
    回答「哪个任务分到了哪个池、落到哪个真实源、调了几次、成没成、耗时多少」。
    """
    repo = SourcePoolRepository(session)
    result = await repo.list_pool_tasks(
        pool_id=pool_id, status=status, project_name=project_name, page=page, page_size=page_size
    )
    return PoolTaskPage(
        items=[_pool_task_view(item) for item in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


def _pool_task_view(item: dict) -> PoolTaskView:
    return PoolTaskView(
        task_id=item["task_id"],
        project_name=item["project_name"],
        task_type=item["task_type"],
        resource_id=item["resource_id"],
        source_pool_id=item["source_pool_id"],
        pool_name=item["pool_name"],
        provider_id=item["provider_id"],
        model_id=item["model_id"],
        status=item["status"],
        error_message=item["error_message"],
        queued_at=dt_to_iso(item["queued_at"]) or "",
        started_at=dt_to_iso(item["started_at"]),
        finished_at=dt_to_iso(item["finished_at"]),
        call_count=item["call_count"],
        success_count=item["success_count"],
        failed_count=item["failed_count"],
        last_call_finished_at=dt_to_iso(item["last_call_finished_at"]),
    )
