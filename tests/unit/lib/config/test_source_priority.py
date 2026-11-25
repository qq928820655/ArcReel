"""多源优先级：配置解析、状态推进、运行态读写。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lib.config.source_priority import (
    QUOTA_STREAK_THRESHOLD,
    RESET_TIMEZONE,
    SourcePriorityConfig,
    SourcePriorityEntry,
    SourcePriorityStateRow,
    SourcePriorityStore,
    advance,
    beijing_today,
    clamp_active_index,
    next_beijing_midnight,
    pick_entry,
    read_project_config,
    setting_key,
    state_scope,
    write_project_config,
)

# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_entry_payload_roundtrip():
    entry = SourcePriorityEntry(provider_id="ark", model_id="seedance-2-0")
    assert entry.pair_key == "ark/seedance-2-0"
    assert SourcePriorityEntry.from_payload(entry.to_payload()) == entry


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"provider_id": "ark"},
        {"provider_id": "", "model_id": "m"},
        {"provider_id": "ark", "model_id": "  "},
        {"provider_id": 1, "model_id": "m"},
        "ark/m",
    ],
)
def test_entry_from_payload_rejects_malformed(raw):
    assert SourcePriorityEntry.from_payload(raw) is None


def test_config_parses_and_keeps_order():
    config = SourcePriorityConfig.from_payload(
        {
            "enabled": True,
            "entries": [
                {"provider_id": "a", "model_id": "b"},
                {"provider_id": "a", "model_id": "a"},
                {"provider_id": "c", "model_id": "c"},
            ],
        }
    )
    assert config.enabled is True
    assert [entry.pair_key for entry in config.entries] == ["a/b", "a/a", "c/c"]
    assert config.is_active is True


def test_config_drops_malformed_entries_but_keeps_valid_ones():
    config = SourcePriorityConfig.from_payload(
        {"enabled": True, "entries": [{"provider_id": "a", "model_id": "b"}, "junk", {"provider_id": ""}]}
    )
    assert [entry.pair_key for entry in config.entries] == ["a/b"]


@pytest.mark.parametrize("raw", [None, {}, {"enabled": False, "entries": []}, "nonsense", []])
def test_config_inactive_when_not_configured(raw):
    config = SourcePriorityConfig.from_payload(raw)
    assert config.is_active is False


def test_config_enabled_but_empty_entries_is_inactive():
    """开关打开但一行都没有：不能当成生效，否则解析会拿到空列表。"""
    config = SourcePriorityConfig.from_payload({"enabled": True, "entries": []})
    assert config.is_active is False


@pytest.mark.parametrize("raw", ["", "   ", "{bad json", "null"])
def test_config_from_json_tolerates_garbage(raw):
    config = SourcePriorityConfig.from_json(raw)
    assert config.is_active is False


def test_setting_key_and_state_scope():
    assert setting_key("video") == "source_priority_video"
    assert state_scope("proj") == ("project", "proj")
    assert state_scope(None) == ("global", "")


# ---------------------------------------------------------------------------
# 项目快照读写
# ---------------------------------------------------------------------------


def test_read_project_config_absent_returns_inactive():
    assert read_project_config(None, "video").is_active is False
    assert read_project_config({}, "video").is_active is False
    assert read_project_config({"source_priority": "junk"}, "video").is_active is False


def test_write_then_read_project_config_roundtrip():
    project: dict = {}
    config = SourcePriorityConfig(
        enabled=True,
        entries=(SourcePriorityEntry("ark", "seedance"), SourcePriorityEntry("vidu", "v2")),
    )
    write_project_config(project, "video", config)
    assert read_project_config(project, "video") == config
    # 其他通道互不影响
    assert read_project_config(project, "image").is_active is False


def test_write_project_config_removes_channel_when_cleared():
    project: dict = {}
    write_project_config(project, "video", SourcePriorityConfig(enabled=True, entries=(SourcePriorityEntry("a", "b"),)))
    write_project_config(project, "video", SourcePriorityConfig())
    assert "source_priority" not in project


def test_write_project_config_keeps_other_channels_when_one_cleared():
    project: dict = {}
    write_project_config(project, "video", SourcePriorityConfig(enabled=True, entries=(SourcePriorityEntry("a", "b"),)))
    write_project_config(project, "image", SourcePriorityConfig(enabled=True, entries=(SourcePriorityEntry("c", "d"),)))
    write_project_config(project, "video", SourcePriorityConfig())
    assert read_project_config(project, "video").is_active is False
    assert read_project_config(project, "image").is_active is True


# ---------------------------------------------------------------------------
# 下标与当前条目
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "count", "expected"),
    [(0, 3, 0), (2, 3, 2), (5, 3, 2), (-1, 3, 0), (0, 0, 0), (9, 0, 0)],
)
def test_clamp_active_index(index, count, expected):
    assert clamp_active_index(index, count) == expected


def test_pick_entry_returns_clamped_index():
    config = SourcePriorityConfig(
        enabled=True,
        entries=(SourcePriorityEntry("a", "b"), SourcePriorityEntry("c", "d")),
    )
    picked = pick_entry(config, 7)
    assert picked is not None
    entry, index = picked
    assert entry.pair_key == "c/d"
    assert index == 1


def test_pick_entry_none_when_inactive():
    assert pick_entry(SourcePriorityConfig(), 0) is None


# ---------------------------------------------------------------------------
# 状态推进
# ---------------------------------------------------------------------------


def _advance(index: int, streak: int, *, count: int = 3, quota: bool = False, ok: bool = False):
    return advance(
        entry_count=count,
        active_index=index,
        quota_streak=streak,
        quota_exhausted=quota,
        succeeded=ok,
    )


def test_success_clears_streak_without_moving():
    assert _advance(1, 1, ok=True) == (1, 0)


def test_single_quota_failure_does_not_switch():
    assert _advance(0, 0, quota=True) == (0, 1)


def test_consecutive_quota_failures_switch_to_next_row():
    assert _advance(0, QUOTA_STREAK_THRESHOLD - 1, quota=True) == (1, 0)


def test_other_failure_does_not_move_or_count():
    assert _advance(0, 0) == (0, 0)
    assert _advance(1, 1) == (1, 1)


def test_quota_failure_on_last_row_stays_with_streak():
    assert _advance(2, QUOTA_STREAK_THRESHOLD - 1, quota=True) == (2, QUOTA_STREAK_THRESHOLD)


def test_success_between_quota_failures_prevents_switch():
    """两次额度不足中间夹一次成功：成功清零，不换行。"""
    index, streak = _advance(0, 0, quota=True)
    assert (index, streak) == (0, 1)
    index, streak = _advance(index, streak, ok=True)
    assert (index, streak) == (0, 0)
    index, streak = _advance(index, streak, quota=True)
    assert (index, streak) == (0, 1)


def test_advance_clamps_out_of_range_index():
    """列表被改短后，旧下标先夹进界内再推进。"""
    assert _advance(9, QUOTA_STREAK_THRESHOLD - 1, count=2, quota=True) == (1, QUOTA_STREAK_THRESHOLD)


# ---------------------------------------------------------------------------
# 时区
# ---------------------------------------------------------------------------


def test_beijing_today_uses_utc_plus_8():
    # 2026-11-24T15:59Z = 北京 23:59（当天）；16:00Z = 北京次日 00:00
    before = datetime(2026, 11, 24, 15, 59, tzinfo=UTC)
    after = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    assert beijing_today(before) == date(2026, 11, 24)
    assert beijing_today(after) == date(2026, 11, 25)


def test_next_beijing_midnight_is_utc_16_00():
    moment = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    upcoming = next_beijing_midnight(moment)
    assert upcoming == datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    assert upcoming.astimezone(RESET_TIMEZONE).hour == 0


def test_next_beijing_midnight_rolls_to_tomorrow_after_midnight_passed():
    moment = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)  # 北京 25 日 01:00
    upcoming = next_beijing_midnight(moment)
    assert upcoming == datetime(2026, 11, 25, 16, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 运行态读写
# ---------------------------------------------------------------------------


async def test_store_global_config_roundtrip(db_session: AsyncSession):
    store = SourcePriorityStore(db_session)
    config = SourcePriorityConfig(
        enabled=True,
        entries=(SourcePriorityEntry("ark", "seedance"), SourcePriorityEntry("vidu", "v2")),
    )
    await store.write_global_config("video", config)
    await db_session.commit()

    configs = await store.read_global_configs()
    assert configs["video"] == config
    assert configs["image"].is_active is False


async def test_store_state_defaults_to_first_row(db_session: AsyncSession):
    store = SourcePriorityStore(db_session)
    row = await store.read_state(scope="project", project_name="p", media_type="video")
    assert row == SourcePriorityStateRow()


async def test_store_apply_outcome_persists_and_switches(db_session: AsyncSession):
    store = SourcePriorityStore(db_session)
    kwargs = {"scope": "project", "project_name": "p", "media_type": "video", "entry_count": 3}

    assert await store.apply_outcome(**kwargs, quota_exhausted=True, succeeded=False) == (0, 1)
    assert await store.apply_outcome(**kwargs, quota_exhausted=True, succeeded=False) == (1, 0)
    await db_session.commit()

    row = await store.read_state(scope="project", project_name="p", media_type="video")
    assert row.active_index == 1
    assert row.quota_streak == 0


async def test_store_set_active_index_clears_streak(db_session: AsyncSession):
    store = SourcePriorityStore(db_session)
    await store.apply_outcome(
        scope="project", project_name="p", media_type="video", entry_count=3, quota_exhausted=True, succeeded=False
    )
    await store.set_active_index(scope="project", project_name="p", media_type="video", active_index=2)
    row = await store.read_state(scope="project", project_name="p", media_type="video")
    assert row.active_index == 2
    assert row.quota_streak == 0


async def test_store_reset_if_due_is_idempotent(db_session: AsyncSession):
    store = SourcePriorityStore(db_session)
    await store.apply_outcome(
        scope="project", project_name="p", media_type="video", entry_count=3, quota_exhausted=True, succeeded=False
    )
    await store.apply_outcome(
        scope="project", project_name="p", media_type="video", entry_count=3, quota_exhausted=True, succeeded=False
    )

    today = date(2026, 11, 25)
    assert await store.reset_if_due(today=today) == 1
    row = await store.read_state(scope="project", project_name="p", media_type="video")
    assert row.active_index == 0
    assert row.quota_streak == 0
    assert row.last_reset_on == today

    # 同一天再跑一次：不动
    assert await store.reset_if_due(today=today) == 0


async def test_store_reset_skips_rows_already_reset_today(db_session: AsyncSession):
    store = SourcePriorityStore(db_session)
    today = date(2026, 11, 25)
    await store.apply_outcome(
        scope="global", project_name="", media_type="video", entry_count=3, quota_exhausted=True, succeeded=False
    )
    await store.reset_if_due(today=today)
    # 手动切行不应把 last_reset_on 丢掉，否则同一天的重置会被重复触发
    await store.set_active_index(scope="global", project_name="", media_type="video", active_index=0)
    row = await store.read_state(scope="global", project_name="", media_type="video")
    assert row.last_reset_on == today
    assert await store.reset_if_due(today=today) == 0
