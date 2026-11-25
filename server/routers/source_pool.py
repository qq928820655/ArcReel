"""视频汇总源 API：CRUD 汇总源与其成员。

汇总源对外以虚拟 provider ``pool-<id>`` 出现，可像普通供应商一样被默认视频模型、i2v/r2v 桶与多源优先级
列表引用；具体成员由 worker 认领任务时按容量分配（见 ADR 0123）。

成员只允许指向已配置的视频模型：写入时逐个校验「供应商存在、模型存在、启用、媒体类型为 video」，避免
存进一个永远不会被分配的空引用。写入后 reload worker 的容量与池表，让分配立即看到新配置。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lib.catalog import query as catalog
from lib.config.registry import PROVIDER_REGISTRY
from lib.config.source_pool import make_pool_provider_id
from lib.custom_provider import is_custom_provider, parse_provider_id
from lib.db import get_async_session
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.db.repositories.source_pool_repo import SourcePoolRepository
from lib.infra.api_errors import BadRequestError, NotFoundError

router = APIRouter(tags=["视频汇总源"])


class PoolMemberPayload(BaseModel):
    provider_id: str
    model_id: str


class PoolPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
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


async def _view(repo: SourcePoolRepository, pool_id: int, name: str, enabled: bool) -> PoolView:
    members = await repo.list_members(pool_id)
    return PoolView(
        id=pool_id,
        provider_id=make_pool_provider_id(pool_id),
        name=name,
        enabled=enabled,
        members=[PoolMemberView(position=m.position, provider_id=m.provider_id, model_id=m.model_id) for m in members],
    )


@router.get("/system/source-pools")
async def list_source_pools(session: AsyncSession = Depends(get_async_session)) -> list[PoolView]:
    repo = SourcePoolRepository(session)
    pools = await repo.list_pools()
    return [await _view(repo, p.id, p.name, p.enabled) for p in pools]


@router.post("/system/source-pools")
async def create_source_pool(body: PoolPayload, session: AsyncSession = Depends(get_async_session)) -> PoolView:
    for member in body.members:
        await _validate_member(session, member.provider_id, member.model_id)
    repo = SourcePoolRepository(session)
    pool = await repo.create_pool(name=body.name, enabled=body.enabled)
    await repo.replace_members(pool.id, [(m.provider_id, m.model_id) for m in body.members])
    await session.commit()
    return await _view(repo, pool.id, pool.name, pool.enabled)


@router.put("/system/source-pools/{pool_id}")
async def update_source_pool(
    pool_id: int, body: PoolPayload, session: AsyncSession = Depends(get_async_session)
) -> PoolView:
    for member in body.members:
        await _validate_member(session, member.provider_id, member.model_id)
    repo = SourcePoolRepository(session)
    pool = await repo.get_pool(pool_id)
    if pool is None:
        raise NotFoundError("source_pool_not_found", pool_id=pool_id)
    pool.name = body.name
    pool.enabled = body.enabled
    await repo.replace_members(pool_id, [(m.provider_id, m.model_id) for m in body.members])
    await session.commit()
    return await _view(repo, pool.id, pool.name, pool.enabled)


@router.delete("/system/source-pools/{pool_id}")
async def delete_source_pool(pool_id: int, session: AsyncSession = Depends(get_async_session)) -> dict[str, bool]:
    repo = SourcePoolRepository(session)
    pool = await repo.get_pool(pool_id)
    if pool is None:
        raise NotFoundError("source_pool_not_found", pool_id=pool_id)
    await repo.delete_pool(pool_id)
    await session.commit()
    return {"deleted": True}
