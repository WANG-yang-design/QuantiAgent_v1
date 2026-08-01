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
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from database import repository as repo
from database.models import Account, Position

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
    # 只读视图
    # ------------------------------------------------------------------
    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
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
                "total_return": round(a.total_pnl / a.init_cash, 4) if a.init_cash else 0,
                "status": a.status,
                "update_time": str(a.update_time),
                "positions": self.get_positions(),
            }

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
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
                    "market_value": round(p.market_value, 2),
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": round(p.pnl_pct, 4),
                    "buy_date": str(p.buy_date) if p.buy_date else "",
                })
            return out

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            p = self._positions.get(symbol)
            if p is None:
                return None
            return {
                "symbol": p.symbol, "name": p.name,
                "total_qty": p.total_qty, "available_qty": p.available_qty,
                "frozen_qty": p.frozen_qty, "today_buy_qty": p.today_buy_qty,
                "cost_price": round(p.cost_price, 4),
                "latest_price": round(p.latest_price, 4),
                "market_value": round(p.market_value, 2),
                "pnl": round(p.pnl, 2), "pnl_pct": round(p.pnl_pct, 4),
            }

    def get_available_cash(self) -> float:
        with self._lock:
            return self._account.cash

    def get_available_qty(self, symbol: str) -> int:
        with self._lock:
            p = self._positions.get(symbol)
            return p.available_qty if p else 0

    # ------------------------------------------------------------------
    # 内部资金/持仓变更(由 OrderManager 撮合后调用)
    # ------------------------------------------------------------------
    def _refresh_market_value(self):
        """按最新价重算市值(价格由外部注入或沿用上次)。"""
        total_mv = 0.0
        total_pnl = 0.0
        for p in self._positions.values():
            p.market_value = p.total_qty * (p.latest_price or 0)
            p.pnl = p.market_value - p.total_qty * (p.cost_price or 0)
            p.pnl_pct = (p.latest_price / p.cost_price - 1) if p.cost_price else 0
            total_mv += p.market_value
            total_pnl += p.pnl
        self._account.market_value = total_mv
        self._account.cash = float(self._account.cash or 0)
        self._account.frozen_cash = float(self._account.frozen_cash or 0)
        self._account.total_asset = self._account.cash + self._account.frozen_cash + total_mv
        self._account.total_pnl = total_pnl

    def update_prices(self, prices: Dict[str, float]):
        """行情更新时刷新价格并重算市值。"""
        with self._lock:
            for symbol, price in prices.items():
                p = self._positions.get(symbol)
                if p and price > 0:
                    p.latest_price = price
            self._refresh_market_value()
            self._persist()

    def apply_trade(self, symbol: str, name: str, side: str, price: float,
                    qty: int, fee: float, trade_time: datetime):
        """
        成交后更新持仓与资金 (T+1 规则在此生效):
        - BUY:  减现金、加持仓, 今日买入量+ (不可卖)
        - SELL: 加现金、减持仓与可用量
        - 成本: 摊薄成本
        """
        with self._lock:
            a = self._account
            if side == "BUY":
                a.cash -= price * qty + fee
                a.frozen_cash = max(0.0, a.frozen_cash - (price * qty + fee) * (qty > 0))
                p = self._positions.get(symbol)
                if p is None:
                    p = Position(position_id=f"POS-{self.account_id}-{symbol}",
                                 account_id=self.account_id,
                                 symbol=symbol, name=name, total_qty=0,
                                 available_qty=0, today_buy_qty=0, frozen_qty=0,
                                 market_value=0.0, pnl=0.0, pnl_pct=0.0,
                                 cost_price=price, latest_price=price)
                    self._positions[symbol] = p
                # 摊薄成本 = (旧持仓市值 + 本次买入金额) / 新总数量
                old_cost_value = p.total_qty * p.cost_price
                p.total_qty += qty
                p.today_buy_qty += qty
                p.cost_price = (old_cost_value + price * qty) / p.total_qty if p.total_qty else price
                p.latest_price = price
                p.buy_date = trade_time.date()
                repo.save_position(p)
            else:  # SELL
                a.cash += price * qty - fee
                p = self._positions.get(symbol)
                if p is None:
                    raise ValueError(f"无持仓可卖: {symbol}")
                p.total_qty -= qty
                p.available_qty -= qty
                # 优先扣减今日买入(T+1: 今日买入部分不会出现在可卖数量中, 防御性处理)
                p.today_buy_qty = max(0, (p.today_buy_qty or 0) - qty)
                p.latest_price = price
                if p.total_qty <= 0:
                    del self._positions[symbol]
                    repo.delete_position(self.account_id, symbol)
                elif p.total_qty > 0 and p.cost_price == 0:
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
            self._persist()

    def unfreeze_qty(self, symbol: str, qty: int):
        with self._lock:
            p = self._positions.get(symbol)
            if p:
                p.frozen_qty = max(0, p.frozen_qty - qty)
                p.available_qty += qty
                self._persist()

    def consume_frozen_on_fill(self, symbol: str, qty: int, side: str):
        """成交后扣减冻结(买: 资金; 卖: 股数)。"""
        with self._lock:
            if side == "SELL":
                p = self._positions.get(symbol)
                if p:
                    p.frozen_qty = max(0, p.frozen_qty - qty)
                    self._persist()

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
