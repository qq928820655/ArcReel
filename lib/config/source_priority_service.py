"""多源优先级的服务层：把任务终态翻译成当前源的推进。

分层位置：本模块在 ``lib.config``（配置与解析）之上、``server`` 之下，由应用装配处把它作为
``outcome_listener`` 注入 ``GenerationWorker``——核心库不反向依赖它，worker 只认一个回调协议。

只在**已接入解析**的媒体类型上推进状态。图片与文本的多源配置可以保存、可以在界面里预览，但解析
尚未接进生成链路，此时推进它们的状态只会让界面显示的「当前源」与实际执行不一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.config.source_priority import (
    MediaType,
    SourcePriorityStore,
    read_project_config,
    state_scope,
)
from lib.generation.quota_exhaustion import classify_quota_exhaustion
from lib.project.project_manager import get_project_manager

logger = logging.getLogger(__name__)

#: 解析已接入多源优先级的媒体类型。图片与文本待后续接入，见模块 docstring。
WIRED_MEDIA_TYPES: frozenset[str] = frozenset({"video"})

#: 任务类型 → 媒体类型。视频两条路线同属 video lane；tts 归 audio（不参与）。
_TASK_MEDIA_TYPE: dict[str, MediaType] = {
    "video": "video",
    "reference_video": "video",
    "image": "image",
    "image_edit": "image",
    "text": "text",
}


@dataclass(frozen=True)
class TaskOutcome:
    """一次任务终态里与多源优先级相关的部分。"""

    media_type: MediaType
    project_name: str
    provider_id: str
    succeeded: bool
    quota_exhausted: bool


def task_outcome(
    task: dict[str, Any],
    *,
    succeeded: bool,
    exc: BaseException | None,
    provider_id: str,
) -> TaskOutcome | None:
    """从任务字典与结果拼出推进所需的输入；与多源无关的任务返回 ``None``。

    媒体类型优先看 ``media_type`` 列，缺失时按任务类型兜底（与 worker 的 lane 判定同口径）。
    """
    raw_media = task.get("media_type")
    media_type = _TASK_MEDIA_TYPE.get(raw_media) if isinstance(raw_media, str) else None
    if media_type is None:
        media_type = _TASK_MEDIA_TYPE.get(str(task.get("task_type") or ""))
    if media_type is None:
        return None
    project_name = task.get("project_name")
    if not isinstance(project_name, str) or not project_name:
        return None
    return TaskOutcome(
        media_type=media_type,
        project_name=project_name,
        provider_id=provider_id,
        succeeded=succeeded,
        quota_exhausted=(not succeeded) and classify_quota_exhaustion(exc),
    )


class SourcePriorityService:
    """任务终态 → 当前源推进。作为 ``GenerationWorker.outcome_listener`` 注入。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        task: dict[str, Any],
        *,
        succeeded: bool,
        exc: BaseException | None,
        provider_id: str,
    ) -> None:
        outcome = task_outcome(task, succeeded=succeeded, exc=exc, provider_id=provider_id)
        if outcome is None or outcome.media_type not in WIRED_MEDIA_TYPES:
            return
        if not outcome.succeeded and not outcome.quota_exhausted:
            # 非额度类失败不推进；成功仍要清零 streak，故只有「失败且非额度」提前返回。
            return
        try:
            await self._apply(outcome)
        except Exception:
            logger.warning(
                "多源优先级状态推进失败 project=%s media=%s",
                outcome.project_name,
                outcome.media_type,
                exc_info=True,
            )

    async def _apply(self, outcome: TaskOutcome) -> None:
        project = await _load_project(outcome.project_name)
        project_config = read_project_config(project, outcome.media_type)
        scope, project_name = state_scope(outcome.project_name)

        async with self._session_factory() as session:
            store = SourcePriorityStore(session)
            if project_config.is_active:
                config = project_config
            else:
                config = (await store.read_global_configs())[outcome.media_type]
                scope, project_name = state_scope(None)
            if not config.is_active:
                return
            await store.apply_outcome(
                scope=scope,
                project_name=project_name,
                media_type=outcome.media_type,
                entry_count=len(config.entries),
                quota_exhausted=outcome.quota_exhausted,
                succeeded=outcome.succeeded,
            )
            await session.commit()


async def _load_project(project_name: str) -> dict | None:
    """读项目快照；读不了按没有项目配置处理（回退全局）。"""
    import asyncio

    def _read() -> dict | None:
        try:
            return get_project_manager().load_project(project_name)
        except (OSError, ValueError):
            return None

    return await asyncio.to_thread(_read)
