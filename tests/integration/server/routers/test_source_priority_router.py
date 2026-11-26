"""多源优先级 API：全局与项目的读写、当前行切换、层级回落。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.db import get_async_session
from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import source_priority

AUTH_DEPENDENCIES = [pytest.importorskip("fastapi").Depends(get_current_user)]


@pytest.fixture
def priority_app(db_factory: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """只挂多源优先级路由、绑内存库、以管理员身份免鉴权；``pm`` 指向 ``tmp_path/projects``。"""

    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    monkeypatch.setattr(source_priority, "get_project_manager", lambda: pm)

    async def _override_session():
        async with db_factory() as session:
            yield session

    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="testuser", role="admin")
    app.dependency_overrides[get_async_session] = _override_session
    app.include_router(source_priority.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return {"client": TestClient(app), "pm": pm}


def _channel(enabled: bool, *pairs: tuple[str, str]) -> dict:
    return {"enabled": enabled, "entries": [{"provider_id": p, "model_id": m} for p, m in pairs]}


def test_global_defaults_to_all_channels_off(priority_app):
    resp = priority_app["client"].get("/api/v1/system/source-priority")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "global"
    for media in ("video", "image", "text"):
        assert body[media]["enabled"] is False
        assert body[media]["entries"] == []
        assert body[media]["active"] is False


def test_patch_global_only_touches_named_channels(priority_app):
    client = priority_app["client"]
    resp = client.patch(
        "/api/v1/system/source-priority",
        json={"video": _channel(True, ("ark", "seedance"), ("openai", "sora"))},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["video"]["enabled"] is True
    assert body["video"]["active"] is True
    assert len(body["video"]["entries"]) == 2
    # 未出现的通道保持原样
    assert body["image"]["entries"] == []
    assert body["text"]["entries"] == []


def test_patch_rejects_entry_missing_model_id(priority_app):
    client = priority_app["client"]
    resp = client.patch(
        "/api/v1/system/source-priority",
        json={"video": {"enabled": True, "entries": [{"provider_id": "ark", "model_id": ""}]}},
    )
    assert resp.status_code == 422


def test_global_select_moves_active_index(priority_app):
    client = priority_app["client"]
    client.patch(
        "/api/v1/system/source-priority",
        json={"video": _channel(True, ("a", "m1"), ("b", "m2"))},
    )
    resp = client.post("/api/v1/system/source-priority/select", json={"media_type": "video", "active_index": 1})
    assert resp.status_code == 200
    assert resp.json()["video"]["active_index"] == 1


def test_global_select_out_of_range_is_rejected(priority_app):
    client = priority_app["client"]
    client.patch("/api/v1/system/source-priority", json={"video": _channel(True, ("a", "m1"))})
    resp = client.post("/api/v1/system/source-priority/select", json={"media_type": "video", "active_index": 5})
    assert resp.status_code == 422


def test_project_falls_back_to_global_when_unconfigured(priority_app):
    client = priority_app["client"]
    client.patch("/api/v1/system/source-priority", json={"video": _channel(True, ("g", "gm"))})

    resp = client.get("/api/v1/projects/demo/source-priority")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "global"
    assert body["video"]["active"] is True
    assert body["video"]["entries"][0]["provider_id"] == "g"


def test_project_config_overrides_global(priority_app):
    client = priority_app["client"]
    client.patch("/api/v1/system/source-priority", json={"video": _channel(True, ("g", "gm"))})
    client.patch(
        "/api/v1/projects/demo/source-priority",
        json={"video": _channel(True, ("p", "pm"))},
    )

    body = client.get("/api/v1/projects/demo/source-priority").json()
    # 只配了 video，image / text 仍回落全局，故整体 scope 是 mixed。
    assert body["scope"] == "mixed"
    assert body["video"]["entries"][0]["provider_id"] == "p"
    # 每通道的来源：video 由本项目拥有，image / text 回落全局。前端据此决定点行时是否先落本项目配置。
    assert body["video"]["source"] == "project"
    assert body["image"]["source"] == "global"
    assert body["text"]["source"] == "global"


def test_project_can_clear_channel_to_fall_back_to_global(priority_app):
    client = priority_app["client"]
    client.patch("/api/v1/system/source-priority", json={"video": _channel(True, ("g", "gm"))})
    client.patch("/api/v1/projects/demo/source-priority", json={"video": _channel(True, ("p", "pm"))})
    # 关闭并清空：该通道从项目配置里移除
    client.patch("/api/v1/projects/demo/source-priority", json={"video": _channel(False)})

    body = client.get("/api/v1/projects/demo/source-priority").json()
    assert body["scope"] == "global"
    assert body["video"]["entries"][0]["provider_id"] == "g"


def test_project_select_requires_project_level_config(priority_app):
    client = priority_app["client"]
    client.patch("/api/v1/system/source-priority", json={"video": _channel(True, ("g", "gm"))})

    resp = client.post("/api/v1/projects/demo/source-priority/select", json={"media_type": "video", "active_index": 0})
    assert resp.status_code == 409


def test_project_select_moves_project_active_index(priority_app):
    client = priority_app["client"]
    client.patch(
        "/api/v1/projects/demo/source-priority",
        json={"video": _channel(True, ("a", "m1"), ("b", "m2"))},
    )
    resp = client.post("/api/v1/projects/demo/source-priority/select", json={"media_type": "video", "active_index": 1})
    assert resp.status_code == 200
    assert resp.json()["video"]["active_index"] == 1


def test_project_unknown_is_404(priority_app):
    resp = priority_app["client"].get("/api/v1/projects/nope/source-priority")
    assert resp.status_code == 404
