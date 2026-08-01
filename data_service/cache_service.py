# -*- coding: utf-8 -*-
"""
缓存服务 (内存 LRU + 短期 TTL)
==============================
盘中高频数据(实时行情/盘口)走内存缓存, 减少数据源限流压力。
行情类缓存 TTL 极短(5-15s), 日K类较长。
"""
import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class TTLCache:
    """线程安全 TTL 缓存 (简单 LRU)。"""

    def __init__(self, maxsize: int = 512, default_ttl: float = 10.0):
        self.maxsize = maxsize
        self.default_ttl = default_ttl
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return default
            expire_at, value = item
            if time.time() > expire_at:
                del self._data[key]
                return default
            self._data.move_to_end(key)     # LRU 触达
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None):
        with self._lock:
            self._data[key] = (time.time() + (ttl if ttl is not None else self.default_ttl), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()


# 全局缓存实例
quote_cache = TTLCache(maxsize=2048, default_ttl=6)     # 实时行情 6s
order_book_cache = TTLCache(maxsize=2048, default_ttl=15)
daily_cache = TTLCache(maxsize=512, default_ttl=3600)   # 日K 1h
minute_cache = TTLCache(maxsize=512, default_ttl=120)   # 分钟K 2min
etf_spot_cache = TTLCache(maxsize=8, default_ttl=15)    # 全市场ETF列表 15s
