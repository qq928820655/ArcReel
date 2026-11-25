"""项目 ``project.json`` 里的模型引用：每个项目的模型设置字段各引用了哪个 ``provider/model``。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from lib.config import resolver
from lib.config.model_setting_keys import PROJECT_MODEL_SETTING_KEYS
from lib.config.source_priority import SOURCE_PRIORITY_MEDIA_TYPES, read_project_config


@dataclass(frozen=True)
class ProjectModelReferences:
    name: str
    title: str | None
    #: 模型设置字段 → 所引的 ``provider/model``；只收非空字符串值。
    references: Mapping[str, str]
    #: 多源优先级列表里的 ``provider/model`` → 媒体类型。一个通道可有多行，故单列一份、
    #: 不复用按字段名的 ``references``：那里一个键只对应一个值。
    source_priority_references: Mapping[str, str] = field(default_factory=dict)


def project_model_references() -> list[ProjectModelReferences]:
    """逐个读项目的 ``project.json``，取出模型设置字段的引用；读不了的项目跳过。

    只读 JSON，不取锁、不迁移（``ProjectManager.load_project``），耗时与项目数成正比。同步文件 I/O，异步调用方放到线程里。
    """
    pm = resolver.get_project_manager()  # 经模块属性取：用例按 lib.config.resolver 替换项目管理器
    found: list[ProjectModelReferences] = []
    for name in pm.list_projects():
        try:
            project = pm.load_project(name)
        except (OSError, ValueError):
            continue
        title = project.get("title")
        priority_references: dict[str, str] = {}
        for media_type in SOURCE_PRIORITY_MEDIA_TYPES:
            for entry in read_project_config(project, media_type).entries:
                priority_references[entry.pair_key] = media_type
        found.append(
            ProjectModelReferences(
                name=name,
                title=title if isinstance(title, str) and title else None,
                references={
                    key: value
                    for key in PROJECT_MODEL_SETTING_KEYS
                    if isinstance(value := project.get(key), str) and value
                },
                source_priority_references=priority_references,
            )
        )
    return found
