"""视频汇总源纯逻辑：身份编解码、分配策略、成员可用态推进。"""

from __future__ import annotations

import pytest

from lib.config.source_pool import (
    NETWORK_STREAK_THRESHOLD,
    QUOTA_STREAK_THRESHOLD,
    MemberAvailability,
    PoolMember,
    advance_member_state,
    has_any_available_member,
    is_member_unavailable,
    is_source_pool_provider,
    make_pool_provider_id,
    parse_pool_provider_id,
    pick_member,
)


class TestPoolIdentity:
    def test_make_and_parse_roundtrip(self):
        assert make_pool_provider_id(3) == "pool-3"
        assert parse_pool_provider_id("pool-3") == 3

    @pytest.mark.parametrize("value", ["pool-3", "pool-1", "pool-42"])
    def test_recognizes_pool_ids(self, value: str):
        assert is_source_pool_provider(value) is True

    @pytest.mark.parametrize(
        "value",
        ["pool-", "pool-0", "pool--1", "pool-abc", "custom-3", "gemini-aistudio", "", None, 3],
    )
    def test_rejects_non_pool_ids(self, value):
        assert is_source_pool_provider(value) is False

    def test_parse_rejects_malformed(self):
        with pytest.raises(ValueError, match="not a source pool"):
            parse_pool_provider_id("custom-3")


class TestAdvanceMemberState:
    def test_success_clears_both(self):
        assert advance_member_state(
            quota_streak=1, network_streak=3, quota_exhausted=False, network_unreachable=False, succeeded=True
        ) == (0, 0)

    def test_quota_increments_until_threshold_then_clears(self):
        q, n = 0, 0
        for _ in range(QUOTA_STREAK_THRESHOLD - 1):
            q, n = advance_member_state(
                quota_streak=q, network_streak=n, quota_exhausted=True, network_unreachable=False, succeeded=False
            )
        assert q == QUOTA_STREAK_THRESHOLD - 1
        # 再失败一次达到阈值：两个计数一起清零（当天不可用由阈值判定）
        q, n = advance_member_state(
            quota_streak=q, network_streak=n, quota_exhausted=True, network_unreachable=False, succeeded=False
        )
        assert (q, n) == (0, 0)

    def test_network_uses_its_own_threshold(self):
        q, n = 0, 0
        for _ in range(NETWORK_STREAK_THRESHOLD - 1):
            q, n = advance_member_state(
                quota_streak=q, network_streak=n, quota_exhausted=False, network_unreachable=True, succeeded=False
            )
        assert n == NETWORK_STREAK_THRESHOLD - 1
        q, n = advance_member_state(
            quota_streak=q, network_streak=n, quota_exhausted=False, network_unreachable=True, succeeded=False
        )
        assert (q, n) == (0, 0)

    def test_unrelated_failure_keeps_streaks(self):
        assert advance_member_state(
            quota_streak=1, network_streak=2, quota_exhausted=False, network_unreachable=False, succeeded=False
        ) == (1, 2)

    def test_is_member_unavailable_by_either_threshold(self):
        assert is_member_unavailable(quota_streak=QUOTA_STREAK_THRESHOLD, network_streak=0) is True
        assert is_member_unavailable(quota_streak=0, network_streak=NETWORK_STREAK_THRESHOLD) is True
        assert is_member_unavailable(quota_streak=1, network_streak=4) is False


def _members(*specs: tuple[int, str, str]) -> tuple[PoolMember, ...]:
    return tuple(PoolMember(position=p, provider_id=prov, model_id=m) for p, prov, m in specs)


class TestPickMember:
    def test_picks_least_loaded(self):
        members = _members((0, "a", "m"), (1, "b", "m"), (2, "c", "m"))
        chosen = pick_member(
            members,
            availability={},
            occupied={"a": 2, "b": 0, "c": 1},
            capacity={"a": 3, "b": 3, "c": 3},
        )
        assert chosen is not None
        assert chosen.provider_id == "b"

    def test_tie_breaks_by_position(self):
        members = _members((0, "a", "m"), (1, "b", "m"))
        chosen = pick_member(members, availability={}, occupied={"a": 0, "b": 0}, capacity={"a": 3, "b": 3})
        assert chosen is not None
        assert chosen.provider_id == "a"

    def test_skips_full_member(self):
        members = _members((0, "a", "m"), (1, "b", "m"))
        chosen = pick_member(members, availability={}, occupied={"a": 3, "b": 1}, capacity={"a": 3, "b": 3})
        assert chosen is not None
        assert chosen.provider_id == "b"

    def test_skips_unavailable_member(self):
        members = _members((0, "a", "m"), (1, "b", "m"))
        chosen = pick_member(
            members,
            availability={"a": MemberAvailability(quota_streak=QUOTA_STREAK_THRESHOLD)},
            occupied={"a": 0, "b": 1},
            capacity={"a": 3, "b": 3},
        )
        assert chosen is not None
        assert chosen.provider_id == "b"

    def test_skips_unsupported_provider(self):
        members = _members((0, "a", "m"), (1, "b", "m"))
        chosen = pick_member(members, availability={}, occupied={"a": 0, "b": 0}, capacity={"a": 0, "b": 3})
        assert chosen is not None
        assert chosen.provider_id == "b"

    def test_returns_none_when_all_full(self):
        members = _members((0, "a", "m"), (1, "b", "m"))
        chosen = pick_member(members, availability={}, occupied={"a": 3, "b": 3}, capacity={"a": 3, "b": 3})
        assert chosen is None

    def test_returns_none_when_all_unavailable(self):
        members = _members((0, "a", "m"))
        chosen = pick_member(
            members,
            availability={"a": MemberAvailability(network_streak=NETWORK_STREAK_THRESHOLD)},
            occupied={"a": 0},
            capacity={"a": 3},
        )
        assert chosen is None

    def test_returns_none_when_empty(self):
        assert pick_member((), availability={}, occupied={}, capacity={}) is None


class TestHasAnyAvailableMember:
    def test_true_when_a_member_is_available_even_if_occupied(self):
        members = _members((0, "a", "m"))
        # 占用满不算「不可用」：has_any 只看当天可用性与供应商支持，不看占用
        assert has_any_available_member(members, availability={}, capacity={"a": 3}) is True

    def test_false_when_all_unavailable(self):
        members = _members((0, "a", "m"), (1, "b", "m"))
        assert (
            has_any_available_member(
                members,
                availability={
                    "a": MemberAvailability(quota_streak=QUOTA_STREAK_THRESHOLD),
                    "b": MemberAvailability(network_streak=NETWORK_STREAK_THRESHOLD),
                },
                capacity={"a": 3, "b": 3},
            )
            is False
        )

    def test_false_when_all_unsupported(self):
        members = _members((0, "a", "m"))
        assert has_any_available_member(members, availability={}, capacity={"a": 0}) is False
