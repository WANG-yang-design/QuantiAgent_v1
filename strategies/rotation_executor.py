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
from datetime import date
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
    rebalance_threshold = float(p.get("rebalance_threshold", 0.15))
    max_total_position = float(p.get("max_total_position", 0.90))  # 总仓位上限(防多只超配)
    stop_loss_pct = float(p.get("stop_loss_pct", 0.08))            # 单标的止损线(浮亏超8%当日收盘价止损, 不等次日)
    market_filter = bool(p.get("market_filter", True))             # 市场风险过滤开关
    market_exit_threshold = float(p.get("market_exit_threshold", -0.03))   # 中位动量低于此值→清仓防守
    market_enter_threshold = float(p.get("market_enter_threshold", 0.0))   # 清仓后需回升到0以上才解除(滞回)
    cool_down_days = int(p.get("cool_down_days", 5))                       # 清仓冷却期(交易日内禁止重新开仓)
    min_order_amount = float(p.get("min_order_amount", 500))       # 最小买入金额(防碎单)
    warmup_days = int(p.get("warmup_days", 20))                    # 预热期(K线不足不开仓)
    min_hold_days = int(p.get("min_hold_days", 3))                 # 最小持仓天数(防"今天买明天卖")
    hold_buffer = int(p.get("hold_buffer", 1))                     # 卖出滞回: 跌出前(top_n+hold_buffer)才卖
    max_buy_momentum = float(p.get("max_buy_momentum", 0.30))      # 追高保护: 20日涨幅超过30%禁止买入
    # 有效单标的上限: 受总仓位约束 (target_weight 可被压缩)
    eff_weight = min(target_weight, max_total_position / max(top_n, 1))

    # 市场风险状态(滞回: 清仓后需明显回暖 + 冷却期, 避免反复进出)
    risk_state = {"off": False, "off_since": None}

    def signal_fn(asof: Dict[str, List[dict]], prices: Dict[str, float],
                  d, broker=None) -> Dict[str, Dict[str, Any]]:
        signals: Dict[str, Dict[str, Any]] = {}
        positions = (broker.positions if broker is not None else {}) or {}

        # 0. 预热期: 数据不足时不开仓(特征不可信)
        n_bars = max((len(b) for b in asof.values()), default=0)
        if n_bars < warmup_days:
            return signals

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

        # 2. 市场环境: 池内标的20日动量中位数(proxy of 市场状态), 带滞回
        moms = [float(features[s].get(f"momentum_{mom_window}d", 0) or 0)
                for s in features]
        market_mom = 0.0
        if len(moms) >= 3:
            market_mom = sorted(moms)[len(moms) // 2]
        if market_filter:
            if market_mom < market_exit_threshold:
                risk_state["off"] = True        # 触发清仓
            elif market_mom > market_enter_threshold:
                risk_state["off"] = False       # 明显回暖才解除(滞回)
        risk_off = risk_state["off"]

        # 3. 排名
        ranked = sorted(features.keys(),
                        key=lambda s: float(features[s].get(f"momentum_{mom_window}d", 0) or 0),
                        reverse=True)
        rank_of = {sym: i for i, sym in enumerate(ranked)}
        top = set(ranked[:top_n])
        # 卖出滞回线: 排名跌破 top_n + hold_buffer 才触发轮动卖出
        sell_rank_limit = top_n + hold_buffer

        # 4. 卖出: 跟踪止损(从峰值回撤) > 硬止损(成本亏损) > 市场risk_off > 最小持仓 > 轮动(滞回)
        for sym, pos in positions.items():
            if pos.get("qty", 0) <= 0:
                continue
            price = prices.get(sym, 0)
            cost = pos.get("cost", 0)
            # 更新持仓峰值(跟踪止损基准)
            peak = pos.get("peak") or cost
            if price > peak:
                pos["peak"] = peak = price
            if price <= 0 or cost <= 0:
                continue
            pnl_pct = price / cost - 1            # 相对成本盈亏
            from_peak = price / peak - 1          # 从最高点回撤
            # 跟踪止损(优先): 从峰值回撤超阈值 → 锁定利润/尽早离场
            if from_peak <= -stop_loss_pct:
                signals[sym] = {
                    "action": "SELL", "qty": pos["qty"],
                    "stop": True,
                    "reason": f"跟踪止损: 从高点{peak:.3f}回撤{from_peak:.1%}超过{-stop_loss_pct:.0%}"
                              f"(当前{price:.3f}, 盈亏{pnl_pct:+.1%})",
                }
                continue
            # 硬止损: 相对成本亏损超过 2×止损线(防长期阴跌无峰值回撤)
            if pnl_pct <= -stop_loss_pct * 2:
                signals[sym] = {
                    "action": "SELL", "qty": pos["qty"],
                    "stop": True,
                    "reason": f"硬止损: 成本亏损{pnl_pct:.1%}超过{-stop_loss_pct * 2:.0%}",
                }
                continue
            # 市场risk_off: 清仓防守(避开系统性下跌)
            if risk_off:
                signals[sym] = {
                    "action": "SELL", "qty": pos["qty"],
                    "reason": f"市场risk_off(中位动量{market_mom:.1%}), 清仓防守",
                }
                continue
            # 最小持仓保护: 刚买入的持仓至少持有 min_hold_days 天
            buy_date = pos.get("buy_date")
            held = (d - buy_date).days if isinstance(buy_date, date) else min_hold_days
            if held < min_hold_days:
                continue
            # 轮动卖出(滞回): 排名跌破 top_n+hold_buffer 才卖
            rank = rank_of.get(sym, 999)
            if rank >= sell_rank_limit:
                mom = float(features[sym].get(f"momentum_{mom_window}d", 0) or 0) if sym in features else 0
                reason = (f"排名第{rank + 1}, 跌出前{sell_rank_limit}, 轮动卖出"
                          f"(动量{mom:+.1%})") if sym in features else "标的失去流动性, 卖出"
                signals[sym] = {"action": "SELL", "qty": pos["qty"], "reason": reason}

        # 5. 买入: 仅在市场非risk_off时; 动量必须为正(熊市不接飞刀)
        if risk_off:
            return signals
        for sym in ranked[:top_n]:
            mom = float(features[sym].get(f"momentum_{mom_window}d", 0) or 0)
            if mom <= 0:
                continue                      # 负动量禁止买入
            if mom > max_buy_momentum:
                continue                      # 追高保护: 短期暴涨后禁止追买(防高位站岗)
            price = prices.get(sym, 0)
            if price <= 0:
                continue
            cur_qty = positions.get(sym, {}).get("qty", 0) or 0
            # 可用资金(总资产×有效权重 与 剩余现金 取小)
            if broker is not None:
                total_asset = broker.cash + broker.position_value(prices)
                avail_cash = broker.cash
            else:
                total_asset = initial_cash
                avail_cash = initial_cash
            target_value = min(total_asset * eff_weight, avail_cash * 0.98)
            target_qty = int(target_value / price // 100 * 100)
            diff = target_qty - cur_qty
            # 最小买入金额(防碎单)
            if diff >= 100 and diff * price >= min_order_amount \
                    and (cur_qty == 0 or diff >= cur_qty * rebalance_threshold):
                signals[sym] = {
                    "action": "BUY", "qty": diff,
                    "reason": (f"动量排名前{top_n}({mom:+.1%}), 补仓至目标权重{eff_weight:.0%}"),
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
