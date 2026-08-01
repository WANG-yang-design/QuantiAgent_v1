# -*- coding: utf-8 -*-
"""
轮动策略执行器 (无 Agent 参与时的规则策略)
==========================================
文档原则: 策略层计算基础信号, Agent 层综合决策, 风控层审核。
当回测/盘中不使用 Agent 时, 系统按本模块的规则逻辑直接生成订单意图。

逻辑(ETF动量轮动 + 持仓再平衡):
  1. 每交易日计算各标的特征(20日动量/成交额/波动率)
  2. 过滤低流动性/高波动标的, 按动量排名
  3. TopN 为持有目标:
     - 排名内持仓不足 → BUY 补至目标仓位
     - 排名外但有持仓 → SELL 全部(轮动换仓)
  4. 目标仓位 = 总资产 * target_weight(默认20%), 数量取整100股

参数配置: config.yaml → strategies.etf_momentum_rotation
查看/修改入口: config/config.yaml + 本文件
"""
import logging
from typing import Any, Callable, Dict, List, Optional

from core.config import get_settings
from features.technical_indicators import compute_technical_features

logger = logging.getLogger("strategy.rotation")


def load_rotation_params() -> Dict[str, Any]:
    """从 config.yaml 读取轮动策略参数(集中配置, 便于修改)。"""
    return get_settings().get("strategies.etf_momentum_rotation", {}) or {}


def build_rotation_signal_fn(initial_cash: float = 100000.0,
                             params: Optional[Dict[str, Any]] = None) -> Callable:
    """
    构建轮动信号函数(回测引擎用)。
    返回 signal_fn(asof, prices, d, broker) → {symbol: {action, qty, reason}}
    """
    p = dict(load_rotation_params())
    if params:
        p.update(params)
    top_n = int(p.get("top_n", 3))
    mom_window = int(p.get("mom_window", 20))
    min_amount = float(p.get("min_amount", 3e7))
    max_vol = float(p.get("max_vol", 0.50))
    target_weight = float(p.get("target_weight", 0.2))
    rebalance_threshold = float(p.get("rebalance_threshold", 0.15))  # 仓位偏离>15%才调仓

    def signal_fn(asof: Dict[str, List[dict]], prices: Dict[str, float],
                  d, broker=None) -> Dict[str, Dict[str, Any]]:
        # 1. 特征计算 + 过滤
        features: Dict[str, Dict[str, Any]] = {}
        for sym, bars in asof.items():
            if not bars:
                continue
            f = compute_technical_features(bars)
            if not f:
                continue
            amount_ma = float(f.get("amount_ma20", 0) or 0)
            vol = float(f.get("volatility_20d", 0) or 0)
            if amount_ma < min_amount:
                continue
            if vol > max_vol:
                continue
            features[sym] = f

        # 2. 动量排名
        ranked = sorted(features.keys(),
                        key=lambda s: float(features[s].get(f"momentum_{mom_window}d", 0) or 0),
                        reverse=True)
        top = set(ranked[:top_n])
        signals: Dict[str, Dict[str, Any]] = {}

        # 3. 排名外的持仓 → 卖出(轮动换仓)
        positions = (broker.positions if broker is not None else {}) or {}
        for sym, pos in positions.items():
            if sym not in top and pos.get("qty", 0) > 0 and sym in features:
                signals[sym] = {
                    "action": "SELL", "qty": pos["qty"],
                    "reason": f"跌出动量Top{top_n}, 轮动卖出",
                }

        # 4. TopN 持仓不足 → 买入至目标仓位(偏离超过阈值才调, 减少噪音交易)
        for sym in ranked[:top_n]:
            price = prices.get(sym, 0)
            if price <= 0:
                continue
            cur_qty = positions.get(sym, {}).get("qty", 0) or 0
            target_qty = int(initial_cash * target_weight / price // 100 * 100)
            diff = target_qty - cur_qty
            if diff >= 100 and (cur_qty == 0 or diff >= cur_qty * rebalance_threshold):
                signals[sym] = {
                    "action": "BUY", "qty": diff,
                    "reason": (f"动量排名前{top_n}, 补仓至目标权重{target_weight:.0%}"
                               f"(20日动量{float(features[sym].get(f'momentum_{mom_window}d', 0) or 0):+.2%})"),
                }
        return signals

    return signal_fn


def rotation_signals_to_plans(signals: Dict[str, Dict[str, Any]],
                              prices: Dict[str, float]) -> List[Dict[str, Any]]:
    """把轮动信号转成交易计划(供模拟盘/实盘手动执行时参考)。"""
    plans = []
    for sym, sig in signals.items():
        plans.append({
            "symbol": sym, "action": sig["action"], "qty": sig["qty"],
            "price": prices.get(sym, 0), "reason": sig.get("reason", ""),
        })
    return plans
