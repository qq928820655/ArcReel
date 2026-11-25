"""额度不足判定：状态码与关键词的边界。"""

from __future__ import annotations

import httpx
import pytest

from lib.generation.quota_exhaustion import (
    classify_network_unreachable,
    classify_quota_exhaustion,
    is_rate_limited,
)


def _http_error(
    status: int, body: str = "", *, url: str = "https://api.example.com/v1/submit"
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


# ---------------------------------------------------------------------------
# 402
# ---------------------------------------------------------------------------


def test_402_is_quota_exhausted_without_keywords():
    assert classify_quota_exhaustion(_http_error(402, "")) is True


def test_402_in_cause_chain_is_detected():
    inner = _http_error(402, "")
    outer = RuntimeError("submit failed")
    outer.__cause__ = inner
    assert classify_quota_exhaustion(outer) is True


# ---------------------------------------------------------------------------
# 关键词
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '{"error": {"message": "余额不足，请充值"}}',
        '{"error": {"message": "额度不足"}}',
        '{"error": {"message": "金额不足"}}',
        '{"error": {"message": "账户没有余额"}}',
        '{"error": {"code": "insufficient_quota"}}',
        '{"error": {"message": "You exceeded your current quota, please check your plan"}}',
        '{"error": {"message": "Insufficient balance in your account"}}',
        '{"error": {"message": "account arrears"}}',
    ],
)
def test_keyword_hits_with_any_status(body):
    assert classify_quota_exhaustion(_http_error(400, body)) is True
    assert classify_quota_exhaustion(_http_error(403, body)) is True


def test_429_with_quota_keyword_counts():
    assert classify_quota_exhaustion(_http_error(429, '{"error": {"message": "insufficient_quota"}}')) is True


def test_bare_429_does_not_count():
    """普通限流不是额度耗尽：等一会还能用，切源是错的。"""
    assert classify_quota_exhaustion(_http_error(429, '{"error": {"message": "rate limit exceeded"}}')) is False
    assert classify_quota_exhaustion(_http_error(429, "")) is False


def test_google_style_quota_exceeded_is_not_treated_as_exhaustion():
    """Google 的 RESOURCE_EXHAUSTED / quota exceeded 是每分钟限流，不是余额用完。"""
    assert (
        classify_quota_exhaustion(_http_error(429, '{"error": {"message": "Quota exceeded for quota metric"}}'))
        is False
    )


# ---------------------------------------------------------------------------
# 排除项
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '{"error": {"message": "content policy violation"}}',
        '{"error": {"message": "invalid request: prompt too long"}}',
        '{"error": {"message": "authentication failed"}}',
        '{"error": {"message": "model not found"}}',
        "",
    ],
)
def test_other_4xx_is_not_exhaustion(body):
    assert classify_quota_exhaustion(_http_error(400, body)) is False
    assert classify_quota_exhaustion(_http_error(404, body)) is False


def test_5xx_keyword_is_ignored():
    """上游故障时正文里出现 quota 说的是配额服务挂了，不是用完了。"""
    assert classify_quota_exhaustion(_http_error(503, '{"error": {"message": "quota service unavailable"}}')) is False
    assert classify_quota_exhaustion(_http_error(500, "insufficient_quota check failed")) is False


def test_timeout_and_generic_errors_are_not_exhaustion():
    assert classify_quota_exhaustion(TimeoutError("timed out")) is False
    assert classify_quota_exhaustion(RuntimeError("boom")) is False


def test_content_policy_code_is_not_exhaustion():
    exc = _http_error(400, '{"error": {"code": "content_filter"}}')
    assert classify_quota_exhaustion(exc) is False


# ---------------------------------------------------------------------------
# 其他入口
# ---------------------------------------------------------------------------


def test_none_and_empty_string():
    assert classify_quota_exhaustion(None) is False
    assert classify_quota_exhaustion("") is False


def test_plain_string_uses_keywords():
    assert classify_quota_exhaustion("供应商返回：额度不足") is True
    assert classify_quota_exhaustion("connection reset") is False


def test_provider_reason_attribute_is_scanned():
    class _Rejected(httpx.HTTPStatusError):
        provider_reason = "insufficient_quota"

    request = httpx.Request("POST", "https://api.example.com")
    response = httpx.Response(400, request=request, text="")
    assert classify_quota_exhaustion(_Rejected("rejected", request=request, response=response)) is True


def test_is_rate_limited_checks_chain():
    assert is_rate_limited(_http_error(429, "")) is True
    outer = RuntimeError("wrapped")
    outer.__cause__ = _http_error(429, "")
    assert is_rate_limited(outer) is True
    assert is_rate_limited(_http_error(402, "")) is False


def test_cause_chain_cycle_does_not_hang():
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert classify_quota_exhaustion(first) is False


# ---------------------------------------------------------------------------
# 网络不可达
# ---------------------------------------------------------------------------


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused", request=httpx.Request("POST", "https://api.example.com"))


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused", request=httpx.Request("POST", "https://api.example.com")),
        httpx.ConnectTimeout("timed out", request=httpx.Request("POST", "https://api.example.com")),
        httpx.PoolTimeout("pool exhausted"),
        httpx.ProxyError("proxy handshake failed"),
    ],
)
def test_connection_setup_failures_are_unreachable(error):
    assert classify_network_unreachable(error) is True


@pytest.mark.parametrize(
    "error",
    [
        # 请求可能已被供应商接收：切源会导致重复计费或孤儿任务，不算不可达。
        httpx.ReadTimeout("read timed out", request=httpx.Request("POST", "https://api.example.com")),
        httpx.WriteError("write failed", request=httpx.Request("POST", "https://api.example.com")),
        httpx.RemoteProtocolError("server disconnected"),
        _http_error(500, "boom"),
        _http_error(402, ""),
        RuntimeError("something else"),
    ],
)
def test_non_connection_errors_are_not_unreachable(error):
    assert classify_network_unreachable(error) is False


def test_unreachable_detected_through_cause_chain():
    outer = RuntimeError("submit failed")
    outer.__cause__ = _connect_error()
    assert classify_network_unreachable(outer) is True


def test_unreachable_ignores_none():
    assert classify_network_unreachable(None) is False


def test_unreachable_has_no_cycle_problem():
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert classify_network_unreachable(first) is False
