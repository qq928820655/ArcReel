"""视频汇总源：一个虚拟来源，成员是已配置供应商的模型，由 worker 认领时按容量展开。

见 ``docs/adr/0123-video-source-pool-as-virtual-provider.md``。三张表分工：

- ``video_source_pool``：汇总源本体（名称、启用）。本期只服务视频，不建 ``media_type`` 列。
- ``video_source_pool_member``：成员（指向已配置的视频模型），``position`` 即分配平局时的先后。
- ``provider_availability_state``：一个 ``(provider, model)`` 当天的可用态（额度/网络 streak、最近一次
  每日重置）。**按供应商模型而非按池**记录：额度与网络是供应商侧的事实，a-dola 用完了，任何引用它的
  池都该跳过它。这样任务终态只需知道实际执行的 provider/model，无需回查「这条任务来自哪个池」，
  热路径上没有额外的归属记账。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, utc_now


class VideoSourcePool(Base):
    """一个汇总源。对外以虚拟 provider id ``pool-<id>`` 出现。"""

    __tablename__ = "video_source_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 对外展示的统一模型名（候选下拉与默认模型页）；空则回退池名。池的虚拟 model_id 固定为 aggregate，
    # 展示层不该把池名重复成「池名 · 池名」，用这一列给用户一个可自定的模型名。
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 能力参考成员：池是虚拟 provider，能力解析无从收敛，取此成员的能力回填。空则取 position=0 的首个成员。
    capability_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capability_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 池自己的能力覆盖（稀疏）：参考成员只提供原值，用户可在其上整维覆盖，语义与自定义供应商模型的
    # capability_overrides 相同。键缺席即该维度跟随参考成员，出现即整维替换。
    capability_overrides: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class VideoSourcePoolMember(Base):
    """汇总源的一个成员：一个已配置供应商的视频模型，按 ``position`` 排序。"""

    __tablename__ = "video_source_pool_member"
    __table_args__ = (
        UniqueConstraint("pool_id", "position", name="uq_video_source_pool_member_position"),
        Index("idx_video_source_pool_member_pool", "pool_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("video_source_pool.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ProviderAvailabilityState(Base):
    """一个 ``(provider, media_type)`` 当天的可用态：连续额度不足、连续网络不可达、最近一次每日重置。

    与 ``source_priority_state`` 同语义：任一成功清零；任一 streak 达到各自阈值时该供应商当天不可用，
    北京零点重置。按供应商而非按池成员记录：额度是账户级事实，a-dola 用完了任何引用它的池都该跳过，
    任务终态也只需 provider_id、无需回查「来自哪个池」。
    """

    __tablename__ = "provider_availability_state"

    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    quota_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    network_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reset_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
