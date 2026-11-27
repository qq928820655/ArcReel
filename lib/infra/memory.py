"""进程级内存卫生：把已释放但未归还操作系统的堆内存及时交还。

CPython 的对象一旦没有活引用就会被回收，但 pymalloc 的 arena 与 glibc 的空闲
chunk 默认留在进程 RSS 里不归还 OS。长时间连续执行重任务（如成片渲染）的常驻
进程因此会呈现"只用不还"的高水位。``release_memory`` 先补一次分代 GC（回收循环
引用垃圾，ORM 对象/异常 traceback 常见），再调 glibc ``malloc_trim(0)`` 把空闲
堆归还 OS。

无副作用、可安全重复调用：glibc 不可用（非 glibc 平台）时静默跳过 trim，仅做 GC。
"""

from __future__ import annotations

import ctypes
import gc
import logging
import sys

logger = logging.getLogger(__name__)

# 解析一次，进程内复用；非 glibc（如 musl / macOS）时为 None。
_malloc_trim = None
_resolved = False


def _resolve_malloc_trim():
    global _malloc_trim, _resolved
    if _resolved:
        return _malloc_trim
    _resolved = True
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL("libc.so.6")
        _malloc_trim = libc.malloc_trim
    except (OSError, AttributeError):
        _malloc_trim = None
    return _malloc_trim


def release_memory(*, trim: bool = True) -> bool:
    """回收循环引用垃圾并归还空闲堆。返回是否执行了 ``malloc_trim``。

    ``trim=False`` 时只做 GC（便于测试或非 glibc 平台的显式降级）。
    """
    gc.collect()
    if not trim:
        return False
    fn = _resolve_malloc_trim()
    if fn is None:
        return False
    try:
        # 参数 0 表示从堆顶尽量释放所有空闲块。
        fn(0)
        return True
    except Exception:
        logger.debug("malloc_trim 调用失败，跳过", exc_info=True)
        return False


__all__ = ["release_memory"]
