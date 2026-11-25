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
        self.pinned: list[tuple[str, str, str]] = []
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

    async def pin_pool_execution_identity(self, task_id: str, *, provider_id: str, model_id: str) -> None:
        self.pinned.append((task_id, provider_id, model_id))

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

    assert queue.pinned == [("t1", "custom-5", "m")]


@pytest.mark.asyncio
async def test_pool_task_requeued_when_all_members_full() -> None:
    members = (PoolMember(position=0, provider_id="custom-2", model_id="m"),)
    queue = _PoolQueue([{"task_id": "t1", "media_type": "video", "provider_id": "pool-1"}])
    worker = _pool_worker(queue, PoolTable(_members={1: members}, _availability={}), {"custom-2": 1})
    requeued: list[str] = []

    async def _record_requeue(task_id: str) -> bool:
        requeued.append(task_id)
        return True

    worker._requeue_single_task = _record_requeue  # type: ignore[method-assign]
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

    worker._requeue_single_task = _record_requeue  # type: ignore[method-assign]

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

    worker._requeue_single_task = _record_requeue  # type: ignore[method-assign]

    await worker._claim_tasks()

    assert queue.failed == []
    assert requeued == ["t1"]
