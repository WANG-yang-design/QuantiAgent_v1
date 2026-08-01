# -*- coding: utf-8 -*-
"""
订单管理系统 + 模拟撮合
=======================
订单状态机:
CREATED → RISK_CHECKED → SUBMITTED → ACCEPTED → PARTIALLY_FILLED → FILLED
                        → CANCEL_PENDING → CANCELLED
                        → REJECTED / FAILED / UNKNOWN

防重复下单: order_intent_id 幂等, 同一意图只能提交一次。
撮合档位(文档 11.3):
  简单: 按下一根K线开盘价成交
  中级: 限价单按下一根K线高低价判断是否成交
  高级: 预留(结合盘口/成交概率)
"""
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from core.ids import gen_order_id
from core.config import get_settings
from database import repository as repo
from paper_trading.paper_account import PaperAccount

logger = logging.getLogger("paper.order")

# 订单状态常量
ST_CREATED = "CREATED"
ST_RISK_CHECKED = "RISK_CHECKED"
ST_SUBMITTED = "SUBMITTED"
ST_ACCEPTED = "ACCEPTED"
ST_PARTIAL = "PARTIALLY_FILLED"
ST_FILLED = "FILLED"
ST_CANCEL_PENDING = "CANCEL_PENDING"
ST_CANCELLED = "CANCELLED"
ST_REJECTED = "REJECTED"
ST_FAILED = "FAILED"
ST_UNKNOWN = "UNKNOWN"

_ACTIVE_STATES = {ST_SUBMITTED, ST_ACCEPTED, ST_PARTIAL}


class OrderManager:
    """订单管理 + 撮合。"""

    def __init__(self, account: Optional[PaperAccount] = None):
        self.account = account or PaperAccount()
        self._lock = threading.RLock()
        self._orders: Dict[str, dict] = {}
        self._load_orders()

    def _load_orders(self):
        for o in repo.get_open_orders(self.account.account_id):
            self._orders[o.order_id] = {
                "order_id": o.order_id, "order_intent_id": o.order_intent_id,
                "symbol": o.symbol, "side": o.side, "order_type": o.order_type,
                "price": o.price, "qty": o.qty, "filled_qty": o.filled_qty,
                "remaining_qty": o.remaining_qty, "status": o.status,
                "submit_time": o.submit_time, "plan_id": o.plan_id,
            }

    # ------------------------------------------------------------------
    # 提交订单 (幂等: 同一 order_intent_id 只能提交一次)
    # ------------------------------------------------------------------
    def submit_order(self, symbol: str, side: str, qty: int, order_type: str = "LIMIT",
                     price: float = 0.0, order_intent_id: Optional[str] = None,
                     plan_id: str = "", name: str = "") -> dict:
        with self._lock:
            # 幂等检查
            if order_intent_id:
                exist = repo.get_order_by_intent(order_intent_id)
                if exist is not None:
                    logger.warning("重复下单拦截: intent=%s 已存在订单 %s",
                                   order_intent_id, exist.order_id)
                    return self._to_view(exist)
            if qty <= 0:
                raise ValueError("下单数量必须 > 0")

            # 资金/持仓冻结
            frozen_amount = 0.0
            if side == "BUY":
                need = price * qty
                self.account.freeze_cash(need)
                frozen_amount = need
            else:
                avail = self.account.get_available_qty(symbol)
                if avail < qty:
                    raise ValueError(f"可用持仓不足: {symbol} 可用{avail}, 需{qty}")
                self.account.freeze_qty(symbol, qty)

            order = {
                "order_id": gen_order_id(),
                "order_intent_id": order_intent_id or gen_order_id(),
                "plan_id": plan_id,
                "account_id": self.account.account_id,
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "price": price,
                "qty": qty,
                "filled_qty": 0,
                "remaining_qty": qty,
                "avg_fill_price": 0.0,
                "status": ST_SUBMITTED,
                "submit_time": datetime.now(),
                "name": name,
                "source": "paper",
                "frozen_amount": frozen_amount,   # 下单时冻结的金额(成交时精确释放)
            }
            saved = repo.save_order({k: v for k, v in order.items()
                                     if k != "frozen_amount"})
            self._orders[saved.order_id] = self._to_view(saved)
            from memory.audit_log import AuditLogger
            AuditLogger.instance().log("order_submitted", "order_manager", {
                "order_id": saved.order_id, "symbol": symbol, "side": side,
                "qty": qty, "price": price, "intent": order["order_intent_id"],
            })
            # 内存订单保留 frozen_amount(不落库)
            view = self._to_view(saved)
            view["frozen_amount"] = frozen_amount
            self._orders[saved.order_id] = view
            return view

    # ------------------------------------------------------------------
    # 撮合: 按下一根K线成交 (回测与盘中通用)
    # ------------------------------------------------------------------
    def match_order(self, order_id: str, bar: Dict[str, Any],
                    slippage: float = 0.0005, mode: str = "simple") -> List[dict]:
        """
        用一根新K线撮合订单。
        - simple: 开盘价+滑点成交
        - medium: 限价单按 K线高低价判断(买: low<=限价; 卖: high>=限价)
        返回成交明细列表。
        """
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                order_row = repo.get_order(order_id)
                if order_row is None:
                    return []
                order = self._to_view(order_row)
            if order["status"] not in _ACTIVE_STATES:
                return []

            o = bar.get("open", 0)
            hi = bar.get("high", o)
            lo = bar.get("low", o)
            trades: List[dict] = []

            if mode == "medium" and order["order_type"] == "LIMIT":
                fillable = order["remaining_qty"]
                if order["side"] == "BUY" and lo <= order["price"]:
                    fill_price = min(order["price"], o)   # 开盘更低按开盘
                    fill_qty = fillable
                elif order["side"] == "SELL" and hi >= order["price"]:
                    fill_price = max(order["price"], o)
                    fill_qty = fillable
                else:
                    return []
            else:
                # simple: 开盘价 + 滑点
                slip = o * slippage
                fill_price = o + slip if order["side"] == "BUY" else o - slip
                fill_qty = order["remaining_qty"]

            fee = self._calc_fee(order["side"], fill_price, fill_qty)
            t = self._do_fill(order, fill_price, fill_qty, fee, bar)
            if t:
                trades.append(t)
            return trades

    def _do_fill(self, order: dict, price: float, qty: int, fee: float,
                 bar: Dict[str, Any]) -> Optional[dict]:
        """执行一笔成交并更新订单/账户。"""
        now = datetime.now()
        order["filled_qty"] += qty
        order["remaining_qty"] -= qty
        order["avg_fill_price"] = ((order["avg_fill_price"] *
                                    (order["filled_qty"] - qty)) + price * qty) / order["filled_qty"]
        # 释放冻结(只释放实际冻结部分, 避免资金凭空多出)
        if order["side"] == "BUY":
            frozen = float(order.get("frozen_amount", 0) or 0)
            # 按成交比例释放(部分成交时释放对应比例)
            release = frozen * qty / max(order["qty"], 1)
            self.account.unfreeze_cash(release)
        else:
            self.account.consume_frozen_on_fill(order["symbol"], qty, "SELL")
        # 更新持仓/资金
        self.account.apply_trade(order["symbol"], order.get("name", ""), order["side"],
                                 price, qty, fee, now)
        order["status"] = ST_FILLED if order["remaining_qty"] == 0 else ST_PARTIAL
        order["filled_time"] = now
        self._persist_order(order)
        trade = {
            "order_id": order["order_id"], "symbol": order["symbol"],
            "side": order["side"], "price": price, "qty": qty,
            "fee": fee, "trade_time": now,
        }
        repo.save_trade(trade)
        from memory.audit_log import AuditLogger
        AuditLogger.instance().log("trade_filled", "order_manager",
                                   {**trade, "plan_id": order.get("plan_id", "")})
        return trade

    # ------------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------------
    def cancel_order(self, order_id: str, reason: str = "manual") -> dict:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                row = repo.get_order(order_id)
                if row is None:
                    raise ValueError(f"订单不存在: {order_id}")
                order = self._to_view(row)
            if order["status"] not in _ACTIVE_STATES and order["status"] != ST_CREATED:
                raise ValueError(f"订单状态 {order['status']} 不可撤单")
            remaining = order["remaining_qty"]
            # 释放冻结(只释放未成交部分的冻结)
            if order["side"] == "BUY" and remaining > 0:
                frozen = float(order.get("frozen_amount", 0) or 0)
                release = frozen * remaining / max(order["qty"], 1)
                self.account.unfreeze_cash(release)
            elif order["side"] == "SELL" and remaining > 0:
                self.account.unfreeze_qty(order["symbol"], remaining)
            order["status"] = ST_CANCELLED
            order["cancel_time"] = datetime.now()
            self._persist_order(order)
            from memory.audit_log import AuditLogger
            AuditLogger.instance().log("order_cancelled", "order_manager",
                                       {"order_id": order_id, "reason": reason})
            return order

    def query_order(self, order_id: str) -> dict:
        row = repo.get_order(order_id)
        return self._to_view(row) if row else {}

    def list_orders(self, status: Optional[str] = None) -> List[dict]:
        """列出订单(全部/按状态)。"""
        from database import repository as repo2
        with self._lock:
            out = []
            for oid, order in self._orders.items():
                if status and order.get("status") != status:
                    continue
                out.append(order)
            # 补充历史订单(从库中取最新50条)
            if not status:
                rows = repo2.get_orders_recent(limit=50, account_id=self.account.account_id)
                known = {o["order_id"] for o in out}
                for r in rows:
                    if r.order_id not in known:
                        out.append(self._to_view(r))
            out.sort(key=lambda x: str(x.get("submit_time") or ""), reverse=True)
            return out

    # ------------------------------------------------------------------
    # 超时撤单(限价单未成交超过阈值)
    # ------------------------------------------------------------------
    def cancel_stale_orders(self, timeout_seconds: Optional[int] = None) -> List[dict]:
        if timeout_seconds is None:
            timeout_seconds = int(get_settings().get("trading_rules.cancel.unfilled_timeout_seconds", 300))
        cancelled = []
        for oid, order in list(self._orders.items()):
            if order["status"] in _ACTIVE_STATES and order.get("submit_time"):
                elapsed = (datetime.now() - order["submit_time"]).total_seconds()
                if elapsed > timeout_seconds:
                    try:
                        cancelled.append(self.cancel_order(oid, reason="timeout"))
                    except Exception as exc:
                        logger.warning("超时撤单失败 %s: %s", oid, exc)
        return cancelled

    # ------------------------------------------------------------------
    def _persist_order(self, order: dict):
        row = repo.get_order(order["order_id"])
        if row is None:
            repo.save_order(order)
        else:
            row.status = order["status"]
            row.filled_qty = order["filled_qty"]
            row.remaining_qty = order["remaining_qty"]
            row.avg_fill_price = order["avg_fill_price"]
            row.filled_time = order.get("filled_time")
            row.cancel_time = order.get("cancel_time")
            repo.update_order(row)

    @staticmethod
    def _to_view(o) -> dict:
        return {
            "order_id": o.order_id, "order_intent_id": o.order_intent_id,
            "plan_id": o.plan_id, "symbol": o.symbol, "side": o.side,
            "order_type": o.order_type, "price": o.price, "qty": o.qty,
            "filled_qty": o.filled_qty, "remaining_qty": o.remaining_qty,
            "avg_fill_price": o.avg_fill_price, "status": o.status,
            "submit_time": o.submit_time, "filled_time": o.filled_time,
            "cancel_time": o.cancel_time, "reject_reason": o.reject_reason,
            "fee": o.fee,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _calc_fee(side: str, price: float, qty: int, asset_type: str = "etf") -> float:
        """
        手续费(按资产类型区分):
        - ETF(场内基金): 佣金按实际费率, 无最低5元门槛, 免印花税
        - 股票: 佣金最低5元 + 卖出印花税万5
        """
        rules = get_settings().section("trading_rules")
        fees = rules.get("fees", {})
        amount = price * qty
        rate = float(fees.get("commission_rate", 0.00025))
        if asset_type == "etf":
            commission = max(amount * rate,
                             float(fees.get("commission_min_etf", 0.0)))
            tax = 0.0
        else:
            commission = max(amount * rate,
                             float(fees.get("commission_min", 5.0)))
            tax = amount * float(fees.get("stamp_tax_rate", 0.0005)) if side == "SELL" else 0.0
        transfer = amount * float(fees.get("transfer_fee_rate", 0.00001))
        return round(commission + transfer + tax, 4)
