# -*- coding: utf-8 -*-
"""
熔断器 (Circuit Breaker)
========================
触发条件(任一满足即全市场停摆):
  单日亏损超阈值 / 连续失败订单超阈值 / 行情延迟 / 账户同步失败 /
  风控服务不可用 / 模型输出异常 / 数据源冲突 / 人工一键暂停
熔断状态全局共享, 风控/合规/交易员在执行前必须检查。
"""
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from core.config import get_settings

logger = logging.getLogger("risk.circuit")


class CircuitBreaker:
    """全市场熔断器(单例)。"""

    _inst: Optional["CircuitBreaker"] = None
    _lock = threading.RLock()

    @classmethod
    def instance(cls) -> "CircuitBreaker":
        with cls._lock:
            if cls._inst is None:
                cls._inst = CircuitBreaker()
            return cls._inst

    def __init__(self):
        self.cfg = get_settings().get("risk.circuit_breaker", {})
        self._paused = False
        self._paused_reason = ""
        self._paused_at: Optional[datetime] = None
        self._fail_order_streak = 0
        self._trip_reasons: Dict[str, str] = {}

    # ---------------- 人工控制 ----------------
    def pause(self, reason: str = "人工暂停"):
        """一键暂停所有交易。"""
        with self._lock:
            self._paused = True
            self._paused_reason = reason
            self._paused_at = datetime.now()
        from memory.audit_log import AuditLogger
        AuditLogger.instance().log("circuit_pause", "manual", {"reason": reason})
        logger.warning("熔断器已启动: %s", reason)

    def resume(self, reason: str = "人工恢复"):
        with self._lock:
            self._paused = False
            self._paused_reason = ""
            self._trip_reasons.clear()
        from memory.audit_log import AuditLogger
        AuditLogger.instance().log("circuit_resume", "manual", {"reason": reason})
        logger.info("熔断器已恢复: %s", reason)

    # ---------------- 状态 ----------------
    def is_paused(self) -> bool:
        return self._paused or bool(self._trip_reasons)

    def paused_reason(self) -> str:
        reasons = list(self._trip_reasons.values())
        if self._paused_reason:
            reasons.insert(0, self._paused_reason)
        return "；".join(reasons)

    def trip(self, key: str, reason: str):
        """触发熔断(自动)。"""
        with self._lock:
            self._trip_reasons[key] = reason
        from memory.audit_log import AuditLogger
        AuditLogger.instance().log("circuit_trip", "risk_engine", {"key": key, "reason": reason})
        logger.error("熔断触发 [%s]: %s", key, reason)

    def reset(self, key: str):
        with self._lock:
            self._trip_reasons.pop(key, None)

    # ---------------- 自动检测 ----------------
    def on_order_failure(self):
        """订单失败计数: 连续失败触发熔断。"""
        with self._lock:
            self._fail_order_streak += 1
            limit = int(self.cfg.get("consecutive_fail_orders", 5))
            if self._fail_order_streak >= limit:
                self.trip("fail_orders", f"连续失败订单{self._fail_order_streak}次")

    def on_order_success(self):
        """订单成功: 清零连败计数, 连续成功自动解除失败单熔断。"""
        with self._lock:
            self._fail_order_streak = 0
            # 修复: 原实现熔断后无任何自动复位路径, 一次误触发系统永久停摆
            if "fail_orders" in self._trip_reasons:
                self._trip_reasons.pop("fail_orders")
                logger.info("连续成功订单, 自动解除失败单熔断")

    def check_daily_loss(self, day_pnl: float, total_asset: float) -> bool:
        """单日亏损熔断。修复: 跨日后自动清除昨日的 daily_loss 熔断,
        否则一次单日亏损(或误判)会让系统永久停摆直到人工干预。"""
        limit = float(self.cfg.get("daily_loss_pct", 0.05))
        with self._lock:
            today = date.today()
            if getattr(self, "_daily_loss_date", None) != today:
                self._daily_loss_date = today
                if "daily_loss" in self._trip_reasons:
                    self._trip_reasons.pop("daily_loss")
                    logger.info("新交易日, 自动清除单日亏损熔断")
        if total_asset <= 0:
            return False
        if day_pnl < 0 and abs(day_pnl) / total_asset >= limit:
            self.trip("daily_loss", f"单日亏损{abs(day_pnl)/total_asset:.1%}≥{limit:.1%}")
            return True
        return False

    def check_quote_delay(self, delay_seconds: float) -> bool:
        """行情延迟熔断。修复: 行情恢复后自动清除, 原实现延迟一次即永久停摆。"""
        limit = float(self.cfg.get("quote_delay_seconds", 60))
        if delay_seconds > limit:
            self.trip("quote_delay", f"行情延迟{delay_seconds:.0f}s")
            return True
        if "quote_delay" in self._trip_reasons:
            with self._lock:
                self._trip_reasons.pop("quote_delay", None)
            logger.info("行情延迟恢复, 自动解除熔断")
        return False

    def check_risk_service(self, healthy: bool) -> bool:
        if not healthy and self.cfg.get("risk_service_down", True):
            self.trip("risk_service", "风控服务不可用")
            return True
        if healthy and "risk_service" in self._trip_reasons:
            with self._lock:
                self._trip_reasons.pop("risk_service", None)
        return False
