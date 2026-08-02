# -*- coding: utf-8 -*-
"""
持仓风控巡检 (硬性止损/移动止盈/市场降仓)
==========================================
为什么需要: Agent 分析是周期性/事件触发的, 行情暴跌时可能来不及反应。
巡检是独立于 Agent 的硬性风控层: 交易时段按固定间隔扫描全部持仓,
浮亏超阈值立即执行止损(经风控引擎+合规), 不依赖 Agent 及时性。

触发规则(阈值全部在 config/risk_limits.yaml position_monitor 可调):
  1. 硬止损:   浮亏超过 stop_loss_pct → 全额卖出
  2. 移动止盈: 从持仓最高价回撤超过 trailing_stop_pct → 全额卖出(锁定利润)
  3. 市场降仓: 大盘 risk_off 时卖出持仓的 market_risk_reduce_ratio(留防守仓)
优先级: 硬止损 > 移动止盈 > 市场降仓
"""
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.ids import gen_order_intent_id
from database import repository as repo
from memory.audit_log import AuditLogger
from notification.notification_service import get_notification_service
from risk.circuit_breaker import CircuitBreaker
from risk.risk_engine import get_risk_engine

logger = logging.getLogger("risk.position_monitor")


class PositionMonitor:
    """持仓风控巡检器。"""

    def __init__(self):
        self.cfg = get_settings().get("risk.position_monitor", {}) or {}
        self._today_stop_orders = 0
        self._today = date.today()

    # ------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def config_view(self) -> Dict[str, Any]:
        """配置视图(Web展示用)。"""
        return dict(self.cfg)

    # ------------------------------------------------------------------
    def check_once(self, broker=None, force_quote: bool = True) -> Dict[str, Any]:
        """
        执行一轮巡检: 扫描全部持仓, 触发止损/止盈/降仓。
        返回 {checked, triggered: [...], executed: [...], skipped: [...]}
        """
        from workflows.intraday_monitor_workflow import get_broker
        from data_service.market_data_service import get_market_service
        broker = broker or get_broker()
        audit = AuditLogger.instance()
        circuit = CircuitBreaker.instance()

        result = {"checked": 0, "triggered": [], "executed": [], "skipped": []}

        if not self.enabled():
            result["skipped"].append("巡检未启用(position_monitor.enabled=false)")
            return result
        if circuit.is_paused():
            result["skipped"].append(f"系统熔断中: {circuit.paused_reason()}")
            return result

        # 每日止损单计数(重置)
        today = date.today()
        if today != self._today:
            self._today = today
            self._today_stop_orders = 0

        # 大盘风险状态(市场降仓判断)
        market_risk_off = False
        if self.cfg.get("market_risk_filter", True):
            try:
                diag = repo.get_latest_market_diagnostic(max_age_minutes=120)
                market_risk_off = bool(diag and diag.get("state") == "risk_off")
            except Exception as exc:
                logger.warning("市场诊断读取失败: %s", exc)

        # 最新行情(先刷新再判断, 确保用的是实时价)
        svc = get_market_service()
        positions = broker.get_positions()
        for p in positions:
            symbol = p.get("symbol", "")
            qty = int(p.get("total_qty", 0) or 0)
            if qty <= 0:
                continue
            result["checked"] += 1
            # 拉最新行情
            try:
                quote, qrep = svc.get_realtime_quote(symbol, "etf")
                price = float(quote.get("latest_price", 0) or 0)
            except Exception:
                price = float(p.get("latest_price", 0) or 0)
            if price <= 0:
                continue
            broker.mark_to_market({symbol: price})

            cost = float(p.get("cost_price", 0) or 0)
            peak = float(p.get("peak_price", 0) or 0) or cost
            available = int(p.get("available_qty", 0) or 0)
            if available < int(self.cfg.get("min_sell_qty", 100)):
                continue

            pnl_pct = price / cost - 1 if cost else 0.0
            from_peak = price / peak - 1 if peak else 0.0
            reason = ""
            sell_qty = 0
            stop_type = ""

            # 1. 硬止损(最高优先)
            if pnl_pct <= -float(self.cfg.get("stop_loss_pct", 0.08)):
                stop_type, reason = "止损", f"硬止损: 浮亏{pnl_pct:.1%}超过{-float(self.cfg.get('stop_loss_pct', 0.08)):.0%}"
                sell_qty = available
            # 2. 移动止盈
            elif from_peak <= -float(self.cfg.get("trailing_stop_pct", 0.08)):
                stop_type, reason = "止盈", f"移动止盈: 从最高{peak:.3f}回撤{from_peak:.1%}(现价{price:.3f}, 盈亏{pnl_pct:+.1%})"
                sell_qty = available
            # 3. 市场降仓
            elif market_risk_off and self.cfg.get("market_risk_filter", True):
                reduce_ratio = float(self.cfg.get("market_risk_reduce_ratio", 0.5))
                keep = int(qty * (1 - reduce_ratio) // 1)
                sell_qty = qty - keep
                if sell_qty >= int(self.cfg.get("min_sell_qty", 100)):
                    stop_type, reason = "降仓", f"市场risk_off, 减仓{reduce_ratio:.0%}(卖{sell_qty}留{keep})"
                else:
                    sell_qty = 0

            if sell_qty <= 0:
                continue
            result["triggered"].append({"symbol": symbol, "type": stop_type,
                                        "qty": sell_qty, "price": price,
                                        "pnl_pct": round(pnl_pct, 4),
                                        "reason": reason})
            audit.log("position_monitor_trigger", "position_monitor",
                      {"symbol": symbol, "type": stop_type, "qty": sell_qty,
                       "price": price, "reason": reason})

            # 执行(经风控+合规)
            if not self.cfg.get("auto_execute", True):
                result["skipped"].append(f"{symbol}: auto_execute=false 仅告警")
                get_notification_service().send_risk_alert_email([{
                    "symbol": symbol, "name": p.get("name", ""),
                    "risk": f"{stop_type}触发", "detail": reason,
                }])
                continue
            if self._today_stop_orders >= int(self.cfg.get("max_daily_stop_orders", 10)):
                result["skipped"].append(f"{symbol}: 当日止损单已达上限")
                continue

            executed = self._execute_stop(broker, symbol, p.get("name", ""),
                                          sell_qty, price, stop_type, reason)
            if executed:
                self._today_stop_orders += 1
                result["executed"].append({**executed, "type": stop_type})
            else:
                result["skipped"].append(f"{symbol}: 风控/执行拦截")

        if result["executed"]:
            logger.warning("持仓巡检执行: %s", result["executed"])
        return result

    # ------------------------------------------------------------------
    def _execute_stop(self, broker, symbol: str, name: str, qty: int,
                      price: float, stop_type: str, reason: str) -> Optional[Dict[str, Any]]:
        """
        执行止损单: 构造交易计划 → 五层风控 → 合规 → 下单。
        风控对 SELL 不拦波动率/溢价; 仅检查 T+1 可用与限额。
        """
        audit = AuditLogger.instance()
        account = broker.get_account()
        plan = {
            "plan_id": f"PLAN-PM-{datetime.now():%Y%m%d%H%M%S%f}",
            "decision_id": "PM-DECISION",
            "symbol": symbol, "name": name,
            "action": "SELL",
            "target_weight": 0.0,
            "order_amount": round(price * qty, 2),
            "estimated_quantity": qty,
            "order_type": "LIMIT",
            "limit_price": round(price * 0.995, 4),   # 略低于现价, 确保成交
            "confidence": 0.95,
            "reasons": [f"[持仓风控巡检] {reason}"],
            "risks": [],
        }
        # 五层风控(卖出放行波动率, 只拦异常)
        risk = get_risk_engine().check_plan(plan, account)
        if risk.result == "REJECT":
            audit.log("position_monitor_blocked", "position_monitor",
                      {"symbol": symbol, "reason": risk.blocked_reason})
            logger.warning("巡检止损被风控拦截 %s: %s", symbol, risk.blocked_reason)
            return None
        # 合规(交易时段/次数/数量) —— ComplianceAgent 规则为同步逻辑, 直接调用
        from agents.execution_agents import ComplianceAgent
        cr = ComplianceAgent()._rules(plan, {"enforce_trading_hours": True})
        if cr.get("compliance_status") == "BLOCKED":
            audit.log("position_monitor_blocked", "position_monitor",
                      {"symbol": symbol, "reason": cr.get("reason")})
            logger.warning("巡检止损被合规拦截 %s: %s", symbol, cr.get("reason"))
            return None
        # 下单
        try:
            order = broker.place_order({
                "symbol": symbol, "side": "SELL", "qty": qty,
                "order_type": "LIMIT", "price": round(price * 0.995, 4),
                "plan_id": plan["plan_id"],
                "order_intent_id": gen_order_intent_id(),
                "name": name,
            })
            audit.log("position_monitor_order", "position_monitor",
                      {"symbol": symbol, "order_id": order.get("order_id"),
                       "qty": qty, "price": plan["limit_price"], "type": stop_type})
            # 通知(邮件+界面)
            try:
                get_notification_service().send_risk_alert_email([{
                    "symbol": symbol, "name": name,
                    "risk": f"持仓巡检{stop_type}单已提交",
                    "detail": f"{reason}; 数量{qty}份 价格{plan['limit_price']:.3f}",
                }])
            except Exception as exc:
                logger.warning("止损通知发送失败: %s", exc)
            return {"symbol": symbol, "order_id": order.get("order_id"),
                    "qty": qty, "price": plan["limit_price"], "reason": reason}
        except Exception as exc:
            audit.log("position_monitor_fail", "position_monitor",
                      {"symbol": symbol, "error": str(exc)})
            logger.error("巡检止损下单失败 %s: %s", symbol, exc)
            return None


_monitor: Optional[PositionMonitor] = None


def get_position_monitor() -> PositionMonitor:
    global _monitor
    if _monitor is None:
        _monitor = PositionMonitor()
    return _monitor
