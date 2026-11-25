---
status: accepted
---

# 视频汇总源：以虚拟 provider/model 暴露一个容量感知的成员池，认领时展开、执行时钉住

现有「多源优先级」（ADR 0121）是有序列表 + 单活跃行：额度或网络失败时指针下移一行，同一时刻只有一个源在跑。这满足「用完再换」，但满足不了「多家同时跑」——用户希望把 a-dola、b-dola 这类已配置供应商的模型汇成一个「总源」，任务指向它，由它按各家剩余并发把任务铺开，从而同时跑满多家、又不超过每家自己的并发上限。

决定引入**汇总源**：一个可命名、可建多个的虚拟来源，成员是已配置供应商的模型。汇总源对外就是一个 `provider/model`——虚拟 provider id 形如 `pool-1`，虚拟 model 名用户自定（如 `aggregate`）。因此它能原样出现在现有所有「选一个源」的位置：默认视频模型、i2v/r2v 桶、以及多源优先级列表的某一行，选择层不需要任何新概念。

## 分配发生在 worker 认领时，不在解析层

并发占用（谁在跑几个）只存在于 worker 内存（`SlotTable`），配置上限在 DB（`CapacityTable`）。解析层（`lib.config`）看不到实时占用，也不能反向依赖 worker。所以池的「分配」只能在 worker 认领任务时做：

```
配置/选择层           入队                  worker 认领                    执行
pool-1/aggregate  →  task.provider_id  →  展开成真实成员               assemble_backend
(虚拟身份)            = "pool-1"           custom-2/seedance-2.0        照常跑真实成员
```

`assemble_backend`、能力闸、`ensure_dispatch_provider` 全部按真实成员工作，执行层无需认识「池」这个概念。

## 分配策略：最少在跑优先，同负载按列表顺序

遍历池成员，先剔除「当天不可用」（额度/网络 streak 达标，复用 ADR 0121 的判定与阈值）与「占用已达该成员并发上限」的，在剩余成员里取占用数最小者；占用数相同时取列表位置靠前者。这样并发利用率高、负载自然均衡，同时保留用户排的先后作为平局裁决。

## 性能约束（本次设计的硬约束）

用户明确要求不得引入性能退化。逐条钉死：

1. **展开纯内存，认领时不查库。** 池成员与成员可用状态随 `reload_limits` 一起装进内存 `PoolTable`，只在改配置时 reload（与 `CapacityTable` 同一时机）。worker 每秒一个 cycle（`TASK_POLL_INTERVAL_SEC=1.0`），若每轮查 DB 就是每秒 N 次查询，直接放大现有卡顿。展开零 I/O。

2. **池满在 SQL 层过滤，禁止空转 claim→requeue。** 池任务的 `provider_id` 是 `pool-1`，永远不进现有 `_pool_full_providers` 黑名单（那是具体供应商 id），池满时会被每轮 claim→判满→requeue，每秒一次空转加一次 UPDATE。必须与供应商池满同口径：在 claim 前算出「池满集合」一并传给 `claim_next`，池满任务在 SQL 层就被跳过。

3. **展开结果落库，不每轮重算。** 选中成员后写进 `task.provider_id`（复用 `persist_execution_provider_id`），下一轮直接按真实成员走，不再进池展开分支；也天然避免同一任务每轮重新分配导致成员漂移。

4. **失败写库不增加频率。** 成员额度/网络状态在一次任务终态时写一次（复用 `task_outcome` 的调用时机与 `source_priority_state` 的写模式），不是每次轮询写。

5. **候选枚举只加一轮内存拼接。** 汇总源作为视频候选追加进 `_build_options` 的 `video_backends`，不触达目录扫描的额外 DB 查询。

## 执行身份必须钉住（唯一动到漂移机制的点）

现有队列执行会先 `without_video_execution_identity` 剥掉 payload 里的身份，再按当前配置重解析，好让排队任务跟随配置切换。池任务恰好相反：它必须执行**认领时分配的那个成员**，不能被重解析回池。

做法：池任务展开时，把成员同时写进 `task.provider_id` 与 payload 的 `video_provider_<bucket>` 钉住键。执行侧对「配置解析为池、但任务行已带真实成员分配」的任务，保留钉住身份、跳过重解析——这与续跑复用同一套「payload 锁定身份即跳过骨架」的机制。只对池展开过的任务生效，现有单源任务的剥身份+重解析行为完全不变。

这条是本次唯一触碰 ADR 0121 漂移机制的地方，回归风险集中在此：必须保证单源任务的既有路径逐字节不变。

## 数据模型

- `video_source_pool`：`id` / `name` / `enabled`。本期只服务视频，不建 `media_type` 列——需要时再加，避免现在留一个恒为 `video` 的空列。
- `video_source_pool_member`：`pool_id` / `position` / `provider_id` / `model_id`。写入时校验指向已配置、已启用且媒体类型为 video 的模型；成员按 `position` 排序即分配平局顺序。
- `source_pool_member_state`：`(pool_id, position)` / `quota_streak` / `network_streak` / `last_reset_on`。语义与 `source_priority_state` 一致，北京零点重置。

## 与多源优先级的关系

不统一、不替换。ADR 0121 的有序列表是「顺序失败转移」策略；汇总源是「容量分配」策略。二者正交：用户可以把 `pool-1` 放进多源优先级列表的第一行，形成「先铺开池，池全耗尽再下移到某个直连源」。这样两种策略可组合，无需给多源优先级加「策略」维度，避免迁移现有配置。

## 明确不采用

- **在解析层展开池**：那里没有实时占用，展开结果必然与实际并发脱节。
- **让执行层认识「池」**：执行侧只该看到真实 provider/model；认识池会把虚拟身份泄漏到装配、能力、计费各层。
- **展开结果不落库、每轮重算**：会造成成员漂移与每秒空转，直接违反性能约束。
- **池满时 mark_failed**：与现有供应商池满一致，应保持 queued 等容量，绝不因一时占满判失败。

## Consequences

- `_trusted_payload_provider`、`validate_backend_value` 增加 `pool-` 前缀识别；`_ensure_video_identity_resolvable` 对池身份校验「池存在且至少一个成员可用」。
- 模型引用扫描把池身份与池成员都算作引用，删除成员模型时能提示哪些池在用。
- 前端：汇总源 CRUD、成员选择器（只列已配置视频模型）、`video_backends` 候选追加池、四语言文案。
- 测试需覆盖：纯内存展开不查库、池满 SQL 过滤不空转、分配策略（最少在跑 + 平局顺序）、执行身份钉住后不漂移、失败标记成员不可用、单源任务路径不变。
