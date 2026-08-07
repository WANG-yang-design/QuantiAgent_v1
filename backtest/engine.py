# -*- coding: utf-8 -*-
"""
回测引擎 (日线 + 分钟, 事件驱动, 无未来函数)
=============================================
核心原则(文档10):
1. T日收盘后只允许使用T日及之前数据
2. 分钟回测按增量喂数据, 不能一次性把未来K线给AI
3. 新闻/公告按发布时间进入系统
4. 关键节点才调用Agent, 避免成本爆炸

撮合: 简单(次日开盘+滑点) / 中级(限价单按K线高低价) / 高级(预留)
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.ids import gen_backtest_id, gen_trace_id
from database import repository as repo
from features.technical_indicators import compute_technical_features, to_frame
from backtest.metrics import compute_metrics

logger = logging.getLogger("backtest.engine")


@dataclass
class BacktestBroker:
    """回测账户(独立于模拟盘, 保证可复现)。"""
    cash: float = 100000.0
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # symbol -> {qty, cost, avail}
    total_fee: float = 0.0
    total_slippage: float = 0.0
    trades: List[Dict[str, Any]] = field(default_factory=list)
    day_pnl: float = 0.0
    prev_equity: float = 100000.0
    skipped_buys: int = 0          # 因资金不足被跳过的买入次数

    def equity(self, prices: Dict[str, float]) -> float:
        mv = sum(p["qty"] * prices.get(s, p["cost"]) for s, p in self.positions.items())
        return self.cash + mv

    def position_value(self, prices: Dict[str, float]) -> float:
        return sum(p["qty"] * prices.get(s, p["cost"]) for s, p in self.positions.items())

    def can_buy(self, symbol: str, qty: int, price: float) -> bool:
        return self.cash >= qty * price * 1.001

    def buy(self, symbol: str, qty: int, price: float, fee: float, slippage_per_share: float,
            date_: date, reason: str = "", name: str = ""):
        cost = qty * price + fee
        self.cash -= cost
        p = self.positions.setdefault(symbol, {"qty": 0, "cost": 0.0, "avail": 0,
                                               "buy_date": date_, "name": name,
                                               "peak": price, "today_buy": 0})
        old_cost = p["qty"] * p["cost"]
        p["qty"] += qty
        p["cost"] = (old_cost + qty * price) / p["qty"]
        p["avail"] += qty
        p["today_buy"] += qty                    # 当日买入量(T+1 检查)
        p["peak"] = max(p.get("peak") or price, price)   # 持仓最高价(跟踪止损基准)
        slip_total = slippage_per_share * qty        # 滑点总成本(每股×数量)
        self.total_fee += fee
        self.total_slippage += slip_total
        self.trades.append({"symbol": symbol, "name": name, "side": "BUY", "qty": qty,
                            "price": price, "amount": qty * price, "fee": fee,
                            "slippage_cost": round(slip_total, 4), "date": date_,
                            "reason": reason, "pnl": 0, "hold_days": 0})

    def sell(self, symbol: str, qty: int, price: float, fee: float, slippage_per_share: float,
             date_: date, reason: str = "", name: str = ""):
        p = self.positions[symbol]
        p["qty"] -= qty
        p["avail"] -= qty
        pnl = (price - p["cost"]) * qty - fee
        slip_total = slippage_per_share * qty
        self.cash += qty * price - fee
        self.total_fee += fee
        self.total_slippage += slip_total
        self.trades.append({"symbol": symbol, "name": name or p.get("name", ""),
                            "side": "SELL", "qty": qty, "price": price,
                            "amount": qty * price, "fee": fee,
                            "slippage_cost": round(slip_total, 4), "date": date_,
                            "reason": reason, "pnl": round(pnl, 4),
                            "hold_days": (date_ - p["buy_date"]).days
                            if isinstance(p["buy_date"], date) else 0})
        if p["qty"] <= 0:
            del self.positions[symbol]

    def start_new_day(self, date_: date):
        """新交易日: 上一日买入数量解锁(T+1 可卖)。"""
        for p in self.positions.values():
            p["today_buy"] = 0

    def sellable_qty(self, symbol: str, t0: bool = False) -> int:
        """可卖数量: T+0 品种全部可卖; T+1 品种当日买入部分不可卖。"""
        p = self.positions.get(symbol)
        if p is None:
            return 0
        if t0:
            return p["qty"]
        return max(0, p["qty"] - p.get("today_buy", 0))

    def close_positions(self, prices: Dict[str, float], date_: date, fee_fn=None):
        """回测结束强制平仓(统计用)。fee_fn: 与引擎一致的手续费函数。"""
        for symbol in list(self.positions.keys()):
            p = self.positions[symbol]
            if p["qty"] > 0 and prices.get(symbol, 0) > 0:
                price = prices[symbol]
                fee = fee_fn(price * p["qty"]) if fee_fn else price * p["qty"] * 0.00025
                self.sell(symbol, p["qty"], price, fee, 0.0, date_, "期末平仓",
                          name=p.get("name", ""))


class BacktestEngine:
    """回测引擎: 日线/分钟双模式。"""

    def __init__(self, start: date, end: date, initial_cash: float = 100000.0,
                 slippage: Optional[float] = None, mode: str = "daily",
                 use_agents: bool = False, agent_interval_days: int = 5,
                 name: str = "", run_id: Optional[str] = None):
        self.start = start
        self.end = end
        self.mode = mode
        self.use_agents = use_agents
        self.agent_interval_days = max(int(agent_interval_days or 5), 1)
        self.name = name or f"回测{start}-{end}"
        rules = get_settings().section("trading_rules")
        self.slippage = slippage if slippage is not None else float(
            rules.get("slippage", {}).get("daily_bar", 0.001))
        # 修复: agent_interval_days=0 时 i % 0 除零崩溃
        self.agent_interval_days = max(int(agent_interval_days or 5), 1)
        fees = rules.get("fees", {})
        self.fee_rate = float(fees.get("commission_rate", 0.00025))
        self.fee_min = float(fees.get("commission_min_etf", 0.0))   # ETF 免最低5元门槛
        self.transfer_rate = float(fees.get("transfer_fee_rate", 0.00001))
        self.broker = BacktestBroker(initial_cash)
        self.initial_cash = initial_cash     # 保存真实初始资金(结果展示用)
        self.run_id = run_id or gen_backtest_id()   # 复用提交时的 run_id(避免重复记录)
        self.equity_curve: List[float] = [initial_cash]
        self.equity_dates: List[str] = [str(start)]
        self.position_curve: List[float] = [0.0]     # 每日持仓市值曲线
        self.benchmark_curve: List[float] = []
        self.name_map: Dict[str, str] = {}           # symbol -> 中文名
        self.progress_cb = None                      # 进度回调(异步任务用)
        self.params: Dict[str, Any] = {}             # 回测参数(结果展示用)

    # ------------------------------------------------------------------
    def _calc_fee(self, amount: float) -> float:
        """回测手续费(与模拟盘一致): ETF 佣金按实际费率(免最低5元) + 过户费。"""
        commission = max(amount * self.fee_rate, self.fee_min)
        return round(commission + amount * self.transfer_rate, 4)

    # ------------------------------------------------------------------
    def run_daily(self, data_loader, signal_fn, benchmark_symbol: str = "000300"):
        """
        日线回测主循环。
        data_loader(symbol, asof) -> bars(截至asof, 含asof)
        signal_fn(asof, prices) -> {symbol: {"action": BUY/SELL, "qty": n, "price": limit}}
        """
        repo.save_backtest_run({
            "run_id": self.run_id, "name": self.name,
            "start_date": self.start, "end_date": self.end,
            "mode": "daily", "status": "RUNNING",
            "config_json": {"use_agents": self.use_agents,
                            "slippage": self.slippage,
                            "agent_interval_days": self.agent_interval_days},
        })
        trade_dates = data_loader.trade_dates(self.start, self.end)
        total_days = max(len(trade_dates), 1)
        # 预加载K线: 起点前额外加载历史(算动量/均线等指标用, 仍无未来函数:
        # 指标只用 ≤T 数据, 交易只在 [start, end] 区间发生)
        load_start = self.start - timedelta(days=250)
        bars_by_symbol = {s: data_loader.load_all_daily(s, load_start, self.end)
                          for s in data_loader.universe()}
        if not trade_dates:
            logger.warning("回测区间内无交易日, 返回空结果")
            return self._finalize()

        # 待成交订单意图(T日生成 → T+1开盘成交)。
        # 修复: 原实现把成交记账提前到信号日迭代, 导致 today_buy 在成交日
        # 被 start_new_day 提前清零(T+1 提前一天解锁), 且净值提前一天反映持仓。
        pending_fills: List[Dict[str, Any]] = []

        for i, d in enumerate(trade_dates):
            # 进度回调(供 Web 异步任务显示进度)
            if self.progress_cb and (i % 10 == 0 or i == total_days - 1):
                self.progress_cb(i + 1, total_days)
            # T+1 解锁: 昨日及以前买入的今日可卖
            self.broker.start_new_day(d)
            # 先执行昨日生成的订单意图(今日开盘价成交, 记账落在今日迭代)
            self._execute_pending_fills(pending_fills, bars_by_symbol, d)
            # ---- 仅使用截至 d 的数据(含 d) ----
            asof = {s: [b for b in bars if b["trade_date"] <= d] for s, bars in bars_by_symbol.items()}
            prices = {s: (bs[-1]["close"] if bs else 0) for s, bs in asof.items()}

            # 特征与信号(脚本策略; Agent 模式在关键节点介入)
            if self.use_agents and i % self.agent_interval_days == 0:
                signals = self._agent_signals(asof, d)
            else:
                # signal_fn 可接收 broker(轮动策略需要持仓做再平衡)
                try:
                    signals = signal_fn(asof, prices, d, self.broker)
                except TypeError:
                    signals = signal_fn(asof, prices, d)

            # ---- 信号入队(T+1 开盘成交) ----
            for symbol, sig in signals.items():
                action = sig.get("action")
                qty = int(sig.get("qty", 0) or 0)
                if action == "BUY" and qty > 0:
                    # 预检(按T日收盘价, 排除明显超支); 成交时按T+1开盘价二次校验
                    if self.broker.can_buy(symbol, qty, prices.get(symbol, 0)):
                        pending_fills.append({
                            "symbol": symbol, "qty": qty, "side": "BUY",
                            "sig": sig, "plan_date": d,
                        })
                    else:
                        self.broker.skipped_buys += 1
                elif action == "SELL" and qty > 0:
                    # T+1 检查: 当日买入部分不可卖(T+0 品种除外)
                    from core.symbol_utils import is_t0_etf
                    sellable = self.broker.sellable_qty(symbol, t0=is_t0_etf(symbol))
                    qty = min(qty, sellable)
                    if qty <= 0:
                        continue
                    sig = {**sig, "qty": qty}
                    if sig.get("stop"):
                        # 止损: 当日收盘价立即成交(模拟盘中触发市价止损, 不等次日开盘)
                        self._fill_at_close(symbol, qty, "SELL", prices, d, sig)
                    else:
                        pending_fills.append({
                            "symbol": symbol, "qty": qty, "side": "SELL",
                            "sig": sig, "plan_date": d,
                        })

            # 持仓市值 + 净值(反映 d 日开盘已成交的持仓, 不包含明日才成交的)
            self.broker.day_pnl = self.broker.equity(prices) - self.broker.prev_equity
            self.broker.prev_equity = self.broker.equity(prices)
            self.equity_curve.append(self.broker.equity(prices))
            self.equity_dates.append(str(d))
            self.position_curve.append(self.broker.position_value(prices))

        # 期末: 平仓 + 基准曲线
        prices = {s: (bs[-1]["close"] if bs else 0) for s, bs in asof.items()}
        self.broker.close_positions(prices, trade_dates[-1], fee_fn=self._calc_fee)
        self.equity_curve.append(self.broker.equity(prices))
        self.equity_dates.append(str(trade_dates[-1]))
        self.position_curve.append(0.0)

        bench = data_loader.load_benchmark(benchmark_symbol, self.start, self.end)
        if bench:
            b0 = bench[0]["close"]
            # 与净值曲线对齐: 基准归一化到与回测相同的初始资金(非固定10万)
            init = self.initial_cash
            self.benchmark_curve = [init] + \
                [b / b0 * init for b in [x["close"] for x in bench]] + \
                [bench[-1]["close"] / b0 * init]
            if len(self.benchmark_curve) > len(self.equity_curve):
                self.benchmark_curve = self.benchmark_curve[:len(self.equity_curve)]

        return self._finalize()

    # ------------------------------------------------------------------
    def _execute_pending_fills(self, pending_fills: List[dict],
                               bars_by_symbol: Dict[str, List[dict]],
                               d: date):
        """
        执行 T-1 日生成的订单意图, 以 T 日开盘价成交(简单撮合)。
        修复: ①成交记账落在实际成交日(T日), today_buy 不再提前清零(T+1规则正确);
        ②成交时按开盘价二次校验资金, 跳空高开超支则截断数量或跳过(B13)。
        """
        for fill in list(pending_fills):
            symbol = fill["symbol"]
            qty = int(fill["qty"])
            side = fill["side"]
            bars = bars_by_symbol.get(symbol) or []
            # 修复: 停牌日挂单既不成交也不移除, 复牌后按复牌日开盘价
            # 成交严重偏离信号时点。超过 5 个交易日未成交的挂单直接丢弃。
            plan_date = fill.get("plan_date")
            if plan_date and (d - plan_date).days > 5:
                logger.warning("丢弃过期挂单 %s %s(计划日 %s)", side, symbol, plan_date)
                pending_fills.remove(fill)
                continue
            nxt = next((b for b in bars if b["trade_date"] == d), None)
            if nxt is None:
                continue
            fill_price = nxt["open"]
            slip = fill_price * self.slippage
            price = fill_price + slip if side == "BUY" else fill_price - slip
            fee = self._calc_fee(price * qty)
            name = self.name_map.get(symbol, "")

            if side == "BUY":
                # 成交价级资金重校验: 开盘价跳空可能超出预检
                if not self.broker.can_buy(symbol, qty, price):
                    # 尝试按可用资金截断(向下取整到100股)
                    max_qty = int(self.broker.cash / (price * 1.001) // 100 * 100)
                    if max_qty >= 100:
                        qty = max_qty
                        fee = self._calc_fee(price * qty)
                    else:
                        self.broker.skipped_buys += 1
                        pending_fills.remove(fill)
                        continue
                self.broker.buy(symbol, qty, price, fee, slip, d,
                                fill["sig"].get("reason", ""), name=name)
            else:
                # 卖出: 执行时再查一次可卖量(T+1), 防御昨日信号后持仓变化
                from core.symbol_utils import is_t0_etf
                sellable = self.broker.sellable_qty(symbol, t0=is_t0_etf(symbol))
                qty = min(qty, sellable)
                if qty <= 0 or self.broker.positions.get(symbol, {}).get("qty", 0) < qty:
                    pending_fills.remove(fill)
                    continue
                self.broker.sell(symbol, qty, price, fee, slip, d,
                                 fill["sig"].get("reason", ""), name=name)
            pending_fills.remove(fill)

    # ------------------------------------------------------------------
    def _fill_at_close(self, symbol: str, qty: int, side: str,
                       prices: Dict[str, float], d: date, sig: Dict[str, Any]):
        """
        止损成交: 用 T 日收盘价立即成交(模拟盘中触发市价止损单)。
        注意: 止损是紧急行动, 不等 T+1 开盘 —— 否则高波动标的一夜暴跌后
        止损价远低于触发价(这就是之前 -35% 才成交的原因)。
        约束: 止损卖出不能超过当日可卖数量(T+1)。
        """
        fill = prices.get(symbol, 0)
        if fill <= 0:
            return
        from core.symbol_utils import is_t0_etf
        sellable = self.broker.sellable_qty(symbol, t0=is_t0_etf(symbol))
        qty = min(qty, sellable)
        if qty <= 0:
            return
        price = fill - fill * self.slippage     # 卖出按市价+滑点
        fee = self._calc_fee(price * qty)
        name = self.name_map.get(symbol, "")
        if self.broker.positions.get(symbol, {}).get("qty", 0) >= qty:
            self.broker.sell(symbol, qty, price, fee, fill * self.slippage, d,
                             sig.get("reason", ""), name=name)

    # ------------------------------------------------------------------
    def _agent_signals(self, asof, d: date) -> Dict[str, str]:
        """
        Agent 模式: 关键节点调用研究工作流。
        规则: 先算规则轮动信号(含卖出), 对 BUY 信号用 Agent 首席结论确认
        (首席非 BUY_CANDIDATE 则跳过), SELL 信号保留(止损/轮动必须可执行)。
        无未来函数: 研究节点使用回测日 asof 数据切片, 不采集实时行情/最新新闻。
        """
        from strategies.rotation_executor import build_rotation_signal_fn
        signals: Dict[str, Any] = {}
        # 1. 规则轮动(含卖出)
        # 修复: 原实现传 {} 作为价格字典 —— 轮动/止损信号全部因 price<=0
        # 被跳过, Agent 模式回测只买不卖, 净值完全失真。改用与信号日一致的收盘价。
        close_prices = {s: (bs[-1]["close"] if bs else 0) for s, bs in asof.items()}
        base = build_rotation_signal_fn(initial_cash=self.broker.cash + self.broker.position_value(close_prices),
                                        params=self.params)(asof, close_prices, d, self.broker)
        for sym, sig in base.items():
            if sig["action"] == "SELL":
                signals[sym] = sig
        # 2. 对 BUY 候选调用 Agent 确认(asof 数据注入, 杜绝未来数据)
        from workflows.research_workflow import run_research
        for symbol, bars in asof.items():
            if not bars:
                continue
            tech = compute_technical_features(bars)
            if not (tech.get("momentum_20d", 0) > 0.03 and tech.get("price_above_ma20")):
                continue
            last = bars[-1]
            state = asyncio.run(run_research(
                symbol, asset_type="etf",
                asof_override={
                    "bars": bars,
                    "quote": {"symbol": symbol, "latest_price": last["close"],
                              "change_pct": tech.get("change_pct", 0)},
                    "order_book": {},
                    "money_flow": {},
                }))
            chief = state.get("chief") or {}
            if chief.get("research_decision") == "BUY_CANDIDATE":
                price = tech.get("close", 0)
                if price > 0:
                    qty = int(self.broker.cash * 0.2 / price // 100 * 100)
                    if qty > 0:
                        signals[symbol] = {"action": "BUY", "qty": qty,
                                           "reason": f"[Agent确认] {chief.get('upside_reason', '')[:120]}"}
        return signals

    # ------------------------------------------------------------------
    def run_minute(self, data_loader, signal_fn, interval_minutes: int = 5):
        """
        分钟回测: 09:30 初始化 → 每 interval 增量喂数据 → 撮合 → 收盘结算。
        data_loader.minute_bars(symbol, day) 按日提供分钟K。
        撮合: 信号在第 i 根收盘后生成, 第 i+1 根开盘价成交(杜绝"用收盘价成交自己信号"的乐观执行)。
        修复: 原实现用当前K线收盘价撮合(轻度未来函数)且无 T+1 处理。
        """
        repo.save_backtest_run({
            "run_id": self.run_id, "name": self.name,
            "start_date": self.start, "end_date": self.end,
            "mode": "minute", "status": "RUNNING",
            "config_json": {"interval_minutes": interval_minutes,
                            "slippage": self.slippage},
        })
        minute_slippage = float(get_settings().get(
            "trading_rules.slippage.minute_bar", 0.0005))
        window: Dict[str, List[dict]] = {}
        prices: Dict[str, float] = {}
        for d in data_loader.trade_dates(self.start, self.end):
            freq = f"{interval_minutes}m"
            bars_by_symbol = {s: data_loader.load_all_minute(s, d, freq)
                              for s in data_loader.universe()}
            # T+1 解锁: 昨日及以前买入的今日可卖
            self.broker.start_new_day(d)
            n_bars = max((len(b) for b in bars_by_symbol.values()), default=0)
            if n_bars == 0:
                # 修复: 修复: 全部标的当日无分钟数据(停牌/数据缺口)——
                # 原实现引用未定义变量 prices/window 直接 NameError 崩溃
                logger.warning("分钟回测 %s 无数据, 跳过", d)
                continue
            for i in range(n_bars):
                # 增量窗口: 只喂 0..i(未来K线绝不可见)
                window = {s: bs[:i + 1] for s, bs in bars_by_symbol.items()}
                prices = {s: (bs[-1]["close"] if bs else 0) for s, bs in window.items()}
                signals = signal_fn(window, prices, d, self.broker) if self._signal_accepts_broker(signal_fn) else signal_fn(window, prices, d)
                for symbol, sig in signals.items():
                    action = sig.get("action")
                    qty = int(sig.get("qty", 0) or 0)
                    if action == "BUY" and qty > 0:
                        if self.broker.can_buy(symbol, qty, prices.get(symbol, 0)):
                            self._fill_next_minute_bar(symbol, qty, "BUY",
                                                       bars_by_symbol, i, d, sig,
                                                       minute_slippage)
                        else:
                            self.broker.skipped_buys += 1
                    elif action == "SELL" and qty > 0:
                        from core.symbol_utils import is_t0_etf
                        sellable = self.broker.sellable_qty(symbol, t0=is_t0_etf(symbol))
                        qty = min(qty, sellable)
                        if qty <= 0:
                            continue
                        self._fill_next_minute_bar(symbol, qty, "SELL",
                                                   bars_by_symbol, i, d,
                                                   {**sig, "qty": qty},
                                                   minute_slippage)
            self.equity_curve.append(self.broker.equity(prices))
            self.equity_dates.append(str(d))
            self.position_curve.append(self.broker.position_value(prices))
        self.broker.close_positions(prices, d, fee_fn=self._calc_fee)
        self.position_curve.append(0.0)
        return self._finalize()

    @staticmethod
    def _signal_accepts_broker(signal_fn) -> bool:
        import inspect
        try:
            return len(inspect.signature(signal_fn).parameters) >= 4
        except (TypeError, ValueError):
            return False

    def _fill_next_minute_bar(self, symbol, qty, side, bars_by_symbol, i, d, sig,
                              minute_slippage):
        """分钟撮合: 信号在第 i 根收盘后生成, 第 i+1 根开盘价±滑点成交。"""
        bars = bars_by_symbol.get(symbol) or []
        if i + 1 >= len(bars):
            return
        nxt = bars[i + 1]
        fill = nxt["open"]
        slip = fill * minute_slippage
        price = fill + slip if side == "BUY" else fill - slip
        fee = self._calc_fee(price * qty)
        name = self.name_map.get(symbol, "")
        bar_time = nxt["bar_time"]
        if side == "BUY":
            # 成交价级资金重校验(跳空高开截断数量)
            if not self.broker.can_buy(symbol, qty, price):
                max_qty = int(self.broker.cash / (price * 1.001) // 100 * 100)
                if max_qty < 100:
                    self.broker.skipped_buys += 1
                    return
                qty = max_qty
                fee = self._calc_fee(price * qty)
            self.broker.buy(symbol, qty, price, fee, slip, bar_time.date(),
                            sig.get("reason", ""), name=name)
        elif self.broker.positions.get(symbol, {}).get("qty", 0) >= qty:
            self.broker.sell(symbol, qty, price, fee, slip, bar_time.date(),
                             sig.get("reason", ""), name=name)

    # ------------------------------------------------------------------
    def _finalize(self) -> Dict[str, Any]:
        """汇总指标并落库。"""
        metrics = compute_metrics(self.equity_curve, self.broker.trades,
                                  self.benchmark_curve or None,
                                  dates=self.equity_dates)
        # 附加: 持仓市值曲线/参数/名称映射/跳过统计
        metrics["position_curve"] = [round(v, 2) for v in self.position_curve]
        metrics["params"] = {
            "mode": self.mode,
            "start": str(self.start), "end": str(self.end),
            "initial_cash": round(self.initial_cash, 2),
            "use_agents": self.use_agents,
            "slippage": self.slippage,
            **self.params,
        }
        metrics["skipped_buys"] = self.broker.skipped_buys
        # 单标的收益统计(修复: 轮动换仓后看不出每只标的的贡献)
        from collections import defaultdict
        sym_stats = defaultdict(lambda: {"symbol": "", "name": "", "buy_count": 0,
                                         "sell_count": 0, "buy_amount": 0.0,
                                         "realized_pnl": 0.0, "wins": 0, "losses": 0})
        for t in self.broker.trades:
            st = sym_stats[t.get("symbol")]
            st["symbol"] = t.get("symbol", "")
            st["name"] = t.get("name", "") or st["name"]
            if t.get("side") == "BUY":
                st["buy_count"] += 1
                st["buy_amount"] += float(t.get("amount", 0) or 0)
            else:
                st["sell_count"] += 1
                pnl = float(t.get("pnl", 0) or 0)
                st["realized_pnl"] += pnl
                if pnl > 0:
                    st["wins"] += 1
                elif pnl < 0:
                    st["losses"] += 1
        stats_list = []
        for st in sym_stats.values():
            closed = st["wins"] + st["losses"]
            stats_list.append({
                "symbol": st["symbol"], "name": st["name"],
                "buy_count": st["buy_count"], "sell_count": st["sell_count"],
                "buy_amount": round(st["buy_amount"], 2),
                "realized_pnl": round(st["realized_pnl"], 2),
                "wins": st["wins"], "losses": st["losses"],
                "win_rate": round(st["wins"] / closed, 4) if closed else 0.0,
            })
        stats_list.sort(key=lambda x: x["realized_pnl"], reverse=True)
        metrics["symbol_stats"] = stats_list
        # 单标的提示(轮动需要多标的)
        traded = {t.get("symbol") for t in self.broker.trades}
        if len(traded) <= 1 and self.broker.trades:
            metrics["note"] = "回测仅涉及 1 个标的, 轮动策略需要至少 2-3 只标的才有换仓效果。"
        elif len(traded) == 0:
            metrics["note"] = "回测期内无任何成交(可能所有标的都被参数过滤, 请检查成交额/波动率参数)。"
        repo.save_backtest_result({
            "run_id": self.run_id,
            "total_return": metrics.get("total_return", 0),
            "annual_return": metrics.get("annual_return", 0),
            "max_drawdown": metrics.get("max_drawdown", 0),
            "sharpe": metrics.get("sharpe", 0),
            "calmar": metrics.get("calmar", 0),
            "win_rate": metrics.get("win_rate", 0) or 0,
            "metrics_json": metrics,
        })
        repo.update_backtest_run(self.run_id, "DONE")
        metrics["run_id"] = self.run_id
        logger.info("回测完成 %s: 总收益 %.2f%% 回撤 %.2f%% 夏普 %.2f 交易 %d 笔",
                    self.run_id, metrics.get("total_return", 0) * 100,
                    metrics.get("max_drawdown", 0) * 100, metrics.get("sharpe", 0),
                    metrics.get("trade_count", 0))
        return metrics
