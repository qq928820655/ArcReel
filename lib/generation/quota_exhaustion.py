"""判定一次生成失败是否为「供应商额度/余额不足」。

用途只有一个：多源优先级在判定额度耗尽时把当前行下移。这类判定一旦误报，就会在供应商其实可用
的情况下把后续生成切到别的源，所以取向是**宁可漏判、不可误判**：

- HTTP 402 Payment Required 是唯一无歧义的状态码信号。
- 其余靠错误体/异常消息里的额度关键词，且状态码不能是 5xx（上游故障时正文里的 "quota" 说的是
  「配额检查服务挂了」，不是「你用完了」）。
- 裸 429 不计入：那是普通限流，等一会就能继续用，不是额度耗尽。
- 内容审核、超时、鉴权失败、确定性 4xx 都不计入。

关键词只在供应商返回的错误体与异常消息里找，不扫请求载荷——提示词里出现「余额」不该被当成额度不足。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

#: 额度/余额不足的文案关键词，中英双语。判定前统一转小写。
QUOTA_EXHAUSTION_KEYWORDS: tuple[str, ...] = (
    # 中文
    "额度不足",
    "额度已用完",
    "额度已耗尽",
    "余额不足",
    "金额不足",
    "没有余额",
    "无余额",
    "欠费",
    "已欠费",
    # 英文（各家措辞不同，取覆盖面最广的几种）
    "insufficient_quota",
    "insufficient quota",
    "insufficient_balance",
    "insufficient balance",
    "insufficient funds",
    # OpenAI 额度耗尽的原话；Google 的 "quota exceeded" 是每分钟限流，刻意不收，避免误判
    "exceeded your current quota",
    "exceed your current quota",
    "out of credits",
    "credit balance is too low",
    "no balance",
    "payment required",
    "arrears",
)

#: 上游故障类状态码：正文里的关键词不作为额度信号。
_UPSTREAM_FAILURE_STATUS = frozenset({500, 502, 503, 504})

#: 限额不足的显式状态码。
_PAYMENT_REQUIRED = 402
_RATE_LIMITED = 429


def _http_status(exc: BaseException) -> int | None:
    """异常携带的 HTTP 状态码：httpx 系挂在 ``response`` 上，google 系挂在异常自身。"""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _response_text(exc: BaseException) -> str:
    """异常关联响应的正文；流式未读或没有响应时返回空串。"""
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        text = response.text
    except Exception:  # 未读取的流式响应会抛任意 httpx 异常，这里只想要正文
        return ""
    return text if isinstance(text, str) else ""


def _cause_chain(exc: BaseException) -> Iterator[BaseException]:
    """从外到内的显式因果链（``raise ... from``），带环路保护。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__


def _flatten_text(value: object, *, depth: int = 0) -> Iterator[str]:
    """把错误体（可能是嵌套 JSON）摊平成字符串片段，供关键词匹配。"""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _flatten_text(item, depth=depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _flatten_text(item, depth=depth + 1)


def _haystack(exc: BaseException) -> str:
    """一个异常可供匹配的全部文本：消息、响应体原文、响应体里的 JSON 字段。"""
    parts: list[str] = [str(exc)]
    body = _response_text(exc)
    if body:
        parts.append(body)
        try:
            parsed = json.loads(body)
        except ValueError:
            pass
        else:
            parts.extend(_flatten_text(parsed))
    # 上游常把额度信息塞进异常的自定义属性（如 provider_reason）
    reason = getattr(exc, "provider_reason", None)
    if isinstance(reason, str):
        parts.append(reason)
    return "\n".join(parts).lower()


def _has_quota_keyword(exc: BaseException) -> bool:
    haystack = _haystack(exc)
    return any(keyword in haystack for keyword in QUOTA_EXHAUSTION_KEYWORDS)


def classify_quota_exhaustion(exc: BaseException | str | None) -> bool:
    """这次失败是否属于「供应商额度/余额不足」。

    判定顺序：402 直接成立；5xx 直接排除；其余状态码（含裸 429、无状态码）只在错误文本命中额度
    关键词时成立。传入字符串时只按关键词判定——那是记账层只拿得到原文的出口。
    """
    if exc is None:
        return False
    if isinstance(exc, str):
        haystack = exc.lower()
        return any(keyword in haystack for keyword in QUOTA_EXHAUSTION_KEYWORDS)

    for candidate in _cause_chain(exc):
        status = _http_status(candidate)
        if status == _PAYMENT_REQUIRED:
            return True
        if status in _UPSTREAM_FAILURE_STATUS:
            # 上游自身故障：正文里的关键词不作数，继续看更内层的因果
            continue
        if _has_quota_keyword(candidate):
            return True
    return False


def is_rate_limited(exc: BaseException) -> bool:
    """异常链上是否有 429：供诊断区分「限流」与「额度耗尽」，两者对多源优先级的处理不同。"""
    return any(_http_status(candidate) == _RATE_LIMITED for candidate in _cause_chain(exc))
