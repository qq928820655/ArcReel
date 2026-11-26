"""视频汇总源 API：CRUD（含统一模型名/能力参考）与监控端点。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.db import get_async_session
from lib.db.models.api_call import ApiCall
from lib.db.models.task import Task
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import source_pool

AUTH_DEPENDENCIES = [pytest.importorskip("fastapi").Depends(get_current_user)]

#: 内置视频模型，作为池成员的合法引用。
VEO = ("gemini-vertex", "veo-3.1-generate-001")


@pytest.fixture
def pool_app(db_factory: async_sessionmaker[AsyncSession]) -> TestClient:
    async def _override_session():
        async with db_factory() as session:
            yield session

    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="testuser", role="admin")
    app.dependency_overrides[get_async_session] = _override_session
    app.include_router(source_pool.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return TestClient(app)


def _pool_body(name="主池", model_name=None, capability=None, members=None):
    return {
        "name": name,
        "model_name": model_name,
        "capability_provider_id": capability[0] if capability else None,
        "capability_model_id": capability[1] if capability else None,
        "enabled": True,
        "members": [{"provider_id": p, "model_id": m} for p, m in (members or [VEO])],
    }


def test_create_and_list_pool_with_model_name(pool_app):
    resp = pool_app.post("/api/v1/system/source-pools", json=_pool_body(model_name="seedance 汇总"))
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["model_name"] == "seedance 汇总"
    assert created["provider_id"] == f"pool-{created['id']}"

    listed = pool_app.get("/api/v1/system/source-pools").json()
    assert any(p["id"] == created["id"] and p["model_name"] == "seedance 汇总" for p in listed)


def test_create_rejects_capability_member_without_pair(pool_app):
    body = _pool_body()
    body["capability_provider_id"] = VEO[0]
    body["capability_model_id"] = None
    resp = pool_app.post("/api/v1/system/source-pools", json=body)
    assert resp.status_code == 400


def test_monitor_lists_only_pool_tasks_with_member_and_calls(pool_app, db_factory):
    created = pool_app.post("/api/v1/system/source-pools", json=_pool_body()).json()
    pool_id = created["id"]

    import asyncio

    async def _seed():
        now = datetime.now(UTC)
        async with db_factory() as session:
            session.add(
                Task(
                    task_id="t1",
                    project_name="demo",
                    task_type="video",
                    media_type="video",
                    resource_id="E1S01",
                    status="succeeded",
                    queued_at=now,
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                    provider_id="gemini-vertex",
                    source_pool_id=pool_id,
                    payload_json='{"video_provider_i2v": "gemini-vertex/veo-3.1-generate-001"}',
                )
            )
            # 单源任务（无 source_pool_id）不应出现
            session.add(
                Task(
                    task_id="t2",
                    project_name="demo",
                    task_type="video",
                    media_type="video",
                    resource_id="E1S02",
                    status="succeeded",
                    queued_at=now,
                    updated_at=now,
                    provider_id="gemini-vertex",
                )
            )
            session.add(
                ApiCall(
                    project_name="demo",
                    call_type="video",
                    model="veo-3.1-generate-001",
                    status="success",
                    provider="gemini-vertex",
                    task_id="t1",
                    started_at=now,
                    finished_at=now,
                )
            )
            await session.commit()

    asyncio.run(_seed())

    resp = pool_app.get("/api/v1/system/source-pools/monitor")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["task_id"] == "t1"
    assert item["pool_name"] == "主池"
    assert item["provider_id"] == "gemini-vertex"
    assert item["model_id"] == "veo-3.1-generate-001"
    assert (item["call_count"], item["success_count"]) == (1, 1)


def test_capabilities_view_reports_base_overrides_and_effective(pool_app):
    created = pool_app.post("/api/v1/system/source-pools", json=_pool_body()).json()
    pool_id = created["id"]
    overrides = {"durations": [4, 8, 12]}
    resp = pool_app.put(
        f"/api/v1/system/source-pools/{pool_id}",
        json={**_pool_body(), "capability_overrides": overrides},
    )
    assert resp.status_code == 200, resp.text

    view = pool_app.get(f"/api/v1/system/source-pools/{pool_id}/capabilities").json()
    assert view["media_type"] == "video"
    assert "durations" in view["editable"]
    # base 来自参考成员的原值（未被池覆盖污染）
    assert view["base"]["durations"] != [4, 8, 12]
    assert view["overrides"]["durations"] == [4, 8, 12]
    # effective 是叠加后的值
    assert view["effective"]["durations"] == [4, 8, 12]


def test_capabilities_view_404_for_unknown_pool(pool_app):
    resp = pool_app.get("/api/v1/system/source-pools/99999/capabilities")
    assert resp.status_code == 404
