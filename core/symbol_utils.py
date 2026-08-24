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


def infer_asset_type(symbol: str) -> str:
    """按A股/场内基金代码推断资产类型；数据库中的显式类型应优先于此兜底。"""
    symbol = str(symbol or "").strip()
    # 沪深场内基金常见号段。不能简单把 159/510 之外全部视为股票，
    # 588 科创ETF、511债券ETF、513跨境ETF同样属于基金。
    if symbol.startswith(("15", "50", "51", "52", "56", "58")):
        return "etf"
    return "stock"


def is_t0_etf(symbol: str, asset_type: str = "etf") -> bool:
    """是否 T+0 可当日回转的 ETF。"""
    if asset_type != "etf":
        return False
    rules = get_settings().get("trading_rules.t0_rules", {}) or {}
    prefixes = [str(p) for p in rules.get("prefixes", ["511", "513", "518", "519"])]
    extra = rules.get("extra_symbols")
    exact = list(_DEFAULT_T0_SYMBOLS) if extra is None else [str(s) for s in extra]
    if symbol in exact:
        return True
    return any(symbol.startswith(p) for p in prefixes)


def price_limit_pct(symbol: str, asset_type: str = "etf") -> float:
    """涨跌幅限制(小数): 主板10%, 科创/创业/北交所相关 20%。
    修复: 原实现非 ETF 一律返回 0.10, 创业板/科创板股票(±20%)的合法
    行情被数据质量层误标 SUSPICIOUS; 且存在重复死代码。"""
    limits = get_settings().get("trading_rules.price_limit", {}) or {}
    if asset_type == "stock":
        # 创业板 300/301, 科创板 688
        if symbol.startswith(("300", "301", "688")):
            return float(limits.get("stock_growth_limit", 0.20))
        # 北交所 8/4/9 开头 ±30%, 以 43/83/87/92 开头为主
        if symbol.startswith(("43", "83", "87", "92")):
            return float(limits.get("stock_bse_limit", 0.30))
        return float(limits.get("stock_limit", 0.10))
    # ETF: 科创板/创业板指数 ETF
    if symbol.startswith("588"):
        return float(limits.get("etf_growth_limit", 0.20))
    # 深市创业板系(T+1): 159915/159949/159952/159977/159908 等
    gemb_codes = {"159915", "159949", "159952", "159977", "159908",
                  "159808", "159971", "159967", "159845"}
    if symbol in gemb_codes:
        return float(limits.get("etf_growth_limit", 0.20))
    return float(limits.get("etf_limit", 0.10))
