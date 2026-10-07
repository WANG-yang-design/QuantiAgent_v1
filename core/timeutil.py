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


# A股收盘(含集合竞价/尾盘)后当日K线才算完成
MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE = 15, 5


def market_closed(now_: datetime | None = None) -> bool:
    """当日是否已收盘(15:05后视为收盘, 周末/节假日也返回True)。"""
    n = now_ or now()
    if n.weekday() >= 5:
        return True
    return (n.hour, n.minute) >= (MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE)


def completed_daily_bars(bars: list) -> list:
    """剔除"尚未收盘的当日K线", 保证日频判断不被盘中短时数据带偏。

    支持 ORM 行与 dict(daily K)。盘中调用时, 若最后一根的 trade_date 是今天,
    直接丢弃; 收盘后(15:05)保留。
    """
    if not bars or market_closed():
        return bars

    def _d(b):
        try:
            return b["trade_date"] if isinstance(b, dict) else b.trade_date
        except (KeyError, AttributeError):
            return None

    t = today()
    out = list(bars)
    while out and _d(out[-1]) == t:
        out.pop()
    return out


def index_asof_date(bars: list) -> str:
    """返回K线序列的数据截止日(剔除未收盘当日后)。"""
    b = completed_daily_bars(bars)
    if not b:
        return ""
    last = b[-1]
    d = last.get("trade_date") if isinstance(last, dict) else getattr(last, "trade_date", None)
    return str(d)[:10] if d else ""
