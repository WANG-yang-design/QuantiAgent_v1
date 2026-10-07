# -*- coding: utf-8 -*-
"""
模拟账户系统
============
字段: 总资产/可用资金/冻结资金/证券市值/总盈亏/当日盈亏/累计手续费/持仓/委托/成交
支持 A 股 T+1: 今日买入不可卖, 昨日及以前可卖。
账户状态持久化到 accounts/positions/orders/trades 表。
"""
import logging
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from database import repository as repo
from database.models import Account, Position
from core.timeutil import today as business_today

logger = logging.getLogger("paper.account")


class PaperAccount:
    """模拟账户(线程安全)。"""

    def __init__(self, account_id: str = "PA-001"):
        self.account_id = account_id
        self._lock = threading.RLock()
        self._account: Optional[Account] = None
        self._positions: Dict[str, Position] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self):
        """从数据库加载账户与持仓(进程内缓存)。"""
        acc = repo.get_account(self.account_id)
        if acc is None:
            from core.config import get_settings
            cash = float(get_settings().get("paper_account.initial_cash", 100000))
            acc = Account(account_id=self.account_id, account_type="paper",
                          cash=cash, frozen_cash=0.0, market_value=0.0,
                          total_asset=cash, total_pnl=0.0, day_pnl=0.0,
                          total_fee=0.0, init_cash=cash)
            repo.save_account(acc)
            acc = repo.get_account(self.account_id) or acc
        # 防御: 历史数据可能为 NULL 的数值字段
        for f in ("cash", "frozen_cash", "market_value", "total_asset",
                  "total_pnl", "day_pnl", "total_fee", "init_cash"):
            if getattr(acc, f) is None:
                setattr(acc, f, 0.0)
        self._account = acc
        for p in repo.get_positions(self.account_id):
            self._positions[p.symbol] = p

    def _persist(self):
        repo.save_account(self._account)

    # ------------------------------------------------------------------
    # 跨进程状态同步(修复): 调度器与 Web 可能各持一个 broker 实例(独立进程),
    # 本进程内存里的账户/持仓缓存会落后于 DB(另一进程撮合后已落库),
    # 表现为"最近成交有变化, 但持仓明细/账户数字不动"。
    # 每次读取前节流地从 DB 重载, DB 是唯一事实来源。
    # ------------------------------------------------------------------
    def _sync_from_db(self, force: bool = False):
        now = time.time()
        if not force and now - getattr(self, "_last_sync_ts", 0.0) < 2.0:
            return
        self._last_sync_ts = now
        try:
            with self._lock:
                acc = repo.get_account(self.account_id)
                if acc is not None:
                    self._account = acc
                self._positions = {
                    p.symbol: p for p in repo.get_positions(self.account_id)
                }
        except Exception as exc:
            logger.warning("账户状态同步失败(继续使用内存缓存): %s", exc)

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------
    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._sync_from_db()
            self._ensure_t1_unlock()
            self._refresh_market_value()
            a = self._account
            return {
                "account_id": a.account_id,
                "cash": round(a.cash, 2),
                "frozen_cash": round(a.frozen_cash, 2),
                "market_value": round(a.market_value, 2),
                "total_asset": round(a.total_asset, 2),
                "total_pnl": round(a.total_pnl, 2),
                "day_pnl": round(a.day_pnl, 2),
                "total_fee": round(a.total_fee, 2),
                "init_cash": round(float(a.init_cash or 0), 2),
                "total_return": round(a.total_pnl / a.init_cash, 4) if a.init_cash else 0,
                "status": a.status,
                "update_time": str(a.update_time),
                "positions": self.get_positions_locked(),
            }

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._sync_from_db()
            return self.get_positions_locked()

    def get_positions_locked(self) -> List[Dict[str, Any]]:
        """调用方必须已持有 self._lock。"""
        self._ensure_t1_unlock()
        _, day_pnl_by_symbol = self._compute_day_pnl_breakdown()
        out = []
        for p in self._positions.values():
            out.append({
                "symbol": p.symbol, "name": p.name,
                "total_qty": p.total_qty,
                "available_qty": p.available_qty,
                "frozen_qty": p.frozen_qty,
                "today_buy_qty": p.today_buy_qty,
                "cost_price": round(p.cost_price, 4),
                "latest_price": round(p.latest_price, 4),
                "peak_price": round(float(p.peak_price or 0), 4),
                "market_value": round(p.market_value, 2),
                "pnl": round(p.pnl, 2),
                "pnl_pct": round(p.pnl_pct, 4),
                "day_pnl": round(day_pnl_by_symbol.get(p.symbol, 0.0), 2),
                "buy_date": str(p.buy_date) if p.buy_date else "",
            })
        return out

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._sync_from_db()
            self._ensure_t1_unlock()
            p = self._positions.get(symbol)
            if p is None:
                return None
            _, day_pnl_by_symbol = self._compute_day_pnl_breakdown()
            return {
                "symbol": p.symbol, "name": p.name,
                "total_qty": p.total_qty, "available_qty": p.available_qty,
                "frozen_qty": p.frozen_qty, "today_buy_qty": p.today_buy_qty,
                "cost_price": round(p.cost_price, 4),
                "latest_price": round(p.latest_price, 4),
                "peak_price": round(float(p.peak_price or 0), 4),
                "market_value": round(p.market_value, 2),
                "pnl": round(p.pnl, 2), "pnl_pct": round(p.pnl_pct, 4),
                "day_pnl": round(day_pnl_by_symbol.get(p.symbol, 0.0), 2),
            }

    def get_available_cash(self) -> float:
        with self._lock:
            self._sync_from_db()
            return self._account.cash

    def get_available_qty(self, symbol: str) -> int:
        with self._lock:
            self._sync_from_db()
            self._ensure_t1_unlock()
            p = self._positions.get(symbol)
            return p.available_qty if p else 0

    def get_position_cost(self, symbol: str) -> Optional[float]:
        """持仓摊薄成本(卖出时计算已实现盈亏用)。"""
        with self._lock:
            self._sync_from_db()
            p = self._positions.get(symbol)
            return float(p.cost_price or 0) if p else None

    # ------------------------------------------------------------------
    # 内部资金/持仓变更(由 OrderManager 撮合后调用)
    # ------------------------------------------------------------------
    def _compute_day_pnl_breakdown(self) -> tuple[float, Dict[str, float]]:
        """
        当日盈亏(券商口径): 当前持仓市值 + 今日现金净流入 - 日初持仓市值。

        同时返回逐标的贡献，供持仓页显示。今日买入标的的日初数量为零，
        因此其当日盈亏自然等于当前市值减去今日买入成本和手续费；部分卖出
        标的则包含当日卖出现金流。完全清仓标的仍计入账户合计，但不会出现在
        当前持仓明细中。

            其中:
              今日现金净流入 = Σ今日SELL(价×量-费) - Σ今日BUY(价×量+费)
              日初持仓量     = 当前持仓量 + 今日卖出 - 今日买入
              日初市值       = Σ 日初持仓量 × 昨收价
        原实现用"进程内当日首次读取时的总盈亏"当基准 —— 服务重启/首次读取时间
        不定, 基准漂移, 当日盈亏严重失真(卖出后基准已含卖出, 数字对不上)。
        新口径与券商一致, 不依赖内存状态, 重启后依然正确。
        """
        today = business_today()
        start_dt = datetime.combine(today, datetime.min.time())
        try:
            trades = repo.get_trades(start=start_dt, account_id=self.account_id)
        except Exception:
            trades = []
        sold: Dict[str, int] = defaultdict(int)
        bought: Dict[str, int] = defaultdict(int)
        cash_flow: Dict[str, float] = defaultdict(float)
        for t in trades:
            if t.side == "SELL":
                sold[t.symbol] += int(t.qty or 0)
                cash_flow[t.symbol] += (
                    float(t.price or 0) * (t.qty or 0) - (t.fee or 0))
            else:
                bought[t.symbol] += int(t.qty or 0)
                cash_flow[t.symbol] -= (
                    float(t.price or 0) * (t.qty or 0) + (t.fee or 0))
        prev: Dict[str, float] = {}
        syms = set(self._positions.keys()) | set(sold) | set(bought)
        # 昨收优先取实时行情(腾讯/新浪均带昨收, 盘后依然有效), 再回退日K库。
        # 修复: 原实现只查日K库, 数据源缺失时回退"现价"→ 日初市值=当前市值,
        # 当日盈亏恒为 0.00(用户反馈"当日盈亏没数据")。
        try:
            from data_service.live_quote_service import get_live_quote_service
            quotes = get_live_quote_service().get_quotes(list(syms), max_age=600.0)
            for sym, q in quotes.items():
                pc = float((q or {}).get("prev_close", 0) or 0)
                if pc > 0:
                    prev[sym] = pc
        except Exception as exc:
            logger.debug("昨收批量获取失败(回退日K库): %s", exc)
        for sym in syms - set(prev.keys()):
            prev[sym] = self._prev_close(sym, today)
        by_symbol: Dict[str, float] = {}
        for sym in syms:
            cur = self._positions.get(sym)
            qty = (int(cur.total_qty or 0) if cur else 0) \
                + sold.get(sym, 0) - bought.get(sym, 0)
            day_start_mv = max(qty, 0) * float(prev.get(sym, 0) or 0)
            current_mv = (int(cur.total_qty or 0) * float(cur.latest_price or 0)
                          if cur else 0.0)
            by_symbol[sym] = round(
                current_mv + cash_flow.get(sym, 0.0) - day_start_mv, 4)
        return round(sum(by_symbol.values()), 4), by_symbol

    def _compute_day_pnl(self) -> float:
        total, _ = self._compute_day_pnl_breakdown()
        return total

    def _prev_close(self, symbol: str, day: date) -> float:
        """最近一个交易日的收盘价(当日盈亏昨收基准)。无K线时回退成本价/现价。"""
        try:
            rows = repo.get_daily_bars(
                symbol, day - timedelta(days=15), day - timedelta(days=1))
            if rows:
                return float(rows[-1].close or 0)
        except Exception:
            pass
        p = self._positions.get(symbol)
        if p:
            return float(p.latest_price or p.cost_price or 0)
        return 0.0

    def _refresh_market_value(self):
        """按最新价重算市值(价格由外部注入或沿用上次)。"""
        total_mv = 0.0
        total_pnl = 0.0
        for p in self._positions.values():
            p.market_value = p.total_qty * (p.latest_price or 0)
            p.pnl = p.market_value - p.total_qty * (p.cost_price or 0)
            p.pnl_pct = (p.latest_price / p.cost_price - 1) if p.cost_price else 0
            if p.latest_price > (p.peak_price or 0):
                p.peak_price = p.latest_price
            total_mv += p.market_value
            total_pnl += p.pnl
        self._account.market_value = total_mv
        self._account.cash = float(self._account.cash or 0)
        self._account.frozen_cash = float(self._account.frozen_cash or 0)
        self._account.total_asset = self._account.cash + self._account.frozen_cash + total_mv
        # 修复: 累计盈亏 = 总资产 - 初始资金(含已实现+浮动, 与券商一致)
        init = float(self._account.init_cash or 0)
        self._account.total_pnl = self._account.total_asset - init
        # 当日盈亏 = 券商口径(昨收+今日成交), 供单日亏损熔断与界面展示
        self._account.day_pnl = self._compute_day_pnl()

    def update_prices(self, prices: Dict[str, float]):
        """行情更新时刷新价格并重算市值, 同时更新持仓峰值(移动止盈基准)。
        修复: 价格刷新后必须落库 —— 原实现只 save_account, 持仓表的
        latest_price/market_value/pnl 从不更新, 服务器上"持仓明细"永远显示
        买入价(现价==成本价、浮盈亏+0.00), 与详情页的实时行情对不上。
        调用方(Web读取刷新/调度器快照/风控巡检)均已限流, 此处每次直接落库,
        避免 _sync_from_db(2s) 用旧DB值覆盖内存新价。"""
        with self._lock:
            repo.refresh_account_prices(self.account_id, prices)
            self._sync_from_db(force=True)

    def _ensure_t1_unlock(self, today: Optional[date] = None):
        """
        T+1 解锁: 昨日及以前买入的数量转入可用(available_qty)。
        判断依据是 buy_date(最近买入日): buy_date < today 则整个 today_buy 均为
        历史买入 → 解锁。幂等且无需持久化状态, 重启后依然正确。
        """
        today = today or business_today()
        if repo.unlock_t1_positions(self.account_id, today):
            self._sync_from_db(force=True)

    def apply_trade(self, symbol: str, name: str, side: str, price: float,
                    qty: int, fee: float, trade_time: datetime):
        """
        成交后更新持仓与资金:
        - BUY:  减现金、加持仓; T+1 品种今日买入不可卖, T+0 品种(跨境/债券/黄金)当日即可卖
        - SELL: 加现金、减持仓与可用量
        - 成本: 摊薄成本
        """
        from core.symbol_utils import is_t0_etf
        with self._lock:
            a = self._account
            t0 = is_t0_etf(symbol)
            if side == "BUY":
                self._ensure_t1_unlock(trade_time.date())
                a.cash -= price * qty + fee
                # 注意: 冻结资金由 OrderManager 撮合后精确释放(unfreeze_cash),
                # 此处不再重复扣减 —— 原实现双重扣减导致 frozen_cash 失真(资金滞留)
                p = self._positions.get(symbol)
                if p is None:
                    p = Position(position_id=f"POS-{self.account_id}-{symbol}",
                                 account_id=self.account_id,
                                 symbol=symbol, name=name, total_qty=0,
                                 available_qty=0, today_buy_qty=0, frozen_qty=0,
                                 market_value=0.0, pnl=0.0, pnl_pct=0.0,
                                 cost_price=price, latest_price=price,
                                 peak_price=price)
                    self._positions[symbol] = p
                # 摊薄成本 = (旧持仓市值 + 本次买入金额) / 新总数量
                old_cost_value = p.total_qty * p.cost_price
                p.total_qty += qty
                if t0:
                    p.available_qty += qty            # T+0: 当日即可卖
                else:
                    p.today_buy_qty += qty            # T+1: 今日买入不可卖
                p.cost_price = (old_cost_value + price * qty) / p.total_qty if p.total_qty else price
                p.latest_price = price
                p.peak_price = max(p.peak_price or price, price)   # 持仓最高价(移动止盈基准)
                p.buy_date = trade_time.date()
                repo.save_position(p)
            else:  # SELL
                self._ensure_t1_unlock(trade_time.date())
                a.cash += price * qty - fee
                p = self._positions.get(symbol)
                if p is None:
                    raise ValueError(f"无持仓可卖: {symbol}")
                p.total_qty -= qty
                # 注意: available_qty 已在下单冻结时扣减(freeze_qty), 成交时不再重复扣
                # T+0 品种当日买入份额(在 available 中)被卖出时, 同步扣减今日买入计数
                if t0:
                    p.today_buy_qty = max(0, (p.today_buy_qty or 0) - qty)
                p.latest_price = price
                if p.total_qty <= 0:
                    del self._positions[symbol]
                    repo.delete_position(self.account_id, symbol)
                else:
                    # 修复: 部分卖出后持仓必须落库 —— 原实现只在"清仓"或
                    # "成本为0"时保存, 普通减仓(如 1300 卖 700)永远不写库,
                    # DB 持仓数量停留在卖出前, 账户总资产被虚增数倍。
                    if p.cost_price == 0:
                        p.cost_price = price
                    repo.save_position(p)
            a.total_fee += fee
            self._refresh_market_value()
            self._persist()

    def freeze_cash(self, amount: float):
        """下单时冻结资金(防止重复下单)。"""
        with self._lock:
            if amount > self._account.cash + 1e-6:
                raise ValueError(f"资金不足: 需{amount:.2f}, 可用{self._account.cash:.2f}")
            self._account.cash -= amount
            self._account.frozen_cash += amount
            self._persist()

    def unfreeze_cash(self, amount: float):
        """撤单/部分成交释放冻结资金。"""
        with self._lock:
            self._account.frozen_cash = max(0.0, self._account.frozen_cash - amount)
            self._account.cash += amount
            self._persist()

    def freeze_qty(self, symbol: str, qty: int):
        """卖出下单冻结可用股数。"""
        with self._lock:
            p = self._positions.get(symbol)
            if p is None or p.available_qty < qty:
                raise ValueError(f"可用持仓不足: {symbol} 可用{p.available_qty if p else 0}")
            p.available_qty -= qty
            p.frozen_qty += qty
            # 修复: 冻结必须落库 —— 多进程场景(web+调度器各自持broker)下
            # 原实现只改内存, 另一进程撮合成交时不知道已冻结,
            # 卖出只减 total_qty 不减可用量 → "总数量300 可用600"脏数据
            repo.save_position(p)

    def unfreeze_qty(self, symbol: str, qty: int):
        with self._lock:
            p = self._positions.get(symbol)
            if p:
                p.frozen_qty = max(0, p.frozen_qty - qty)
                p.available_qty += qty
                repo.save_position(p)

    def consume_frozen_on_fill(self, symbol: str, qty: int, side: str):
        """成交后扣减冻结(买: 资金; 卖: 股数)。"""
        with self._lock:
            if side == "SELL":
                p = self._positions.get(symbol)
                if p:
                    p.frozen_qty = max(0, p.frozen_qty - qty)
                    repo.save_position(p)

    # ------------------------------------------------------------------
    def snapshot_to_db(self):
        """收盘快照(账户净值曲线)。"""
        with self._lock:
            self._refresh_market_value()
            repo.save_account_snapshot({
                "account_id": self.account_id,
                "cash": self._account.cash,
                "market_value": self._account.market_value,
                "total_asset": self._account.total_asset,
                "pnl": self._account.total_pnl,
            })
            self._persist()
