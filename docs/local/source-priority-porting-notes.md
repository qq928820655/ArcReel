# 本地功能：多源优先级（不进上游）

> 本目录记录**仅存在于本地 fork** 的功能改动，供升级上游后重新补回，避免被覆盖或还原。
> 这些文件不随 PR 提交到上游 `ArcReel/ArcReel`。升级上游后请按本文逐条重放。
>
> 对应提交：`a22e25352`（多源优先级）、`b8f24a916`（导出修复，见文末）
> 对应版本：0.37.0

---

## 一、功能是什么

视频 / 图片 / 文本三条通道，各有一份**有序的 `(provider, model)` 列表 + 独立开关**，取代该通道的默认模型配置：

- 当前来源**连续 2 次**额度不足 → 下移一行；末行用完停住不换。
- 每天**北京时间 00:00** 回到首行（日期粒度幂等，非「每 24 小时」）。
- 开关关闭 → 整条链路不参与解析，行为与没有这个功能**完全等价**（含零额外查库）。
- 层级：项目层多源 > 全局层多源 > 既有四级骨架。
- 本期**只有视频接入解析**（`WIRED_MEDIA_TYPES={"video"}`）；图片/文本只落配置与界面。

设计取舍见 `docs/adr/0121-source-priority-ordered-list-per-channel.md`。

---

## 二、数据分层（升级后先确认这两处）

| 层 | 位置 | 说明 |
|---|---|---|
| 配置·全局 | 表 `system_setting`，键 `source_priority_<video\|image\|text>`，值为 JSON | 走既有设置通道，不新建表 |
| 配置·项目 | `project.json` 的 `source_priority` 字段 | `{video:{enabled,entries}, image:..., text:...}` |
| 运行态 | 新表 `source_priority_state` | 主键 `(scope, project_name, media_type)` |

`scope` 取 `global`/`project`；全局行的 `project_name` 用空串占位（主键列不允许 NULL）。

---

## 三、新增文件（整体复制即可，通常不冲突）

### 后端核心

```
lib/config/source_priority.py            # 配置/运行态核心：Entry/Config/StateRow/Store/advance/... 395 行
lib/config/source_priority_service.py    # SourcePriorityService：任务终态→推进，作 outcome_listener 147 行
lib/config/source_priority_reset.py      # 每日重置循环 + 启动补偿 65 行
lib/generation/quota_exhaustion.py       # classify_quota_exhaustion 额度判定 155 行
lib/db/models/source_priority.py         # ORM：SourcePriorityState 42 行
server/routers/source_priority.py        # 4 个 API 端点 258 行
```

### 迁移

```
alembic/versions/b7c1d9e4a2f3_add_source_priority_state.py   # 建表
alembic/versions/c8e4f1a6b2d5_add_source_priority_network_streak.py  # 加 network_streak 列
```

**升级后必须重挂迁移链**：该文件 `down_revision = "f6d2a8c4b931"`。上游若在此之后新增了迁移，
新 head 会与它分叉，`alembic heads` 会报多个 head。处理方式：

```bash
# 1. 看当前上游 head
uv run alembic heads

# 2. 把本迁移的 down_revision 改成上游最新 head，revision id 保持不变（避免与既有库版本号冲突）
#    编辑 alembic/versions/b7c1d9e4a2f3_add_source_priority_state.py 的 down_revision

# 3. 确认单 head
uv run alembic heads
```

若本地库已升到 `b7c1d9e4a2f3`，改 down_revision 后不要 `downgrade`，直接 `upgrade head` 即可。

**两个迁移是链式的**：`c8e4f1a6b2d5.down_revision = "b7c1d9e4a2f3"`。重挂时只需改**链头** `b7c1d9e4a2f3` 的 `down_revision` 指向上游最新 head，`c8e4f1a6b2d5` 保持在它之后，不要动。否则会再次分叉。

### 前端

```
frontend/src/components/shared/SourcePrioritySection.tsx   # 编辑器（全局/项目共用）365 行
```

### 文档

```
docs/adr/0121-source-priority-ordered-list-per-channel.md
```

### 测试（升级后用来验证重放是否完整）

```
tests/unit/lib/config/test_source_priority.py                    # 配置/推进/Store
tests/unit/lib/config/test_source_priority_service.py            # 终态→推进
tests/unit/lib/config/test_source_priority_reset.py              # 每日重置
tests/unit/lib/generation/test_quota_exhaustion.py               # 额度判定
tests/integration/server/routers/test_source_priority_router.py  # 4 端点
tests/integration/lib/db/migrations/test_alembic_source_priority_state.py
tests/integration/lib/config/test_config_resolver.py::TestSourcePriorityOffCost  # 关闭态零开销（见第六节）
```

---

## 四、修改文件（升级时最易冲突，逐条核对）

> 行号是 0.37.0 时的位置，仅作定位参考；升级后按「锚点」搜代码。

### 1. `lib/db/models/__init__.py`

- import 区加：`from lib.db.models.source_priority import SourcePriorityState`
- `__all__` 里加 `"SourcePriorityState"`

### 2. `lib/config/resolver.py`（核心接入，改动最大）

**(a) 模块级新增 helper**（锚点：`_video_bucket_reference_unavailable` 之后）

```python
def _source_priority_generation_type(generation_type: VideoGenerationType | None) -> VideoGenerationType:
    return generation_type if generation_type is not None else "i2v"
```

**(b) `resolve_video_backend` 加参数**（锚点：该函数签名）

```python
# fmt: off
project_name: str | None = None,
# fmt: on
```

并透传给 `_resolve_video_provider_model(..., project_name=project_name)`。

**(c) `_resolve_layered_backend` 加可选 settings 快照**（关键：避免关闭态多查一次库）

```python
async def _resolve_layered_backend(self, svc, session, project, keys, *, settings: dict[str, str] | None = None):
    ...
    if settings is None:
        settings = await svc.get_all_settings()
```

**(d) 新增 `_resolve_source_priority_video`**（锚点：`_resolve_video_provider_model` 之前）

要点，勿照抄旧实现：
- 先读 `read_project_config(project, "video")`；未生效才看全局。
- **全局配置从传入的 `settings` 快照解析**（`SourcePriorityConfig.from_json(settings.get(setting_key("video"), ""))`），
  **不要**再 `store.read_global_configs()`——那是额外一次 `get_prefixed` 查询，关闭态会退化。
- 未开启直接 `return None`，不碰运行态表（这样 `session=None` 的调用方也不崩）。

**(e) `_resolve_video_provider_model` 里接线**（锚点：`if submitted_job:` 分支之后）

```python
settings = await svc.get_all_settings()
source_priority = await self._resolve_source_priority_video(
    session, project, generation_type, project_name=project_name, settings=settings
)
if source_priority is not None:
    return source_priority
# 之后的两次 _resolve_layered_backend 调用都传 settings=settings
```

顺序约束：payload 锁定与续跑分流必须在多源之前——续跑换源会轮询到别家厂商的任务。

### 3. `lib/generation/generation_worker.py`

- 新增 `TaskOutcomeListener` Protocol（锚点：`TaskExecutor` 之后）
- `__init__` 加 `outcome_listener: TaskOutcomeListener | None = None`，存 `self._outcome_listener`
- 新增 `_notify_outcome`，**内部 try/except 吞掉所有异常**——绝不能影响任务已落定的结果
- 在成功、失败两条终态路径各调一次 `await self._notify_outcome(...)`

### 4. `lib/generation/generation_queue.py`

`resolve_video_execution_for_queued_task` 里 `resolve_video_backend(...)` 加 `project_name=project_name`。

### 5. `server/services/tasks/generation_context.py`

`resolve_video_backend(...)` 调用处加 `project_name=project_name`。

### 6. `server/app.py`

四处：

1. `from server.routers import (...)` 里加 `source_priority,`
2. `lifespan` 启动段（锚点：`start_trash_expiry` 附近）：
   ```python
   # fmt: off
   from lib.config.source_priority_reset import start_source_priority_reset, stop_source_priority_reset
   source_priority_reset = start_source_priority_reset(async_session_factory)
   # fmt: on
   ```
3. `lifespan` finally 段：`await stop_source_priority_reset(source_priority_reset)`
4. `create_generation_worker()`：注入 `outcome_listener=SourcePriorityService(async_session_factory)`
5. 路由注册（锚点：`system_config.router` 之后）：
   ```python
   # fmt: off
   app.include_router(source_priority.router, prefix="/api/v1",
                      dependencies=[Depends(get_current_user)], tags=["多源优先级"])
   # fmt: on
   ```

### 7. `server/services/project/model_references.py`

- 加 `from dataclasses import dataclass, field`（field）
- `ProjectModelReferences` 加字段 `source_priority_references: Mapping[str, str] = field(default_factory=dict)`
- `project_model_references()` 里遍历三通道 entries，填 `priority_references`

目的：删模型时确认框能列出多源列表里的引用。

### 8. `server/routers/system_config.py`

`_referenced_models` 里把全局多源列表的每一行也算作引用（媒体类型 = 所在通道）。

### 9. `server/routers/provider_models.py`

`_project_references` 判定加 `or reference in project.source_priority_references`。

### 10. 前端

**`frontend/src/app-routes.ts`**
- `SETTINGS_SECTIONS` 加 `"source-priority"`（放在 `"default-models"` 之后）
- `PROJECT_SETTINGS_TABS` 加 `"source-priority"`（放在 `"models"` 之后）

**`frontend/src/components/pages/SystemConfigPage.tsx`**
- import `ListOrdered`（lucide）、`SourcePrioritySection`
- `SECTIONS` 加 `"source-priority"` 项
- `SECTION_GROUPS` 的 generation 分组加 `"source-priority"`
- `SectionContent` 加 `case "source-priority": return <SourcePrioritySection scope="global" />;`

**`frontend/src/components/pages/ProjectSettingsPage.tsx`**
- import `ListOrdered`、`SourcePrioritySection`
- `TABS` 加 `"source-priority"`
- `TAB_GROUPS` 的 project 分组加 `"source-priority"`
- `switch(tab)` 加 `case "source-priority":` 渲染 `<SourcePrioritySection scope="project" projectName={projectName} />`

**`frontend/src/types/system.ts`**
加 `SourcePriorityEntry` / `SourcePriorityChannel` / `SourcePriorityChannelView` /
`SourcePriorityResponse` / `SourcePriorityPatch` / `SourcePriorityMediaType`。

**`frontend/src/api.ts`**
加 6 个方法：`getGlobalSourcePriority` / `updateGlobalSourcePriority` / `selectGlobalSourcePriority` /
`getProjectSourcePriority` / `updateProjectSourcePriority` / `selectProjectSourcePriority`。

**i18n（zh / en / vi / ru 各 24 个键）**
键前缀 `source_priority_*`，外加 `settings_source_priority`、`project_settings_tab_source_priority`。
四个语言的键集合必须一致（`satisfies Record<keyof typeof enDashboard, string>` 会在 typecheck 卡住漏翻）。

---

## 五、API 端点（升级后回归）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/v1/system/source-priority` | 全局配置 + 运行态 |
| PATCH | `/api/v1/system/source-priority` | 写全局配置（只更新出现的通道） |
| POST | `/api/v1/system/source-priority/select` | 手动切全局当前行 |
| GET | `/api/v1/projects/{name}/source-priority` | 项目配置 + 运行态（未配置通道回落全局） |
| PATCH | `/api/v1/projects/{name}/source-priority` | 写项目配置 |
| POST | `/api/v1/projects/{name}/source-priority/select` | 手动切项目当前行（项目层未配置时 409） |

注意：`patch_project_source_priority` 写 `project.json`，**必须**用
`run_sync_transaction` + 线程函数体内 `project_change_source("webui")` 包住（contextvar 不跨
`asyncio.to_thread` 传播）。`select` 只写运行态表，登记在
`tests/unit/server/routers/test_web_write_change_source_marking.py` 的 `_EXEMPT` 里。

---

## 六、升级后必查的坑（都是踩过的）

1. **关闭态零额外查库**：全局配置必须从 `get_all_settings()` 的快照里取，不能单独
   `read_global_configs()`。回归测试 `TestSourcePriorityOffCost` 会卡住这一点——升级后先跑它。
2. **`session=None` 不崩**：未开启时不能碰运行态表。部分调用方（费用估算、限流路由）不传 session。
3. **迁移分叉**：上游新增迁移后要改 `down_revision`，见第三节。
4. **i18n 四语言键集合一致**：漏一个语言 typecheck 就挂。
5. **测试替身签名**：任何 fake 的 `resolve_video_backend` 都要接受 `project_name=` 关键字
   （`tests/unit/lib/generation/test_generation_queue.py` 已改）。
6. **`_notify_outcome` 吞异常**：observer 失败不能影响任务结果，否则一个额度判定 bug 会连带任务失败。

---

## 七、验证命令

```bash
# 后端定向（最快确认重放完整）
uv run pytest tests/unit/lib/config/test_source_priority*.py \
  tests/unit/lib/generation/test_quota_exhaustion.py \
  tests/integration/server/routers/test_source_priority_router.py \
  tests/integration/lib/db/migrations/test_alembic_source_priority_state.py \
  "tests/integration/lib/config/test_config_resolver.py::TestSourcePriorityOffCost" -q

# 迁移单 head
uv run alembic heads

# 前端
cd frontend && npx tsc --noEmit && npx eslint src
```

---

## 八、另一笔本地提交：导出 500 修复（`b8f24a916`）

不属于多源功能，但同属本地 fork。**这是上游也会遇到的真实 bug**，升级后若上游未修需重放：

- `lib/artifacts/artifact_planner.py`：无原文跳过该集时显式 `_skip(...)` 记账
- `lib/artifacts/artifact_activation.py`：rebase 时对「规划器主动跳过」的 claim 直接丢弃；
  路径凭空消失、规划器没见过的仍报错（不放宽真正的校验）
- 测试：`tests/integration/lib/artifacts/test_script_plan_basis.py::test_export_drops_script_plan_claim_of_an_episode_without_source`

判据：规划器把同一身份记进了 `plan.skipped` → 丢弃；否则 → 报错。

---

## 九、快速重放方式

若整段重放，可从一个干净的本地 tag 取文件：

```bash
# 本功能的完整改动范围（相对上一提交）
git diff --stat b8f24a916 a22e25352

# 只看某个文件的改法
git diff b8f24a916 a22e25352 -- lib/config/resolver.py

# 把新增文件整体拷出（不依赖 git 历史）
git show a22e25352:lib/config/source_priority.py > /tmp/source_priority.py
```

---

## 十、网络不可达触发（`c8e4f1a6b2d5`）

与额度触发并列的第二种下移条件：当前来源**连不上**时也换下一行。

### 改了什么

| 文件 | 改动 |
|---|---|
| `lib/generation/quota_exhaustion.py` | 新增 `classify_network_unreachable()` 与 `_NETWORK_UNREACHABLE_ERRORS`（`ConnectError`/`ConnectTimeout`/`PoolTimeout`/`ProxyError`），沿 `__cause__` 因果链查 |
| `lib/config/source_priority.py` | `advance()` 改返回三元组 `(active_index, quota_streak, network_streak)`，新增 `network_streak`/`network_unreachable`/`network_threshold` 参数；`NETWORK_STREAK_THRESHOLD=5`、`network_streak_threshold()`（每次读 env `ARCREEL_SOURCE_PRIORITY_NETWORK_THRESHOLD`）；`SourcePriorityStateRow.network_streak`；Store 全链（read/_upsert/set/apply_outcome/reset）带 `network_streak` |
| `lib/config/source_priority_service.py` | `TaskOutcome.network_unreachable`；`task_outcome` 分判；`_apply` 传 `network_unreachable` |
| `lib/db/models/source_priority.py` | `network_streak` 列（`server_default="0"`） |
| `alembic/versions/c8e4f1a6b2d5_...py` | 加列迁移，`down_revision="b7c1d9e4a2f3"` |
| 四语言 i18n | `source_priority_quota_note` 文案补网络触发说明 |
| ADR 0121 | 新增「连不上供应商的判定」一节 |

### 判定口径（改动时别放宽）

- 只收**连接建立阶段**的错误（`ConnectError`/`ConnectTimeout`/`PoolTimeout`/`ProxyError`）——请求确定没送达，换源不会重复计费。
- **不收**读超时/写错误/中途断连（请求可能已送达，换源会重复计费或轮询到别家任务）。
- 与下游工具请求的网络异常无关。
- 阈值 5 次，比额度的 2 次宽（连接失败可能是本机抖动/DNS/入口短暂不可达）。
- 与额度**分开计数**，合并会误切；任一达阈值即下移并把两计数一起清零。

### 验证

```bash
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u GEMINI_API_KEY -u ARK_API_KEY \
  uv run pytest tests/unit/lib/config/test_source_priority.py \
  tests/unit/lib/config/test_source_priority_service.py \
  tests/unit/lib/generation/test_quota_exhaustion.py \
  tests/integration/lib/db/migrations/test_alembic_source_priority_state.py -q
```

### 本地库升级

```bash
uv run alembic upgrade head
```

`source_priority_state` 表会多出 `network_streak` 列，旧行默认 0，无需回填。
