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


def active_rotation_preset_name() -> str:
    """Return the selected paper-rotation preset (legacy key supported)."""
    try:
        import json
        from core.config import ROOT_DIR
        f = ROOT_DIR / "data" / "strategy_presets.json"
        if f.exists():
            store = json.loads(f.read_text(encoding="utf-8")) or {}
            active = str(store.get("active_paper") or store.get("active_live") or "")
            params = (store.get("presets") or {}).get(active) or {}
            return active if params.get("universe_mode") == "dynamic_etf" else ""
    except Exception:
        pass
    return ""


def validate_rotation_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """校验并返回轮动参数副本，避免非法组合静默退化为无交易策略。"""
    p = dict(params or {})
    if int(p.get("top_n", 3)) not in range(1, 11):
        raise ValueError("top_n 应在 1~10 之间")
    if int(p.get("mom_window", 20)) not in (5, 10, 15, 20, 30, 60):
        raise ValueError("mom_window 仅支持 5/10/15/20/30/60")
    if int(p.get("trend_ma_window", 20)) not in (10, 20, 60):
        raise ValueError("trend_ma_window 仅支持 10/20/60")
    if not 1 <= int(p.get("rebalance_interval_days", 1)) <= 20:
        raise ValueError("rebalance_interval_days 应在 1~20 之间")
    for key in ("hard_stop_pct", "trailing_stop_pct"):
        if key in p and not 0.02 <= float(p[key]) <= 0.30:
            raise ValueError(f"{key} 应在 2%~30% 之间")
    if "trailing_stop_activation" in p and not 0 <= float(p["trailing_stop_activation"]) <= 0.50:
        raise ValueError("trailing_stop_activation 应在 0~50% 之间")
    for key in ("target_weight", "max_total_position", "initial_ratio", "bottom_ratio"):
        if key in p and not 0 <= float(p[key]) <= 1:
            raise ValueError(f"{key} 应在 0~1 之间")
    if float(p.get("min_momentum", 0.01)) >= float(p.get("max_buy_momentum", 0.25)):
        raise ValueError("min_momentum 必须小于 max_buy_momentum")
    range_pos = float(p.get("max_position_in_recent_range", 1.0))
    if not 0 < range_pos <= 1:
        raise ValueError("max_position_in_recent_range 应在 0~1 之间")
    return p


def load_rotation_params(include_live: bool = True) -> Dict[str, Any]:
    """从 config.yaml 读取轮动策略参数(集中配置, 便于修改)。
    模拟盘自动轮动叠加 data/strategy_presets.json 的 active_paper 参数；
    ``include_live`` 保留旧参数名兼容内部调用。回测始终传 False，彼此隔离。"""
    # Copy before applying the live overlay.  Mutating the Settings mapping here
    # would leak the active-live preset into every later backtest in-process.
    params = dict(get_settings().get("strategies.etf_momentum_rotation", {}) or {})
    try:
        import json
        from core.config import ROOT_DIR
        f = ROOT_DIR / "data" / "strategy_presets.json"
        if include_live and f.exists():
            store = json.loads(f.read_text(encoding="utf-8")) or {}
            active = store.get("active_paper") or store.get("active_live", "")
            if (active and active in (store.get("presets") or {}) and
                    (store["presets"][active] or {}).get("universe_mode") == "dynamic_etf"):
                active_params = dict(store["presets"][active] or {})
                active_params.pop("universe_mode", None)
                params.update(active_params)
                # Live loading must provide the same legacy compatibility as
                # backtest loading: an old one-line stop remains self-contained
                # and cannot inherit unrelated new defaults.
                if "stop_loss_pct" in active_params:
                    if "hard_stop_pct" not in active_params:
                        params["hard_stop_pct"] = active_params["stop_loss_pct"]
                    if "trailing_stop_pct" not in active_params:
                        params["trailing_stop_pct"] = active_params["stop_loss_pct"]
    except Exception:
        pass
    return dict(params)


def resolve_rotation_params(params: Optional[Dict[str, Any]] = None,
                            use_live_preset: bool = False) -> Dict[str, Any]:
    """Return the complete, validated parameter set used by one run.

    Backtests deliberately ignore the active paper preset so a historical run
    cannot change merely because the operator switched it later.
    """
    resolved = load_rotation_params(include_live=use_live_preset)
    overrides = dict(params or {})
    if overrides:
        resolved.update(overrides)
    # Old saved strategies only had one stop_loss_pct. Preserve their intended
    # threshold while new strategies can tune the two models independently.
    if "stop_loss_pct" in overrides:
        resolved.setdefault("hard_stop_pct", overrides["stop_loss_pct"])
        resolved.setdefault("trailing_stop_pct", overrides["stop_loss_pct"])
        if "hard_stop_pct" not in overrides:
            resolved["hard_stop_pct"] = overrides["stop_loss_pct"]
        if "trailing_stop_pct" not in overrides:
            resolved["trailing_stop_pct"] = overrides["stop_loss_pct"]
    return validate_rotation_params(resolved)


def build_rotation_signal_fn(initial_cash: float = 100000.0,
                             params: Optional[Dict[str, Any]] = None,
                             use_live_preset: bool = False,
                             feature_cache: Optional[Dict[Any, Dict[str, Any]]] = None,
                             universe_provider: Optional[Callable] = None,
                             ) -> Callable:
    """
    构建轮动信号函数(回测引擎用)。
    返回 signal_fn(asof, prices, d, broker) → {symbol: {action, qty, reason}}
    """
    p = resolve_rotation_params(params, use_live_preset=use_live_preset)
    top_n = int(p.get("top_n", 3))
    mom_window = int(p.get("mom_window", 20))
    min_amount = float(p.get("min_amount", 3e7))
    max_vol = float(p.get("max_vol", 0.50))
    target_weight = float(p.get("target_weight", 0.2))
    rebalance_threshold = float(p.get("rebalance_threshold", 0.15))
    max_total_position = float(p.get("max_total_position", 0.90))  # 总仓位上限(防多只超配)
    legacy_stop = float(p.get("stop_loss_pct", 0.08))
    hard_stop_pct = float(p.get("hard_stop_pct", legacy_stop))
    trailing_stop_pct = float(p.get("trailing_stop_pct", legacy_stop))
    trailing_stop_activation = float(p.get("trailing_stop_activation", 0.05))
    market_filter = bool(p.get("market_filter", True))             # 市场风险过滤开关
    market_exit_threshold = float(p.get("market_exit_threshold", -0.03))   # 中位动量低于此值→清仓防守
    market_enter_threshold = float(p.get("market_enter_threshold", 0.0))   # 清仓后需回升到0以上才解除(滞回)
    cool_down_days = int(p.get("cool_down_days", 5))                       # 清仓冷却期(交易日内禁止重新开仓)
    rebalance_interval_days = max(1, int(p.get("rebalance_interval_days", 1)))
    min_order_amount = float(p.get("min_order_amount", 500))       # 最小买入金额(防碎单)
    warmup_days = int(p.get("warmup_days", 20))                    # 预热期(K线不足不开仓)
    min_hold_days = int(p.get("min_hold_days", 3))                 # 最小持仓天数(防"今天买明天卖")
    hold_buffer = int(p.get("hold_buffer", 1))                     # 卖出滞回: 跌出前(top_n+hold_buffer)才卖
    max_buy_momentum = float(p.get("max_buy_momentum", 0.25))      # 追高保护: 20日涨幅超过25%禁止买入(修复: 原30%仍常买在最高点)
    min_momentum = float(p.get("min_momentum", 0.01))
    initial_ratio = float(p.get("initial_ratio", 0.5))             # 首仓比例: 首次买入只建目标仓位的50%(分批建仓)
    bottom_ratio = float(p.get("bottom_ratio", 0.2))               # 底仓比例: 排名跌出时减仓至底仓留观察, 不全清
    fresh_stop_mult = float(p.get("fresh_stop_mult", 1.5))         # 新仓止损放宽: 持有<min_hold_days时止损线放宽倍数
    # ---- 买点质量过滤(修复: "买在最高点→8%止损清仓"的反复磨损) ----
    require_above_ma20 = bool(p.get("require_above_ma20", True))   # 必须站上MA20才买(防下跌趋势接刀)
    trend_ma_window = int(p.get("trend_ma_window", 20))
    if trend_ma_window not in (10, 20, 60):
        raise ValueError("trend_ma_window 仅支持 10/20/60")
    max_distance_from_ma20 = float(p.get("max_distance_from_ma20", 0.12))  # 收盘价高出MA20超12%=短线过热, 不追
    max_position_in_recent_range = float(
        p.get("max_position_in_recent_range", 1.0))  # 20日区间位置上限；1=关闭
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
    supplied_state = p.get("_risk_state")
    risk_state = supplied_state if isinstance(supplied_state, dict) else {
        "off": False, "off_since": None}
    supplied_rebalance_state = p.get("_rebalance_state")
    rebalance_state = (supplied_rebalance_state
                       if isinstance(supplied_rebalance_state, dict)
                       else {"signal_day_no": 0, "last_signal_date": ""})

    def signal_fn(asof: Dict[str, List[dict]], prices: Dict[str, float],
                  d, broker=None) -> Dict[str, Dict[str, Any]]:
        signal_date = str(d)[:10]
        # A live/paper strategy function is rebuilt for every scheduler run,
        # unlike a backtest closure. Keep the counter in an optional mutable
        # state object and never double-count a manual rerun on the same date.
        if str(rebalance_state.get("last_signal_date") or "") != signal_date:
            rebalance_state["signal_day_no"] = int(
                rebalance_state.get("signal_day_no", 0) or 0) + 1
            rebalance_state["last_signal_date"] = signal_date
        signal_day_no = int(rebalance_state.get("signal_day_no", 1) or 1)
        can_rebalance = ((signal_day_no - 1) % rebalance_interval_days == 0)
        signals: Dict[str, Dict[str, Any]] = {}
        positions = (broker.positions if broker is not None else {}) or {}

        # Stage 1 dynamic pool gates rankings and new buys. Existing holdings
        # remain in the input so stop-loss and rotation exits stay executable.
        eligible_symbols = (set(universe_provider(d, asof))
                            if universe_provider is not None else set(asof))

        # 0. 预热期: 数据不足时不开仓(特征不可信)
        n_bars = max((len(b) for b in asof.values()), default=0)
        if n_bars < warmup_days:
            return signals

        # 1. 特征计算 + 过滤
        features: Dict[str, Dict[str, Any]] = {}
        all_features: Dict[str, Dict[str, Any]] = {}
        for sym, bars in asof.items():
            if sym not in eligible_symbols and sym not in positions:
                continue
            if not bars:
                continue
            cache_key = (sym, bars[-1].get("trade_date"), len(bars))
            f = feature_cache.get(cache_key) if feature_cache is not None else None
            if f is None:
                f = compute_technical_features(bars)
                if feature_cache is not None:
                    feature_cache[cache_key] = f
            if not f:
                continue
            all_features[sym] = f
            amount_ma = float(f.get("amount_ma20", 0) or 0)
            vol = float(f.get("volatility_20d", 0) or 0)
            if amount_ma < min_amount:
                continue
            if vol > max_vol:
                continue
            if sym in eligible_symbols:
                features[sym] = f

        # 2. 市场环境: 池内标的20日动量中位数(proxy of 市场状态), 带滞回
        moms = [float(all_features[s].get(f"momentum_{mom_window}d", 0) or 0)
                for s in all_features]
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
            stop_threshold = hard_stop_pct
            if held < min_hold_days:
                stop_threshold = hard_stop_pct * fresh_stop_mult
            trailing_active = peak / cost - 1 >= trailing_stop_activation
            if pnl_pct <= -stop_threshold:
                signals[sym] = {
                    "action": "SELL", "qty": pos["qty"],
                    "stop": True,
                    "reason": f"成本止损: 成本{cost:.3f}亏损{pnl_pct:.1%}超过{-stop_threshold:.0%}"
                              f"(当前{price:.3f}, 盈亏{pnl_pct:+.1%}, 持有{held}天)",
                }
                continue
            if trailing_active and from_peak <= -trailing_stop_pct:
                signals[sym] = {
                    "action": "SELL", "qty": pos["qty"],
                    "stop": True,
                    "reason": f"移动止损: 盈利曾达{peak / cost - 1:+.1%}, "
                              f"从高点{peak:.3f}回撤{from_peak:.1%}超过"
                              f"{-trailing_stop_pct:.0%}(当前{price:.3f})",
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
            # 轮动减仓(滞回): 排名跌破 top_n+hold_buffer → 减仓至底仓(留观察仓, 不全清)
            # 当日仅因流动性/波动率过滤掉的持仓不视为排名垫底，避免误减仓。
            if not can_rebalance or sym not in features:
                continue
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
        if risk_off or in_cool_down or not can_rebalance:
            return signals
        batch_amount = 0.0    # 本批次已买入金额(修复: 原实现多只各自按剩余现金算, 总买入超出现金)
        for sym in ranked[:top_n]:
            mom = float(features[sym].get(f"momentum_{mom_window}d", 0) or 0)
            if mom < min_momentum:
                continue                      # 负动量禁止买入
            if mom > max_buy_momentum:
                continue                      # 追高保护: 短期暴涨后禁止追买(防高位站岗)
            # 买点质量过滤(修复: 买在最高点→8%止损的反复磨损)
            f = features[sym]
            close = float(f.get("close", 0) or 0)
            trend_ma = float(f.get(f"ma{trend_ma_window}", 0) or 0)
            if require_above_ma20 and close > 0 and trend_ma > 0 and close < trend_ma:
                continue                      # 未站上MA20(下跌趋势)不接刀
            if trend_ma > 0 and close > 0 and max_distance_from_ma20 > 0 \
                    and (close / trend_ma - 1) > max_distance_from_ma20:
                continue                      # 短线过热: 高出MA20太多不追
            if 0 < max_position_in_recent_range < 1:
                recent = (asof.get(sym) or [])[-20:]
                lows = [float(b.get("low") or 0) for b in recent
                        if float(b.get("low") or 0) > 0]
                highs = [float(b.get("high") or 0) for b in recent
                         if float(b.get("high") or 0) > 0]
                if lows and highs and max(highs) > min(lows):
                    range_pos = (close - min(lows)) / (max(highs) - min(lows))
                    if range_pos > max_position_in_recent_range:
                        continue              # 已处20日区间顶部，等待回撤而非追高
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

    signal_fn.strategy_timeframe = "daily"
    signal_fn.universe_provider = universe_provider
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
