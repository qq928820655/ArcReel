"""多源优先级的配置与运行态：有序的 ``(provider, model)`` 列表、当前生效行、额度不足推进。

三层数据分开存放：

- **配置**（开关与有序列表）低频写、参与模型引用扫描：全局落 ``system_setting``（键 ``source_priority_<media>``，
  值为 JSON），项目落 ``project.json`` 的 ``source_priority`` 字段。
- **运行态**（当前第几行、连续几次额度不足、最近一次每日重置）高频易变：落 ``source_priority_state`` 表，
  不回写 ``project.json`` 那份热读文件。

解析顺序与既有四级骨架一致：项目层有配置且开启时用项目的列表，否则回落全局。多源关闭时整条链路不参与
解析，行为与没有这个功能完全相同。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.models.source_priority import GLOBAL_PROJECT_NAME, SCOPE_GLOBAL, SCOPE_PROJECT, SourcePriorityState

logger = logging.getLogger(__name__)

MediaType = Literal["video", "image", "text"]

#: 参与多源优先级的媒体类型，顺序即 UI 里的通道顺序。
SOURCE_PRIORITY_MEDIA_TYPES: tuple[MediaType, ...] = ("video", "image", "text")

#: 全局设置的键前缀：完整键是 ``source_priority_<media_type>``。
SETTING_KEY_PREFIX = "source_priority_"

#: 项目 ``project.json`` 里承载三类通道配置的字段名。
PROJECT_FIELD = "source_priority"

#: 连续几次额度不足才下移一行。1 会让一次偶发的限流误切源，2 是「确实用完了」的最小可信信号。
QUOTA_STREAK_THRESHOLD = 2

#: 每日重置的时区：北京时间零点恢复首行。
RESET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


def setting_key(media_type: MediaType) -> str:
    """全局设置里该媒体类型的多源配置键。"""
    return f"{SETTING_KEY_PREFIX}{media_type}"


@dataclass(frozen=True)
class SourcePriorityEntry:
    """列表中的一行：一对 ``(provider_id, model_id)``。"""

    provider_id: str
    model_id: str

    @property
    def pair_key(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    def to_payload(self) -> dict[str, str]:
        return {"provider_id": self.provider_id, "model_id": self.model_id}

    @classmethod
    def from_payload(cls, raw: object) -> SourcePriorityEntry | None:
        """从 ``{"provider_id": ..., "model_id": ...}`` 解析；结构不合法返回 ``None``。"""
        if not isinstance(raw, dict):
            return None
        provider_id = raw.get("provider_id")
        model_id = raw.get("model_id")
        if not isinstance(provider_id, str) or not isinstance(model_id, str):
            return None
        provider_id, model_id = provider_id.strip(), model_id.strip()
        if not provider_id or not model_id:
            return None
        return cls(provider_id=provider_id, model_id=model_id)


@dataclass(frozen=True)
class SourcePriorityConfig:
    """一条通道的配置：开关 + 有序列表。``entries`` 为空时整条配置视为未生效。"""

    enabled: bool = False
    entries: tuple[SourcePriorityEntry, ...] = ()

    @property
    def is_active(self) -> bool:
        """开关打开且有至少一行可用条目才算生效。"""
        return self.enabled and bool(self.entries)

    def to_payload(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "entries": [entry.to_payload() for entry in self.entries]}

    @classmethod
    def from_payload(cls, raw: object) -> SourcePriorityConfig:
        """容错解析：结构不对的部分按缺省处理，绝不抛错。

        读侧面对的是用户可手改的 JSON 与历史数据，任何一处畸形都不该让生成链路失败——畸形即视为
        未开启，回退到既有四级解析。
        """
        if not isinstance(raw, dict):
            return cls()
        enabled = raw.get("enabled") is True
        raw_entries = raw.get("entries")
        entries: list[SourcePriorityEntry] = []
        if isinstance(raw_entries, list):
            for item in raw_entries:
                entry = SourcePriorityEntry.from_payload(item)
                if entry is not None:
                    entries.append(entry)
        return cls(enabled=enabled, entries=tuple(entries))

    @classmethod
    def from_json(cls, raw: str) -> SourcePriorityConfig:
        """从设置表里的 JSON 字符串解析；空串或坏 JSON 视为未配置。"""
        text = (raw or "").strip()
        if not text:
            return cls()
        try:
            return cls.from_payload(json.loads(text))
        except (ValueError, TypeError):
            logger.warning("多源优先级配置不是合法 JSON，按未开启处理: %r", text[:200])
            return cls()


@dataclass(frozen=True)
class SourcePriorityStateRow:
    """一行运行态。``active_index`` 可能超出配置长度（列表被改短），消费方需自行夹取。"""

    active_index: int = 0
    quota_streak: int = 0
    last_reset_on: date | None = None


@dataclass(frozen=True)
class ResolvedSource:
    """解析结果：当前生效的条目 + 它所在的层与下标，供展示与状态推进复用。"""

    entry: SourcePriorityEntry
    scope: Literal["global", "project"]
    project_name: str
    active_index: int


def clamp_active_index(active_index: int, entry_count: int) -> int:
    """把运行态下标夹进 ``[0, entry_count)``：列表改短后旧的较大下标回落到末行。"""
    if entry_count <= 0:
        return 0
    if active_index < 0:
        return 0
    return min(active_index, entry_count - 1)


def beijing_today(now: datetime | None = None) -> date:
    """当前北京日期，用作每日重置的幂等标记。"""
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(RESET_TIMEZONE).date()


def next_beijing_midnight(now: datetime | None = None) -> datetime:
    """下一个北京零点的 UTC 时刻，供重置循环计算睡眠时长。"""
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(RESET_TIMEZONE)
    next_local = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=RESET_TIMEZONE)
    return next_local.astimezone(UTC)


def advance(
    *,
    entry_count: int,
    active_index: int,
    quota_streak: int,
    quota_exhausted: bool,
    succeeded: bool,
) -> tuple[int, int]:
    """按一次任务结果推进状态，返回新的 ``(active_index, quota_streak)``。

    规则（与需求逐条对应）：

    - 成功：清零 streak，不换行。
    - 额度不足：streak + 1；达到阈值时下移一行并清零，已到末行则停在末行、streak 保持。
    - 其他失败：不动。
    """
    index = clamp_active_index(active_index, entry_count)
    if succeeded:
        return index, 0
    if not quota_exhausted:
        return index, quota_streak
    streak = quota_streak + 1
    if streak < QUOTA_STREAK_THRESHOLD:
        return index, streak
    if index + 1 >= entry_count:
        # 末行也用完：不再切换，streak 保持以便观察；次日零点整体重置。
        return index, streak
    return index + 1, 0


class SourcePriorityStore:
    """配置与运行态的读写。配置走传入的 ``ConfigService`` / 项目快照，运行态走本类的 session。

    所有写操作都是「读-改-写」单事务，避免并发失败回调互相覆盖 streak。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------- 配置：全局 ----------------

    async def read_global_configs(self) -> dict[MediaType, SourcePriorityConfig]:
        """一次查询取回三类通道的全局配置；缺键视为未配置。"""
        from lib.config.repository import SystemSettingRepository

        raw_values = await SystemSettingRepository(self._session).get_prefixed(SETTING_KEY_PREFIX)
        return {
            media: SourcePriorityConfig.from_json(raw_values.get(setting_key(media), ""))
            for media in SOURCE_PRIORITY_MEDIA_TYPES
        }

    async def write_global_config(self, media_type: MediaType, config: SourcePriorityConfig) -> None:
        from lib.config.repository import SystemSettingRepository

        await SystemSettingRepository(self._session).upsert(setting_key(media_type), json.dumps(config.to_payload()))

    # ---------------- 运行态 ----------------

    async def read_state(self, *, scope: str, project_name: str, media_type: MediaType) -> SourcePriorityStateRow:
        stmt = select(SourcePriorityState).where(
            SourcePriorityState.scope == scope,
            SourcePriorityState.project_name == project_name,
            SourcePriorityState.media_type == media_type,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return SourcePriorityStateRow()
        return SourcePriorityStateRow(
            active_index=row.active_index,
            quota_streak=row.quota_streak,
            last_reset_on=row.last_reset_on,
        )

    async def _upsert_state(
        self,
        *,
        scope: str,
        project_name: str,
        media_type: MediaType,
        active_index: int,
        quota_streak: int,
        last_reset_on: date | None,
    ) -> None:
        stmt = select(SourcePriorityState).where(
            SourcePriorityState.scope == scope,
            SourcePriorityState.project_name == project_name,
            SourcePriorityState.media_type == media_type,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            self._session.add(
                SourcePriorityState(
                    scope=scope,
                    project_name=project_name,
                    media_type=media_type,
                    active_index=active_index,
                    quota_streak=quota_streak,
                    last_reset_on=last_reset_on,
                )
            )
        else:
            row.active_index = active_index
            row.quota_streak = quota_streak
            row.last_reset_on = last_reset_on
            row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def set_active_index(
        self, *, scope: str, project_name: str, media_type: MediaType, active_index: int
    ) -> None:
        """用户手动点选某一行：直接切过去并清零 streak。"""
        current = await self.read_state(scope=scope, project_name=project_name, media_type=media_type)
        await self._upsert_state(
            scope=scope,
            project_name=project_name,
            media_type=media_type,
            active_index=max(0, active_index),
            quota_streak=0,
            last_reset_on=current.last_reset_on,
        )

    async def apply_outcome(
        self,
        *,
        scope: str,
        project_name: str,
        media_type: MediaType,
        entry_count: int,
        quota_exhausted: bool,
        succeeded: bool,
    ) -> tuple[int, int]:
        """按一次任务结果推进并落库，返回新的 ``(active_index, quota_streak)``。"""
        current = await self.read_state(scope=scope, project_name=project_name, media_type=media_type)
        new_index, new_streak = advance(
            entry_count=entry_count,
            active_index=current.active_index,
            quota_streak=current.quota_streak,
            quota_exhausted=quota_exhausted,
            succeeded=succeeded,
        )
        if (new_index, new_streak) != (current.active_index, current.quota_streak):
            await self._upsert_state(
                scope=scope,
                project_name=project_name,
                media_type=media_type,
                active_index=new_index,
                quota_streak=new_streak,
                last_reset_on=current.last_reset_on,
            )
            if new_index != current.active_index:
                logger.info(
                    "多源优先级切换 scope=%s project=%s media=%s %d -> %d（额度不足）",
                    scope,
                    project_name,
                    media_type,
                    current.active_index,
                    new_index,
                )
        return new_index, new_streak

    async def reset_if_due(self, *, today: date) -> int:
        """把所有 ``last_reset_on`` 早于 *today* 的行重置回首行，返回受影响行数。

        幂等：同一天重复调用不会重复推进。``last_reset_on`` 为空的旧行同样重置。
        """
        stmt = select(SourcePriorityState)
        rows = list((await self._session.execute(stmt)).scalars())
        changed = 0
        for row in rows:
            if row.last_reset_on is not None and row.last_reset_on >= today:
                continue
            row.active_index = 0
            row.quota_streak = 0
            row.last_reset_on = today
            row.updated_at = datetime.now(UTC)
            changed += 1
        if changed:
            await self._session.flush()
            logger.info("多源优先级每日重置：%d 行回到首行（%s）", changed, today.isoformat())
        return changed


def state_scope(project_name: str | None) -> tuple[str, str]:
    """由项目名求运行态的作用域键：有项目用项目层，无项目用全局。"""
    if project_name:
        return SCOPE_PROJECT, project_name
    return SCOPE_GLOBAL, GLOBAL_PROJECT_NAME


def read_project_config(project: dict | None, media_type: MediaType) -> SourcePriorityConfig:
    """从 ``project.json`` 快照读某通道的配置；没有项目或字段畸形时返回未配置。"""
    if not project:
        return SourcePriorityConfig()
    raw = project.get(PROJECT_FIELD)
    if not isinstance(raw, dict):
        return SourcePriorityConfig()
    return SourcePriorityConfig.from_payload(raw.get(media_type))


def write_project_config(project: dict, media_type: MediaType, config: SourcePriorityConfig) -> None:
    """把某通道配置写进项目快照（就地修改，调用方负责落盘）。

    关闭且无条目时移除该通道，避免 ``project.json`` 里堆积无意义的空配置。
    """
    raw = project.get(PROJECT_FIELD)
    container: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    if config.enabled or config.entries:
        container[media_type] = config.to_payload()
    else:
        container.pop(media_type, None)
    if container:
        project[PROJECT_FIELD] = container
    else:
        project.pop(PROJECT_FIELD, None)


def pick_entry(config: SourcePriorityConfig, active_index: int) -> tuple[SourcePriorityEntry, int] | None:
    """按运行态下标取当前生效条目，返回 ``(entry, 夹取后的下标)``；配置未生效时返回 ``None``。"""
    if not config.is_active:
        return None
    index = clamp_active_index(active_index, len(config.entries))
    return config.entries[index], index
