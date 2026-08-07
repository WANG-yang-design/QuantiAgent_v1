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
    """从 config.yaml 读取轮动策略参数(集中配置, 便于修改)。
    修复: Web"命名策略"一键应用到实盘后, 在此叠加 data/strategy_presets.json
    的 active_live 参数 —— 运行时生效, 无需重启, 且回测表单仍可单独覆盖。"""
    params = get_settings().get("strategies.etf_momentum_rotation", {}) or {}
    try:
        import json
        from core.config import ROOT_DIR
        f = ROOT_DIR / "data" / "strategy_presets.json"
        if f.exists():
            store = json.loads(f.read_text(encoding="utf-8")) or {}
            active = store.get("active_live", "")
            if active and active in (store.get("presets") or {}):
                params.update(store["presets"][active])
    except Exception:
        pass
    return dict(params)


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
    stop_loss_pct = float(p.get("stop_loss_pct", 0.08))            # 跟踪止损线(从峰值回撤超此值当日收盘价止损)
    market_filter = bool(p.get("market_filter", True))             # 市场风险过滤开关
    market_exit_threshold = float(p.get("market_exit_threshold", -0.03))   # 中位动量低于此值→清仓防守
    market_enter_threshold = float(p.get("market_enter_threshold", 0.0))   # 清仓后需回升到0以上才解除(滞回)
    cool_down_days = int(p.get("cool_down_days", 5))                       # 清仓冷却期(交易日内禁止重新开仓)
    min_order_amount = float(p.get("min_order_amount", 500))       # 最小买入金额(防碎单)
    warmup_days = int(p.get("warmup_days", 20))                    # 预热期(K线不足不开仓)
    min_hold_days = int(p.get("min_hold_days", 3))                 # 最小持仓天数(防"今天买明天卖")
    hold_buffer = int(p.get("hold_buffer", 1))                     # 卖出滞回: 跌出前(top_n+hold_buffer)才卖
    max_buy_momentum = float(p.get("max_buy_momentum", 0.25))      # 追高保护: 20日涨幅超过25%禁止买入(修复: 原30%仍常买在最高点)
    initial_ratio = float(p.get("initial_ratio", 0.5))             # 首仓比例: 首次买入只建目标仓位的50%(分批建仓)
    bottom_ratio = float(p.get("bottom_ratio", 0.2))               # 底仓比例: 排名跌出时减仓至底仓留观察, 不全清
    fresh_stop_mult = float(p.get("fresh_stop_mult", 1.5))         # 新仓止损放宽: 持有<min_hold_days时止损线放宽倍数
    # ---- 买点质量过滤(修复: "买在最高点→8%止损清仓"的反复磨损) ----
    require_above_ma20 = bool(p.get("require_above_ma20", True))   # 必须站上MA20才买(防下跌趋势接刀)
    max_distance_from_ma20 = float(p.get("max_distance_from_ma20", 0.12))  # 收盘价高出MA20超12%=短线过热, 不追
    # ---- 低位启动识别(修复: "不要放过低位看涨" —— 纯动量排名只认涨得多的,
    #      刚启动的低位票排不进去。从60日低点回升且站上MA20的标的, 排名加分) ----
    low_rebound_bonus = float(p.get("low_rebound_bonus", 0.015))   # 排名动量加分(相当于动量+1.5%)
    low_rebound_from_low_pct = float(p.get("low_rebound_from_low_pct", 0.10))  # 距60日低点回升≥10%才算启动
    # 有效单标的上限: 受总仓位约束 (target_weight 可被压缩)
    eff_weight = min(target_weight, max_total_position / max(top_n, 1))

    def _low_rebound(sym: str, features: Dict[str, Any],
                     asof: Dict[str, List[dict]]) -> float:
        """低位启动加分: 从60日低点回升≥阈值 且 收盘站上MA20 → 返回 bonus。"""
        if low_rebound_bonus <= 0:
            return 0.0
        f = features.get(sym) or {}
        close = float(f.get("close", 0) or 0)
        ma20 = float(f.get("ma20", 0) or 0)
        if close <= 0 or ma20 <= 0 or close < ma20:
            return 0.0
        bars = asof.get(sym) or []
        lows = [float(b.get("low") or 0) for b in bars[-60:] if (b.get("low") or 0) > 0]
        if not lows:
            return 0.0
        low60 = min(lows)
        if low60 <= 0:
            return 0.0
        rebound = close / low60 - 1
        if rebound >= low_rebound_from_low_pct:
            return low_rebound_bonus
        return 0.0

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
                risk_state["off_since"] = d     # 记录清仓日(冷却期起点)
            elif market_mom > market_enter_threshold:
                risk_state["off"] = False       # 明显回暖才解除(滞回)
        risk_off = risk_state["off"]
        # 清仓冷却期: risk_off 解除后 cool_down_days 交易日内禁止重新开仓(防反复进出)。
        # 修复: 原实现冷却期内 return 丢弃全部信号 —— 含止损/轮动卖出,
        # 清仓后残留持仓在冷却期内完全无保护。冷却期只应阻断买入。
        in_cool_down = False
        if risk_state["off_since"] is not None and not risk_off:
            since = risk_state["off_since"]
            if isinstance(since, date) and isinstance(d, date) \
                    and (d - since).days < cool_down_days:
                in_cool_down = True

        # 3. 排名(低位启动加分, 修复: 纯动量排名漏掉刚启动的低位标的)
        ranked = sorted(
            features.keys(),
            key=lambda s: (float(features[s].get(f"momentum_{mom_window}d", 0) or 0)
                           + _low_rebound(s, features, asof)),
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
            # 新仓保护: 刚买入(持有<min_hold_days)的正常波动容易触发8%回撤止损,
            # 造成"买在高点→次日-8%割肉"的反复磨损。新仓止损线放宽 fresh_stop_mult 倍
            # (修复: 原实现止损不区分新旧仓, 新仓当天就按8%触发)。
            buy_date = pos.get("buy_date")
            held = (d - buy_date).days if isinstance(buy_date, date) else min_hold_days
            stop_threshold = stop_loss_pct
            if held < min_hold_days:
                stop_threshold = stop_loss_pct * fresh_stop_mult
            # 跟踪止损(优先): 从峰值回撤超阈值 → 锁定利润/尽早离场
            if from_peak <= -stop_threshold:
                signals[sym] = {
                    "action": "SELL", "qty": pos["qty"],
                    "stop": True,
                    "reason": f"跟踪止损: 从高点{peak:.3f}回撤{from_peak:.1%}超过{-stop_threshold:.0%}"
                              f"(当前{price:.3f}, 盈亏{pnl_pct:+.1%}, 持有{held}天)",
                }
                continue
            # 注意: 原"硬止损(成本亏损2×线)"是死代码 —— 从峰值回撤的跟踪止损在
            # 任何成本亏损场景下都会先于它触发(peak≥cost 时 from_peak≤pnl_pct)。
            # 如需独立的"成本亏损止损", 请配置 stop_loss_pct 之外的新参数。
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
            # 轮动减仓(滞回): 排名跌破 top_n+hold_buffer → 减仓至底仓(留观察仓, 不全清)
            rank = rank_of.get(sym, 999)
            if rank >= sell_rank_limit:
                mom = float(features[sym].get(f"momentum_{mom_window}d", 0) or 0) if sym in features else 0
                # 保留底仓(bottom_ratio, 向上取整100份), 其余卖出
                # 修复: 原实现 keep 不按 100 份取整(300份→卖240留60), 产生非法手数
                import math
                keep = math.ceil(pos["qty"] * bottom_ratio / 100) * 100
                keep = min(keep, pos["qty"])
                sell_qty = pos["qty"] - keep
                if sell_qty < 100:
                    sell_qty = pos["qty"]
                    keep = 0
                reason = (f"排名第{rank + 1}, 跌出前{sell_rank_limit}, 减仓至底仓"
                          f"(卖出{sell_qty}份, 留{keep}份观察; 动量{mom:+.1%})")
                signals[sym] = {"action": "SELL", "qty": sell_qty, "reason": reason}

        # 5. 买入: 仅在市场非risk_off时; 动量必须为正(熊市不接飞刀); 冷却期内禁止开仓
        if risk_off or in_cool_down:
            return signals
        batch_amount = 0.0    # 本批次已买入金额(修复: 原实现多只各自按剩余现金算, 总买入超出现金)
        for sym in ranked[:top_n]:
            mom = float(features[sym].get(f"momentum_{mom_window}d", 0) or 0)
            if mom <= 0:
                continue                      # 负动量禁止买入
            if mom > max_buy_momentum:
                continue                      # 追高保护: 短期暴涨后禁止追买(防高位站岗)
            # 买点质量过滤(修复: 买在最高点→8%止损的反复磨损)
            f = features[sym]
            close = float(f.get("close", 0) or 0)
            ma20 = float(f.get("ma20", 0) or 0)
            if require_above_ma20 and close > 0 and ma20 > 0 and close < ma20:
                continue                      # 未站上MA20(下跌趋势)不接刀
            if ma20 > 0 and close > 0 and max_distance_from_ma20 > 0 \
                    and (close / ma20 - 1) > max_distance_from_ma20:
                continue                      # 短线过热: 高出MA20太多不追
            price = prices.get(sym, 0)
            if price <= 0:
                continue
            cur_qty = positions.get(sym, {}).get("qty", 0) or 0
            # 可用资金(总资产×有效权重 与 剩余现金(扣本批次已买) 取小)
            if broker is not None:
                total_asset = broker.cash + broker.position_value(prices)
                avail_cash = broker.cash - batch_amount
            else:
                total_asset = initial_cash
                avail_cash = initial_cash - batch_amount
            if avail_cash <= 0:
                break
            target_value = min(total_asset * eff_weight, avail_cash * 0.98)
            target_qty = int(target_value / price // 100 * 100)
            diff = target_qty - cur_qty
            # 最小买入金额(防碎单)
            if diff >= 100 and diff * price >= min_order_amount \
                    and (cur_qty == 0 or diff >= cur_qty * rebalance_threshold):
                if cur_qty == 0:
                    # 首次建仓: 只建目标仓位的一部分(分批建仓, 避免一次性追满)
                    target_qty = int(target_qty * initial_ratio // 100 * 100)
                    diff = target_qty
                    if diff < 100 or diff * price < min_order_amount:
                        continue
                    reason = (f"动量排名前{top_n}({mom:+.1%}), 分批建仓{initial_ratio:.0%}"
                              f"(目标权重{eff_weight:.0%}的{initial_ratio:.0%})")
                else:
                    reason = (f"动量排名前{top_n}({mom:+.1%}), 加仓至目标权重{eff_weight:.0%}")
                signals[sym] = {"action": "BUY", "qty": diff, "reason": reason}
                batch_amount += diff * price
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
