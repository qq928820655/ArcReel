---
status: accepted
---

# 项目详情摘要按文件签名缓存 verified 比对，避免高频刷新拖垮详情接口

项目详情（`GET /projects/{name}`）与项目列表同用 `WorkflowStateService.get_project_summary`，但详情取 `verified` 口径：每集每件产物都要重建规范基线、哈希分镜图与其引用图，才能区分 current 与 stale（见 ADR 0062）。几十集、上千件产物的项目单次比对要数秒。

生成高峰期前端由 SSE 事件高频触发 `refreshProject`，同一份未变动的项目被反复全量比对，在 2 核机器上与视频轮询、Agent 抢占 CPU 后排队，单个详情请求可冲到 20 秒以上，越过前端默认的 20 秒请求超时，用户看到「同步项目变更失败：请求超时」。

决定在 `get_project_summary` 内为 `verified` 结果加一层进程级缓存：以项目目录下参与产物时效判定的文件的 `(相对路径, 大小, mtime_ns)` 快照作签名，签名不变且未过短 TTL 时直接复用；任一不成立就重算并写回。签名只读文件元数据，两千文件的项目实测 40-60ms，远低于一次全量比对。`renders/`、`presentations/`、`subtitles/`、`versions/` 目录不纳入签名——它们体积最大、生成期间频繁变化，且不参与产物时效判定；`versions/versions.json` 单独取这一个文件，因为它影响快照定址。

## Consequences

- 缓存按项目目录绝对路径作键、挂在类上跨实例共享：路由每次请求都新建 `WorkflowStateService`，缓存挂实例上等于每次落空。
- `registered` 不读写此缓存：列表页一次一个项目、本身足够快，且两种口径不能互相顶替。
- TTL 只兜底「签名没覆盖到的输入被外部改动」这类边角情形，不做正确性担保；正确性由签名覆盖的项目内文件变更保证。
- 缓存对象是 `ProjectSummary` 模型实例，调用方须只读；现状所有消费方都只读。
- 测试需在用例前后清缓存（`reset_summary_cache_for_tests`，已挂进 `conftest` 的 `isolated_data_root`）。
