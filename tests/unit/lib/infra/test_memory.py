"""lib.infra.memory 的释放逻辑：GC 总执行，trim 按平台能力尽力而为。"""

from __future__ import annotations

import gc

import pytest

from lib.infra import memory


def test_release_memory_collects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """release_memory 必须触发一次分代 GC（循环引用垃圾的回收入口）。"""
    called = {"n": 0}
    real_collect = gc.collect

    def _spy() -> int:
        called["n"] += 1
        return real_collect()

    monkeypatch.setattr(memory.gc, "collect", _spy)
    memory.release_memory(trim=False)
    assert called["n"] == 1


def test_release_memory_trim_false_skips_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "_resolve_malloc_trim", lambda: None)
    assert memory.release_memory(trim=False) is False


def test_release_memory_calls_trim_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _fake_trim(arg: int) -> int:
        calls.append(arg)
        return 1

    monkeypatch.setattr(memory, "_resolve_malloc_trim", lambda: _fake_trim)
    assert memory.release_memory() is True
    assert calls == [0]


def test_release_memory_survives_trim_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_arg: int) -> int:
        raise RuntimeError("no trim")

    monkeypatch.setattr(memory, "_resolve_malloc_trim", lambda: _boom)
    assert memory.release_memory() is False
