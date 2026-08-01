# -*- coding: utf-8 -*-
"""
标的交易规则工具 (T+0 / 涨跌幅)
================================
A股 ETF 交易规则差异:
- T+0(当日买入可当日卖出): 跨境QDII(513xxx/1599xx)、债券(511xxx)、黄金(518xxx)、商品(519xxx)
- T+1: 股票型ETF(510/512/515/1590/1591/1592/1595/1596/1597/588等)
- 涨跌幅: 主板ETF ±10%; 科创板/创业板相关ETF ±20%(588xxx, 以及跟踪科创/创业指数的)
判断规则可从 config/trading_rules.yaml t0_rules 覆盖。
"""
from core.config import get_settings


def is_t0_etf(symbol: str, asset_type: str = "etf") -> bool:
    """是否 T+0 可当日回转的 ETF。"""
    if asset_type != "etf":
        return False
    rules = get_settings().get("trading_rules.t0_rules", {}) or {}
    prefixes = [str(p) for p in rules.get("prefixes", ["511", "513", "518", "519", "1599"])]
    exact = [str(s) for s in rules.get("extra_symbols", [])]
    if symbol in exact:
        return True
    return any(symbol.startswith(p) for p in prefixes)


def price_limit_pct(symbol: str, asset_type: str = "etf") -> float:
    """涨跌幅限制(小数): 主板ETF 10%, 科创/创业相关 ETF 20%。"""
    if asset_type != "etf":
        return 0.10
    # 科创板/创业板指数 ETF: 588xxx(沪科创), 159xxx 中的创业板系(159915/159949/159952等)
    if symbol.startswith("588"):
        return 0.20
    # 深市创业板系: 以 1599/1599 开头的主要是跨境(T+0), 创业板宽基为 159915/159949/159977/159952/159908 等
    gemb_codes = {"159915", "159949", "159952", "159977", "159908", "159808", "159971"}
    if symbol in gemb_codes:
        return 0.20
    return 0.10
