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

    def equity(self, prices: Dict[str, float]) -> float:
        mv = sum(p["qty"] * prices.get(s, p["cost"]) for s, p in self.positions.items())
        return self.cash + mv

    def can_buy(self, symbol: str, qty: int, price: float) -> bool:
        return self.cash >= qty * price * 1.001

    def buy(self, symbol: str, qty: int, price: float, fee: float, slippage: float,
            date_: date, reason: str = ""):
        cost = qty * price + fee
        self.cash -= cost
        p = self.positions.setdefault(symbol, {"qty": 0, "cost": 0.0, "avail": 0,
                                               "buy_date": date_, "name": ""})
        old_cost = p["qty"] * p["cost"]
        p["qty"] += qty
        p["cost"] = (old_cost + qty * price) / p["qty"]
        p["avail"] += qty
        self.total_fee += fee
        self.total_slippage += slippage
        self.trades.append({"symbol": symbol, "side": "BUY", "qty": qty, "price": price,
                            "fee": fee, "slippage_cost": slippage, "date": date_,
                            "reason": reason, "pnl": 0, "hold_days": 0})

    def sell(self, symbol: str, qty: int, price: float, fee: float, slippage: float,
             date_: date, reason: str = ""):
        p = self.positions[symbol]
        p["qty"] -= qty
        p["avail"] -= qty
        pnl = (price - p["cost"]) * qty - fee
        self.cash += qty * price - fee
        self.total_fee += fee
        self.total_slippage += slippage
        self.trades.append({"symbol": symbol, "side": "SELL", "qty": qty, "price": price,
                            "fee": fee, "slippage_cost": slippage, "date": date_,
                            "reason": reason, "pnl": pnl,
                            "hold_days": (date_ - p["buy_date"]).days
                            if isinstance(p["buy_date"], date) else 0})
        if p["qty"] <= 0:
            del self.positions[symbol]

    def close_positions(self, prices: Dict[str, float], date_: date, fee_rate: float = 0.0005):
        """回测结束强制平仓(统计用)。"""
        for symbol in list(self.positions.keys()):
            p = self.positions[symbol]
            if p["qty"] > 0 and prices.get(symbol, 0) > 0:
                price = prices[symbol]
                fee = price * p["qty"] * fee_rate
                self.sell(symbol, p["qty"], price, fee, 0.0, date_, "期末平仓")


class BacktestEngine:
    """回测引擎: 日线/分钟双模式。"""

    def __init__(self, start: date, end: date, initial_cash: float = 100000.0,
                 slippage: Optional[float] = None, mode: str = "daily",
                 use_agents: bool = False, agent_interval_days: int = 5,
                 name: str = ""):
        self.start = start
        self.end = end
        self.mode = mode
        self.use_agents = use_agents
        self.agent_interval_days = agent_interval_days
        self.name = name or f"回测{start}-{end}"
        rules = get_settings().section("trading_rules")
        self.slippage = slippage if slippage is not None else float(
            rules.get("slippage", {}).get("daily_bar", 0.001))
        self.fee_rate = float(rules.get("fees", {}).get("commission_rate", 0.00025))
        self.fee_min = float(rules.get("fees", {}).get("commission_min", 5.0))
        self.broker = BacktestBroker(initial_cash)
        self.run_id = gen_backtest_id()
        self.equity_curve: List[float] = [initial_cash]
        self.equity_dates: List[str] = [str(start)]
        self.benchmark_curve: List[float] = []

    # ------------------------------------------------------------------
    def _calc_fee(self, amount: float) -> float:
        return max(amount * self.fee_rate, self.fee_min)

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
        # 预加载全部K线(但只按 asof 截取, 杜绝未来函数)
        bars_by_symbol = {s: data_loader.load_all_daily(s, self.start, self.end)
                          for s in data_loader.universe()}

        for i, d in enumerate(trade_dates):
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

            # ---- 次日撮合(计划在T日生成, T+1开盘成交) ----
            for symbol, sig in signals.items():
                action = sig.get("action")
                qty = int(sig.get("qty", 0) or 0)
                if action == "BUY" and qty > 0 and self.broker.can_buy(symbol, qty, prices.get(symbol, 0)):
                    self._fill_next_open(symbol, qty, "BUY", bars_by_symbol, d, sig)
                elif action == "SELL" and qty > 0:
                    self._fill_next_open(symbol, qty, "SELL", bars_by_symbol, d, sig)

            # 持仓市值 + 净值
            self.broker.day_pnl = self.broker.equity(prices) - self.broker.prev_equity
            self.broker.prev_equity = self.broker.equity(prices)
            self.equity_curve.append(self.broker.equity(prices))
            self.equity_dates.append(str(d))

        # 期末: 平仓 + 基准曲线
        prices = {s: (bs[-1]["close"] if bs else 0) for s, bs in asof.items()}
        self.broker.close_positions(prices, trade_dates[-1])
        self.equity_curve.append(self.broker.equity(prices))
        self.equity_dates.append(str(trade_dates[-1]))

        bench = data_loader.load_benchmark(benchmark_symbol, self.start, self.end)
        if bench:
            b0 = bench[0]["close"]
            # 与净值曲线对齐: 初始点 + 每日收盘 + 期末点
            self.benchmark_curve = [100000.0] + \
                [b / b0 * 100000 for b in [x["close"] for x in bench]] + \
                [bench[-1]["close"] / b0 * 100000]
            if len(self.benchmark_curve) > len(self.equity_curve):
                self.benchmark_curve = self.benchmark_curve[:len(self.equity_curve)]

        return self._finalize()

    # ------------------------------------------------------------------
    def _fill_next_open(self, symbol: str, qty: int, side: str,
                        bars_by_symbol: Dict[str, List[dict]], d: date, sig: Dict[str, Any]):
        """
        T+1 开盘价 + 滑点成交(简单撮合)。
        注意: 用全量K线找 T+1 的bar只用于"成交价", 决策特征仍只用 ≤T 的数据(无未来函数)。
        """
        bars = bars_by_symbol.get(symbol) or []
        nxt = next((b for b in bars if b["trade_date"] > d), None)
        if nxt is None:
            return
        fill = nxt["open"]
        slip = fill * self.slippage
        price = fill + slip if side == "BUY" else fill - slip
        fee = self._calc_fee(price * qty)
        if side == "BUY":
            self.broker.buy(symbol, qty, price, fee, slip, nxt["trade_date"], sig.get("reason", ""))
        else:
            if self.broker.positions.get(symbol, {}).get("qty", 0) >= qty:
                self.broker.sell(symbol, qty, price, fee, slip, nxt["trade_date"], sig.get("reason", ""))

    # ------------------------------------------------------------------
    def _agent_signals(self, asof, d: date) -> Dict[str, str]:
        """Agent 模式: 在关键节点调用研究工作流(成本高, 按 agent_interval_days 节流)。"""
        from workflows.research_workflow import run_research
        signals: Dict[str, Any] = {}
        for symbol, bars in asof.items():
            if not bars:
                continue
            tech = compute_technical_features(bars)
            if tech.get("momentum_20d", 0) > 0.05 and tech.get("price_above_ma20"):
                # 进入 Agent 分析
                state = asyncio.run(run_research(symbol, asset_type="etf"))
                chief = state.get("chief") or {}
                price = tech.get("close", 0)
                if chief.get("research_decision") == "BUY_CANDIDATE":
                    qty = int(self.broker.cash * 0.2 / price // 100 * 100) if price else 0
                    if qty > 0:
                        signals[symbol] = {"action": "BUY", "qty": qty,
                                           "reason": chief.get("upside_reason", "")[:200]}
        return signals

    # ------------------------------------------------------------------
    def run_minute(self, data_loader, signal_fn, interval_minutes: int = 5):
        """
        分钟回测: 09:30 初始化 → 每 interval 增量喂数据 → 撮合 → 收盘结算。
        data_loader.minute_bars(symbol, day) 按日提供分钟K。
        """
        repo.save_backtest_run({
            "run_id": self.run_id, "name": self.name,
            "start_date": self.start, "end_date": self.end,
            "mode": "minute", "status": "RUNNING",
            "config_json": {"interval_minutes": interval_minutes,
                            "slippage": self.slippage},
        })
        self.slippage = float(get_settings().get(
            "trading_rules.slippage.minute_bar", 0.0005))
        for d in data_loader.trade_dates(self.start, self.end):
            freq = f"{interval_minutes}m"
            bars_by_symbol = {s: data_loader.load_all_minute(s, d, freq)
                              for s in data_loader.universe()}
            n_bars = max((len(b) for b in bars_by_symbol.values()), default=0)
            for i in range(n_bars):
                # 增量窗口: 只喂 0..i(未来K线绝不可见)
                window = {s: bs[:i + 1] for s, bs in bars_by_symbol.items()}
                prices = {s: (bs[-1]["close"] if bs else 0) for s, bs in window.items()}
                signals = signal_fn(window, prices, d)
                for symbol, sig in signals.items():
                    action = sig.get("action")
                    qty = int(sig.get("qty", 0) or 0)
                    if action == "BUY" and qty > 0 and self.broker.can_buy(symbol, qty, prices.get(symbol, 0)):
                        self._fill_current_bar(symbol, qty, "BUY", window, sig)
                    elif action == "SELL" and qty > 0:
                        self._fill_current_bar(symbol, qty, "SELL", window, sig)
            self.equity_curve.append(self.broker.equity(prices))
            self.equity_dates.append(str(d))
        prices = {s: (bs[-1]["close"] if bs else 0) for s, bs in window.items()}
        self.broker.close_positions(prices, d)
        return self._finalize()

    def _fill_current_bar(self, symbol, qty, side, window, sig):
        """分钟撮合: 当前(最新可见)K线收盘±滑点(分钟滑点0.05%)。"""
        bars = window.get(symbol) or []
        if not bars:
            return
        bar = bars[-1]
        fill = bar["close"]
        slip = fill * self.slippage
        price = fill + slip if side == "BUY" else fill - slip
        fee = self._calc_fee(price * qty)
        if side == "BUY":
            self.broker.buy(symbol, qty, price, fee, slip, bar["bar_time"].date(),
                            sig.get("reason", ""))
        elif self.broker.positions.get(symbol, {}).get("qty", 0) >= qty:
            self.broker.sell(symbol, qty, price, fee, slip, bar["bar_time"].date(),
                             sig.get("reason", ""))

    # ------------------------------------------------------------------
    def _finalize(self) -> Dict[str, Any]:
        """汇总指标并落库。"""
        metrics = compute_metrics(self.equity_curve, self.broker.trades,
                                  self.benchmark_curve or None)
        repo.save_backtest_result({
            "run_id": self.run_id,
            "total_return": metrics.get("total_return", 0),
            "annual_return": metrics.get("annual_return", 0),
            "max_drawdown": metrics.get("max_drawdown", 0),
            "sharpe": metrics.get("sharpe", 0),
            "calmar": metrics.get("calmar", 0),
            "win_rate": metrics.get("win_rate", 0),
            "metrics_json": metrics,
        })
        repo.update_backtest_run(self.run_id, "DONE")
        metrics["run_id"] = self.run_id
        logger.info("回测完成 %s: 总收益 %.2f%% 回撤 %.2f%% 夏普 %.2f",
                    self.run_id, metrics.get("total_return", 0) * 100,
                    metrics.get("max_drawdown", 0) * 100, metrics.get("sharpe", 0))
        return metrics
