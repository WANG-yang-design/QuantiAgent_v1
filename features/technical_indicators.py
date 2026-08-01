# -*- coding: utf-8 -*-
"""
技术指标计算 (纯 pandas/numpy, 不调用 LLM)
==========================================
MA/EMA/MACD/RSI/KDJ/BOLL/ATR/VWAP/量比/换手率/波动率/最大回撤/动量/支撑压力/突破
输入: 日K列表 [{trade_date, open, high, low, close, volume, amount}]
"""
import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def to_frame(bars: List[Dict[str, Any]]) -> pd.DataFrame:
    """标准化为 DataFrame, 按时间升序。"""
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    time_col = "trade_date" if "trade_date" in df.columns else "bar_time"
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.sort_values(time_col).reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def compute_technical_features(bars: List[Dict[str, Any]],
                               periods: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    计算全部技术特征, 返回 dict(最新值快照 + 序列)。
    供技术分析师 Agent 与策略层使用。
    """
    p = periods or {}
    n_ma = p.get("ma", 20)
    df = to_frame(bars)
    if df.empty:
        return {}

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    amount = df["amount"].astype(float)

    # ---------------- 均线 ----------------
    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = close.rolling(w).mean()
    ma20, ma60 = df["ma20"].iloc[-1], df["ma60"].iloc[-1]
    ma5, ma10 = df["ma5"].iloc[-1], df["ma10"].iloc[-1]
    # 均线多头排列: ma5>ma10>ma20>ma60
    bull_align = bool(ma5 > ma10 > ma20 > ma60) if not math.isnan(ma60) else False
    bear_align = bool(ma5 < ma10 < ma20 < ma60) if not math.isnan(ma60) else False

    # ---------------- MACD ----------------
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2
    if len(df) >= 3:
        macd_gold_cross = bool(dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1])
        macd_dead_cross = bool(dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1])
    else:
        macd_gold_cross = macd_dead_cross = False

    # ---------------- RSI (14) ----------------
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)).fillna(50).iloc[-1]
    rsi_overbought = bool(rsi > 70)
    rsi_oversold = bool(rsi < 30)

    # ---------------- KDJ (9,3,3) ----------------
    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d

    # ---------------- BOLL (20,2) ----------------
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    up = mid + 2 * std
    dn = mid - 2 * std
    boll_break_up = bool(close.iloc[-1] > up.iloc[-1])

    # ---------------- ATR (14) ----------------
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    atr_pct = float(atr / close.iloc[-1]) if close.iloc[-1] else 0.0

    # ---------------- 波动率(20日年化) ----------------
    ret = close.pct_change().dropna()
    vol_20 = float(ret.tail(20).std() * math.sqrt(252))
    vol_5 = float(ret.tail(5).std() * math.sqrt(252))

    # ---------------- 最大回撤(60日) ----------------
    peak = close.tail(60).cummax()
    drawdown = (close.tail(60) / peak - 1)
    max_drawdown_60d = float(drawdown.min())

    # ---------------- 动量 ----------------
    mom_5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0
    mom_20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else 0.0
    mom_60 = float(close.iloc[-1] / close.iloc[-61] - 1) if len(close) > 61 else 0.0

    # ---------------- 成交量 ----------------
    vol_ma5 = float(volume.rolling(5).mean().iloc[-1]) if len(volume) >= 5 else 0.0
    vol_ratio = float(volume.iloc[-1] / vol_ma5) if vol_ma5 else 0.0
    amount_ma5 = float(amount.rolling(5).mean().iloc[-1]) if len(amount) >= 5 else 0.0
    amount_ma20 = float(amount.rolling(20).mean().iloc[-1]) if len(amount) >= 20 else 0.0

    # ---------------- VWAP (当日) ----------------
    day = df.iloc[-1]
    vwap = float(amount.iloc[-1] / volume.iloc[-1]) if volume.iloc[-1] else float(close.iloc[-1])
    price_above_vwap = bool(close.iloc[-1] >= vwap)

    # ---------------- 支撑/压力 (20日高低点) ----------------
    recent_high = float(high.tail(min(20, len(high))).max())
    recent_low = float(low.tail(min(20, len(low))).min())
    support = recent_low
    resistance = recent_high
    near_resistance = bool(close.iloc[-1] >= resistance * 0.98)
    near_support = bool(close.iloc[-1] <= support * 1.02)

    # ---------------- 突破信号 ----------------
    if len(df) >= 3:
        breakout_20d = bool(close.iloc[-1] > recent_high * 0.999 and close.iloc[-2] <= recent_high)
        breakdown_20d = bool(close.iloc[-1] < recent_low * 1.001 and close.iloc[-2] >= recent_low)
    else:
        breakout_20d = breakdown_20d = False

    # ---------------- 趋势强度 ----------------
    up_days = int((close.diff() > 0).tail(min(20, len(close))).sum())
    trend_strength = float(up_days / min(20, len(close))) if len(close) else 0.0

    # ---------------- 涨跌停状态(最新日) ----------------
    chg = float(close.iloc[-1] / df["close"].shift(1).iloc[-1] - 1) if len(df) > 1 else 0.0

    return {
        # 行情快照
        "close": float(close.iloc[-1]),
        "open": float(day["open"]),
        "high": float(day["high"]),
        "low": float(day["low"]),
        "volume": float(volume.iloc[-1]),
        "amount": float(amount.iloc[-1]),
        "change_pct": chg * 100,
        # 均线
        "ma5": _f(ma5), "ma10": _f(ma10), "ma20": _f(ma20), "ma60": _f(ma60),
        "bull_align": bull_align, "bear_align": bear_align,
        "price_above_ma20": bool(close.iloc[-1] > ma20) if not math.isnan(ma20) else False,
        # MACD
        "macd_dif": _f(dif.iloc[-1]), "macd_dea": _f(dea.iloc[-1]),
        "macd": _f(macd.iloc[-1]),
        "macd_gold_cross": macd_gold_cross, "macd_dead_cross": macd_dead_cross,
        # 振荡指标
        "rsi": _f(rsi), "rsi_overbought": rsi_overbought, "rsi_oversold": rsi_oversold,
        "kdj_k": _f(k.iloc[-1]), "kdj_d": _f(d.iloc[-1]), "kdj_j": _f(j.iloc[-1]),
        # 通道
        "boll_mid": _f(mid.iloc[-1]), "boll_up": _f(up.iloc[-1]), "boll_dn": _f(dn.iloc[-1]),
        "boll_break_up": boll_break_up,
        # 波动/回撤
        "atr": _f(atr), "atr_pct": atr_pct,
        "volatility_20d": vol_20, "volatility_5d": vol_5,
        "max_drawdown_60d": max_drawdown_60d,
        # 动量
        "momentum_5d": mom_5, "momentum_20d": mom_20, "momentum_60d": mom_60,
        # 量能
        "volume_ratio": vol_ratio, "amount_ma5": amount_ma5, "amount_ma20": amount_ma20,
        # 执行参考
        "vwap": vwap, "price_above_vwap": price_above_vwap,
        "support_20d": support, "resistance_20d": resistance,
        "near_resistance": near_resistance, "near_support": near_support,
        "breakout_20d": breakout_20d, "breakdown_20d": breakdown_20d,
        "trend_strength": trend_strength,
    }


def _f(v: Any) -> float:
    try:
        f = float(v)
        return f if f == f else 0.0
    except Exception:
        return 0.0


def compute_etf_features(bars: List[Dict[str, Any]], quote: Optional[Dict[str, Any]] = None,
                         etf_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """ETF 专项特征: 流动性/规模/折溢价/IOPV偏离/同类排名。"""
    tech = compute_technical_features(bars)
    out: Dict[str, Any] = {"liquidity_score": 0.0, "premium_rate": 0.0, "iopv_deviation": 0.0}
    if quote:
        amount = float(quote.get("amount", 0))
        latest = float(quote.get("latest_price", 0))
        iopv = float(quote.get("iopv", 0))
        premium = float(quote.get("premium_rate", 0))
        out["latest_price"] = latest
        out["amount"] = amount
        out["premium_rate"] = premium
        # 流动性评分: 成交额越大分越高 (5000万→60分, 1亿→80分)
        out["liquidity_score"] = min(100.0, amount / 1e8 * 80) if amount else 0.0
        # IOPV 偏离
        if latest > 0 and iopv > 0:
            out["iopv_deviation"] = (latest - iopv) / iopv
    if etf_info:
        out["tracking_index"] = etf_info.get("tracking_index", "")
        out["scale"] = float(etf_info.get("scale", 0))
        out["is_qdii"] = bool(etf_info.get("is_qdii", False))
        out["fund_company"] = etf_info.get("fund_company", "")
    out.update({k: v for k, v in tech.items() if k in (
        "close", "amount_ma5", "amount_ma20", "volatility_20d",
        "max_drawdown_60d", "momentum_20d", "momentum_60d")})
    return out


def compute_money_flow_features(flow: Dict[str, Any]) -> Dict[str, Any]:
    """资金流特征(最近一次净流入快照)。"""
    if not flow:
        return {}
    main = float(flow.get("main_inflow", 0) or 0)
    super_ = float(flow.get("super_inflow", 0) or 0)
    return {
        "main_inflow": main,
        "super_inflow": super_,
        "flow_score": 100.0 if main > 0 else 0.0,       # 简化: 净流入方向打分
        "flow_direction": "in" if main > 0 else "out",
    }
