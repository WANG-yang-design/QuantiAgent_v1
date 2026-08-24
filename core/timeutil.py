# -*- coding: utf-8 -*-
"""统一业务时区。

数据库现有 DateTime 列保存无 tzinfo 的业务本地时间，因此 ``now``
返回配置时区的 naive datetime；和外部 aware 时间比较时使用
``aware_now``。
"""
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def timezone() -> ZoneInfo:
    try:
        from core.config import get_settings
        name = str(get_settings().get("system.timezone", "Asia/Shanghai")
                   or "Asia/Shanghai")
    except Exception:
        name = "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def aware_now() -> datetime:
    return datetime.now(timezone())


def now() -> datetime:
    return aware_now().replace(tzinfo=None)


def today() -> date:
    return aware_now().date()
