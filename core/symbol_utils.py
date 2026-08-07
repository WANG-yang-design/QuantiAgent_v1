# -*- coding: utf-8 -*-
"""
标的交易规则工具 (T+0 / 涨跌幅)
===============================
A股 ETF 交易规则差异:
- T+0(当日买入可当日卖出): 跨境QDII(513xxx/1599xx精确表)、债券(511xxx)、黄金(518xxx)、商品(519xxx)
- T+1: 股票型ETF(510/512/515/1590/1591/1592/1595/1596/1597/588等)
- 涨跌幅: 主板ETF ±10%; 科创板/创业板相关ETF ±20%(588xxx, 以及跟踪科创/创业指数的)
判断规则可从 config/trading_rules.yaml t0_rules 覆盖。
注意: 1599xx 前缀混合了 T+0 跨境ETF 与 T+1 创业板ETF(159915等), 必须用精确代码表判定。
"""
from core.config import get_settings

# 深市 T+0 跨境/商品 ETF 精确代码表(默认值, 可被 trading_rules.yaml extra_symbols 覆盖)
_DEFAULT_T0_SYMBOLS = {
    "159920",   # 恒生ETF
    "159938",   # 广发纳斯达克100ETF
    "159941",   # 纳指ETF
    "159985",   # 豆粕ETF(商品)
    "159605",   # 中概互联网ETF
    "159607",   # 中概互联ETF
    "159632",   # 纳斯达克ETF
    "159655",   # 恒生科技ETF
    "159659",   # 纳指ETF(汇添富)
    "159866",   # 日经ETF
}


def is_t0_etf(symbol: str, asset_type: str = "etf") -> bool:
    """是否 T+0 可当日回转的 ETF。"""
    if asset_type != "etf":
        return False
    rules = get_settings().get("trading_rules.t0_rules", {}) or {}
    prefixes = [str(p) for p in rules.get("prefixes", ["511", "513", "518", "519"])]
    exact = [str(s) for s in rules.get("extra_symbols", [])] or list(_DEFAULT_T0_SYMBOLS)
    if symbol in exact:
        return True
    return any(symbol.startswith(p) for p in prefixes)


def price_limit_pct(symbol: str, asset_type: str = "etf") -> float:
    """涨跌幅限制(小数): 主板10%, 科创/创业/北交所相关 20%。
    修复: 原实现非 ETF 一律返回 0.10, 创业板/科创板股票(±20%)的合法
    行情被数据质量层误标 SUSPICIOUS; 且存在重复死代码。"""
    if asset_type == "stock":
        # 创业板 300/301, 科创板 688
        if symbol.startswith(("300", "301", "688")):
            return 0.20
        # 北交所 8/4/9 开头 ±30%, 以 43/83/87/92 开头为主
        if symbol.startswith(("43", "83", "87", "92")):
            return 0.30
        return 0.10
    # ETF: 科创板/创业板指数 ETF
    if symbol.startswith("588"):
        return 0.20
    # 深市创业板系(T+1): 159915/159949/159952/159977/159908 等
    gemb_codes = {"159915", "159949", "159952", "159977", "159908",
                  "159808", "159971", "159967", "159845"}
    if symbol in gemb_codes:
        return 0.20
    return 0.10
