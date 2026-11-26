"""视频汇总源的纯逻辑：虚拟身份编解码、容量感知分配、成员可用态推进。

见 ``docs/adr/0123-video-source-pool-as-virtual-provider.md``。这里只放**不碰 DB、不碰 worker 状态**
的纯函数：身份格式、分配策略、streak 推进。成员装载、状态读写与 worker 集成各自在上层。

虚拟身份：``pool-<id>`` 作 provider，model 为用户自定的虚拟模型名（如 ``aggregate``）。对外就是一个
普通 ``provider/model``，因此能直接出现在默认模型、任务类型桶与多源优先级列表里。

可用态按 ``provider`` 记录而非按池成员：额度与网络是供应商账户侧的事实，a-dola 用完了，
任何引用它的池都该跳过它。任务终态只需知道实际执行的 provider_id，无需回查「这条任务来自哪个池」。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 虚拟 provider id 前缀。与内置注册表键、``custom-`` 前缀都不同，避免与真实供应商混淆。
SOURCE_POOL_PREFIX = "pool-"

#: 汇总源的虚拟 model_id。对外身份是 ``pool-<id>/aggregate``，展示名由池的 ``model_name`` 决定。
POOL_MODEL_ID = "aggregate"

#: 池成员可用态与多源优先级共用同一套阈值语义（见 ADR 0121）。
QUOTA_STREAK_THRESHOLD = 2
NETWORK_STREAK_THRESHOLD = 5


def make_pool_provider_id(pool_id: int) -> str:
    """``3`` → ``"pool-3"``。"""
    return f"{SOURCE_POOL_PREFIX}{pool_id}"


def is_source_pool_provider(provider_id: object) -> bool:
    """判断 provider_id 是否指向一个汇总源（``pool-<正整数>``）。"""
    if not isinstance(provider_id, str) or not provider_id.startswith(SOURCE_POOL_PREFIX):
        return False
    suffix = provider_id.removeprefix(SOURCE_POOL_PREFIX)
    return suffix.isdigit() and int(suffix) > 0


def parse_pool_provider_id(provider_id: str) -> int:
    """``"pool-3"`` → ``3``。格式不合法时抛 ``ValueError``。"""
    if not is_source_pool_provider(provider_id):
        raise ValueError(f"not a source pool provider id: {provider_id!r}")
    return int(provider_id.removeprefix(SOURCE_POOL_PREFIX))


@dataclass(frozen=True)
class PoolMember:
    """一个池成员：真实供应商模型 + 列表位置（平局裁决顺序）。"""

    position: int
    provider_id: str
    model_id: str

    @property
    def pair_key(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


@dataclass(frozen=True)
class MemberAvailability:
    """成员当天的可用态。``unavailable`` 由阈值判定（见 :func:`is_member_unavailable`）。"""

    quota_streak: int = 0
    network_streak: int = 0


def advance_member_state(
    *,
    quota_streak: int,
    network_streak: int,
    quota_exhausted: bool,
    network_unreachable: bool,
    succeeded: bool,
    network_threshold: int = NETWORK_STREAK_THRESHOLD,
) -> tuple[int, int]:
    """按一次任务结果推进成员的两个 streak，返回 ``(quota_streak, network_streak)``。

    与 ``source_priority.advance`` 同一套规则，但**不换行**——池里没有「下一行」，只有「这个成员
    今天还能不能用」：

    - 成功：两个计数清零。
    - 额度不足 / 网络不可达：各自 +1，分开计数。
    - 任一达到阈值即把两个计数一起清零（当天视为不可用，明日重置时恢复）。
    - 其他失败：都不动。
    """
    if succeeded:
        return 0, 0
    next_quota = quota_streak + 1 if quota_exhausted else quota_streak
    next_network = network_streak + 1 if network_unreachable else network_streak
    if next_quota >= QUOTA_STREAK_THRESHOLD or next_network >= network_threshold:
        # 达到阈值：当天不再用这个成员，计数清零（明日重置时连同可用态一起恢复）。
        return 0, 0
    return next_quota, next_network


def is_member_unavailable(
    *, quota_streak: int, network_streak: int, network_threshold: int = NETWORK_STREAK_THRESHOLD
) -> bool:
    """成员是否当天不可用：任一 streak 达到阈值。"""
    return quota_streak >= QUOTA_STREAK_THRESHOLD or network_streak >= network_threshold


def pick_member(
    members: tuple[PoolMember, ...],
    *,
    availability: dict[str, MemberAvailability],
    occupied: dict[str, int],
    capacity: dict[str, int],
    network_threshold: int = NETWORK_STREAK_THRESHOLD,
) -> PoolMember | None:
    """在成员里选一个可分配者：剔除当天不可用与占用已满的，取占用最少者；平局取 position 小者。

    ``occupied`` 以 ``provider_id`` 为键记录当前在跑数（同一 provider 可被多个池共享，占用是全局的）；
    ``capacity`` 同样按 ``provider_id`` 给出并发上限。纯内存计算，不做任何 I/O。

    返回 ``None`` 表示此刻没有可分配的成员（全满或全不可用），调用方据此让任务保持 queued。
    """
    best: PoolMember | None = None
    best_load = -1
    for member in sorted(members, key=lambda m: m.position):
        state = availability.get(member.provider_id)
        if state is not None and is_member_unavailable(
            quota_streak=state.quota_streak, network_streak=state.network_streak, network_threshold=network_threshold
        ):
            continue
        cap = capacity.get(member.provider_id, 0)
        if cap <= 0:
            # 该成员指向的供应商不支持视频或已下线：跳过，不参与分配。
            continue
        load = occupied.get(member.provider_id, 0)
        if load >= cap:
            continue
        if best is None or load < best_load:
            best = member
            best_load = load
    return best


def has_any_available_member(
    members: tuple[PoolMember, ...],
    *,
    availability: dict[str, MemberAvailability],
    capacity: dict[str, int],
    network_threshold: int = NETWORK_STREAK_THRESHOLD,
) -> bool:
    """池里是否还有「当天可用且供应商支持视频」的成员——不看占用，只看是否全被额度/网络/下线排除。

    worker 用它区分两种「现在分配不出来」：还有可用成员只是都占满了（应等容量、保持 queued），
    还是全部成员当天都不可用（继续等没有意义）。纯内存。
    """
    for member in members:
        state = availability.get(member.provider_id)
        if state is not None and is_member_unavailable(
            quota_streak=state.quota_streak, network_streak=state.network_streak, network_threshold=network_threshold
        ):
            continue
        if capacity.get(member.provider_id, 0) > 0:
            return True
    return False
