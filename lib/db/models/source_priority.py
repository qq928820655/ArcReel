"""多源优先级的运行态：当前使用到列表第几行、连续几次额度不足、最近一次每日重置。

配置（开关与有序列表）不在这里：全局走 ``system_setting``、项目走 ``project.json``。这张表只承载
「哪一行生效」这类高频易变的状态，避免每次失败都回写 ``project.json`` 那份热读文件。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, utc_now

SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

#: 全局作用域在 ``project_name`` 列上的占位值：主键四列都不允许 NULL，全局行给空串。
GLOBAL_PROJECT_NAME = ""


class SourcePriorityState(Base):
    """一个 ``(作用域, 项目, 媒体类型)`` 的多源优先级运行态。

    ``active_index`` 是当前生效的列表行下标；``quota_streak`` 与 ``network_streak`` 分别是该行
    **连续额度不足**与**连续网络不可达**的次数，分开累计、互不干扰：任一成功即清零，任一达到阈值
    时下移一行并把两者一起清零。``last_reset_on`` 是最近一次每日重置的北京日期，启动补偿与零点
    任务都按它幂等——同一天重复跑不会重复推进。
    """

    __tablename__ = "source_priority_state"
    __table_args__ = (Index("idx_source_priority_state_media_type", "media_type"),)

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    project_name: Mapped[str] = mapped_column(String(64), primary_key=True, default=GLOBAL_PROJECT_NAME)
    media_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    active_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quota_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    network_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_reset_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
