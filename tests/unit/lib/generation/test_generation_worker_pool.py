"""汇总源在 worker 认领时的展开：纯内存选成员、钉住身份、满池/全不可用时回队。

只验证外部可观察行为：选中哪个成员、是否写了钉住身份、回队还是失败。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from lib.config.source_pool import QUOTA_STREAK_THRESHOLD, MemberAvailability, PoolMember
from lib.generation.generation_worker import CapacityTable, GenerationWorker, PoolTable
from tests.fakes import refuse_resume_execution


class _PoolQueue:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self._tasks = list(tasks)
        self.pinned: list[tuple[str, str, str, int]] = []
        self.requeued: list[str] = []
        self.failed: list[str] = []
        self.claimed: list[str] = []
        self.done = asyncio.Event()

    async def acquire_or_renew_worker_lease(self, **_kwargs: Any) -> bool:
        return True

    async def release_worker_lease(self, **_kwargs: Any) -> None:
        return None

    async def list_orphan_tasks_on_start(self) -> list[dict[str, Any]]:
        return []

    async def claim_next_task(self, media_type: str, **_kwargs: Any) -> dict[str, Any] | None:
        if self._tasks:
            task = self._tasks.pop(0)
            self.claimed.append(task["task_id"])
            return task
        return None

    async def pin_pool_execution_identity(
        self, task_id: str, *, provider_id: str, model_id: str, source_pool_id: int
    ) -> None:
        self.pinned.append((task_id, provider_id, model_id, source_pool_id))

    async def persist_execution_provider_id(self, task_id: str, provider_id: str) -> None:
        return None

    async def requeue_task(self, task_id: str) -> bool:
        self.requeued.append(task_id)
        return True

    async def mark_task_succeeded(self, task_id: str, result: dict[str, Any]) -> int:
        self.done.set()
        return 1

    async def mark_task_failed(self, task_id: str, message: str) -> int:
        self.failed.append(task_id)
        self.done.set()
        return 1


def _pool_worker(queue: _PoolQueue, pools: PoolTable, capacity: dict[str, int]) -> GenerationWorker:
    async def execute(task: dict[str, Any], *, claimed_provider_id: str) -> dict[str, Any]:
        return {"provider": claimed_provider_id}

    async def no_settle(*, taskless_started_before: datetime | None) -> int:
        return 0

    worker = GenerationWorker(
        queue=queue,
        capacity=CapacityTable(
            _limits={pid: {"video": cap} for pid, cap in capacity.items()},
            _defaults={"video": 3},
        ),
        slots=None,
        provider_projection=lambda task: "custom-2",
        executor=execute,
        lanes=("video",),
        settle_interrupted_calls=no_settle,
        resume_executor=refuse_resume_execution,
    )
    worker._pools = pools
    return worker


@pytest.mark.asyncio
async def test_pool_task_expands_to_least_loaded_member_and_pins() -> None:
    members = (
        PoolMember(position=0, provider_id="custom-2", model_id="m"),
        PoolMember(position=1, provider_id="custom-5", model_id="m"),
    )
    queue = _PoolQueue([{"task_id": "t1", "media_type": "video", "provider_id": "pool-1"}])
    worker = _pool_worker(queue, PoolTable(_members={1: members}, _availability={}), {"custom-2": 3, "custom-5": 3})
    # custom-2 已占 1，custom-5 空闲 → 应选 custom-5
    worker._slots.register("custom-2", "video", "other", asyncio.get_running_loop().create_future())

    await worker._claim_tasks()

    assert queue.pinned == [("t1", "custom-5", "m", 1)]


@pytest.mark.asyncio
async def test_pool_task_requeued_when_all_members_full() -> None:
    members = (PoolMember(position=0, provider_id="custom-2", model_id="m"),)
    queue = _PoolQueue([{"task_id": "t1", "media_type": "video", "provider_id": "pool-1"}])
    worker = _pool_worker(queue, PoolTable(_members={1: members}, _availability={}), {"custom-2": 1})
    requeued: list[str] = []

    async def _record_requeue(task_id: str) -> bool:
        requeued.append(task_id)
        return True

    worker._requeue_single_task = _record_requeue
    # 占满唯一成员
    worker._slots.register("custom-2", "video", "other", asyncio.get_running_loop().create_future())

    await worker._claim_tasks()

    assert queue.pinned == []
    assert requeued == ["t1"]
    assert queue.failed == []


@pytest.mark.asyncio
async def test_pool_task_requeued_when_all_members_unavailable() -> None:
    members = (
        PoolMember(position=0, provider_id="custom-2", model_id="m"),
        PoolMember(position=1, provider_id="custom-5", model_id="m"),
    )
    queue = _PoolQueue([{"task_id": "t1", "media_type": "video", "provider_id": "pool-1"}])
    availability = {
        "custom-2": MemberAvailability(quota_streak=QUOTA_STREAK_THRESHOLD),
        "custom-5": MemberAvailability(network_streak=5),
    }
    worker = _pool_worker(
        queue, PoolTable(_members={1: members}, _availability=availability), {"custom-2": 3, "custom-5": 3}
    )
    requeued: list[str] = []

    async def _record_requeue(task_id: str) -> bool:
        requeued.append(task_id)
        return True

    worker._requeue_single_task = _record_requeue

    await worker._claim_tasks()

    assert queue.pinned == []
    assert requeued == ["t1"]
    assert queue.failed == []


@pytest.mark.asyncio
async def test_pool_task_never_marked_failed_when_no_member() -> None:
    """池成员为空 / 全不可用绝不 mark_failed：保持可重试，与供应商池满同口径。"""
    queue = _PoolQueue([{"task_id": "t1", "media_type": "video", "provider_id": "pool-9"}])
    worker = _pool_worker(queue, PoolTable(_members={}, _availability={}), {})
    requeued: list[str] = []

    async def _record_requeue(task_id: str) -> bool:
        requeued.append(task_id)
        return True

    worker._requeue_single_task = _record_requeue

    await worker._claim_tasks()

    assert queue.failed == []
    assert requeued == ["t1"]


@pytest.mark.asyncio
async def test_pool_expand_syncs_payload_bucket_keys_into_memory_task() -> None:
    """回归：展开只改 provider_id 而不补 payload 桶键时，执行侧会把身份剥掉、按项目配置重解析回 pool-1。

    执行入口读的是内存 task 的 payload（不是数据库），故展开必须把选定成员的桶键同步进内存字典，
    同时保留入队时的其它 payload 字段（如 script_file）。
    """
    members = (PoolMember(position=0, provider_id="custom-10", model_id="seedance-2.0-mini"),)
    queue = _PoolQueue([])
    worker = _pool_worker(queue, PoolTable(_members={1: members}, _availability={}), {"custom-10": 3})
    task = {
        "task_id": "t1",
        "media_type": "video",
        "provider_id": "pool-1",
        "payload": {"script_file": "episode_74.json"},
    }

    expanded = await worker._expand_pool_task(task, "video")

    assert expanded is not None
    assert expanded["provider_id"] == "custom-10"
    assert expanded["payload"]["video_provider_i2v"] == "custom-10/seedance-2.0-mini"
    assert expanded["payload"]["video_provider_r2v"] == "custom-10/seedance-2.0-mini"
    # 入队时的其它字段不被覆盖
    assert expanded["payload"]["script_file"] == "episode_74.json"


@pytest.mark.asyncio
async def test_warn_empty_pool_distinguishes_empty_table_from_missing_pool(caplog: pytest.LogCaptureFixture) -> None:
    """缺口③回归：池表整表为空 vs 单池缺失要给出不同告警，前者提示 reload 未执行。

    整表空是"启动没 reload / 加载失败"的静默故障形态，任务会持续回队空转；单池缺失只是配置问题。
    """
    import logging

    caplog.set_level(logging.WARNING)

    # 整表空：任务却是池任务 → 提示 reload 未执行
    empty_queue = _PoolQueue([])
    empty_worker = _pool_worker(empty_queue, PoolTable(_members={}, _availability={}), {})
    empty_worker._warn_empty_pool({"task_id": "t1"}, 1)
    assert any("池表为空" in r.message for r in caplog.records)

    caplog.clear()

    # 有池但缺该池 → 提示池被禁用/删除，而不是 reload 未执行
    other = (PoolMember(position=0, provider_id="custom-2", model_id="m"),)
    partial_worker = _pool_worker(_PoolQueue([]), PoolTable(_members={2: other}, _availability={}), {})
    partial_worker._warn_empty_pool({"task_id": "t2"}, 1)
    assert any("不在已装载的池表中" in r.message for r in caplog.records)
    assert not any("池表为空" in r.message for r in caplog.records)
