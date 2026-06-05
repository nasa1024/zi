"""前缀化 ID 生成（设计 §02 通用列约定：id TEXT PRIMARY KEY, 前缀化、可读、可跨表引用）。

不依赖 ulid 库：用单调时间 + 计数器 + 进程随机种子构造 26 字符 base32 后缀，
保证同进程内单调递增、跨表唯一、人类可读（前缀标明表）。
"""
from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32（去掉 I L O U）
_lock = threading.Lock()
_last_ms = 0
_counter = 0


def _b32(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """生成形如 'fact_01J8...' 的前缀化 ID（同进程内单调、唯一）。"""
    global _last_ms, _counter
    with _lock:
        ms = int(time.time() * 1000)
        if ms <= _last_ms:
            _counter += 1
        else:
            _last_ms = ms
            _counter = 0
        ts = _b32(ms, 9)            # ~45 bit 时间
        ctr = _b32(_counter, 3)     # 同毫秒内序号
        rnd = _b32(int.from_bytes(os.urandom(4), "big"), 6)
    return f"{prefix}_{ts}{ctr}{rnd}"
