# -*- coding: utf-8 -*-
"""
风控规则引擎 (五层 + 组合层)
============================
账户级 / 标的级 / 策略级 / 订单级 / 模型级 / 组合级
输出 RiskCheckResult: APPROVE / REJECT / REDUCE / CONFIRM_REQUIRED
风控为硬规则, 优先级高于交易员 Agent。逐层检查并记录审计明细。
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings
from database import repository as repo
from risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger("risk.engine")


@dataclass
class RiskCheckResult:
    """风控审核结果(与文档 19.4 一致)。"""
    risk_check_id: str
    decision_id: str = ""
    result: str = "APPROVE"            # APPROVE/REJECT/REDUCE/CONFIRM_REQUIRED
    approved_amount: float = 0.0
    approved_quantity: int = 0
    risk_level: str = "LOW"            # LOW/MEDIUM/HIGH
    warnings: List[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None
    layer_results: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "risk_check_id": self.risk_check_id,
            "decision_id": self.decision_id,
            "result": self.result,
            "approved_amount": self.approved_amount,
            "approved_quantity": self.approved_quantity,
            "risk_level": self.risk_level,
            "warnings": self.warnings,
            "blocked_reason": self.blocked_reason,
            "layer_results": self.layer_results,
        }


class RiskEngine:
    """五层风控引擎。"""

    def __init__(self):
        self.cfg = get_settings().get("risk", {})
        self.cb = CircuitBreaker.instance()

    # ==================================================================
    def check_plan(self, plan: Dict[str, Any], account_view: Dict[str, Any],
                   features: Optional[Dict[str, Any]] = None,
                   etf_features: Optional[Dict[str, Any]] = None,
                   broker=None) -> RiskCheckResult:
        """
        审核一份交易计划(交易员输出)。
        plan: {plan_id, decision_id, symbol, name, action, target_weight,
               order_amount, estimated_quantity, order_type, limit_price,
               confidence, reasons, risks, human_confirm_required}
        """
        from core.ids import gen_risk_check_id
        result = RiskCheckResult(risk_check_id=gen_risk_check_id(),
                                 decision_id=plan.get("decision_id", ""))
        self.cb = CircuitBreaker.instance()
        total_asset = float(account_view.get("total_asset", 0) or 0)
        cash = float(account_view.get("cash", 0) or 0)

        # 0. 熔断检查(最高优先)
        if self.cb.is_paused():
            result.result = "REJECT"
            result.blocked_reason = f"系统熔断中: {self.cb.paused_reason()}"
            result.layer_results["circuit"] = {"result": "REJECT"}
            return self._finalize(plan, result)

        action = plan.get("action", "HOLD")
        if action == "HOLD":
            result.result = "APPROVE"
            result.risk_level = "LOW"
            result.layer_results["hold"] = {"result": "APPROVE"}
            return self._finalize(plan, result)

        layers: Dict[str, Any] = {}

        # ---- 1. 账户级 ----
        acc = self._check_account(action, plan, account_view)
        layers["account"] = acc
        if acc["result"] == "REJECT":
            return self._finalize(plan, result, "REJECT", acc)

        # ---- 2. 标的级 ----
        sym = self._check_symbol(plan, features, etf_features)
        layers["symbol"] = sym
        if sym["result"] == "REJECT":
            return self._finalize(plan, result, "REJECT", sym)

        # ---- 3. 策略级 ----
        strat = self._check_strategy(plan)
        layers["strategy"] = strat
        if strat["result"] == "REJECT":
            return self._finalize(plan, result, "REJECT", strat)

        # ---- 4. 订单级 ----
        ordr = self._check_order(plan, account_view, features)
        layers["order"] = ordr
        if ordr["result"] == "REJECT":
            return self._finalize(plan, result, "REJECT", ordr)

        # ---- 5. 模型级 ----
        model = self._check_model(plan)
        layers["model"] = model

        # ---- 6. 组合级(集中度) ----
        combo = self._check_portfolio(plan, account_view, broker)
        layers["portfolio"] = combo

        # ---- 汇总判定 ----
        warnings: List[str] = []
        for layer in layers.values():
            warnings.extend(layer.get("warnings", []))
        result.layer_results = layers
        result.warnings = warnings

        # 确认分级(文档 14): ≤1000元低风险自动 / ≤5000元中风险邮件界面确认 / 高风险禁止
        # 修复: 原实现"高风险=一律REJECT", 导致:
        #   1) 交易员标注 human_confirm_required 的计划(主动要求人工确认)被直接拒绝,
        #      人工确认队列永远为空, 用户无法干预;
        #   2) 高风险 SELL(止损/止盈/轮动离场)也被拒绝, 仓位无法退出。
        #   现改为: 人工确认标注或卖出单 → CONFIRM_REQUIRED(人工确认时附带分析原因);
        #   仅买入类高风险才 REJECT。
        amount = float(plan.get("order_amount", 0) or 0)
        risk_level = self._risk_level(plan, features, etf_features)
        result.risk_level = risk_level
        cp = self.cfg.get("confirmation_policy", {})
        action = str(plan.get("action", "HOLD")).upper()
        forced_confirm = bool(plan.get("human_confirm_required"))

        if model.get("result") == "REJECT":
            result.result = "REJECT"
            result.blocked_reason = model.get("reason") or "交易计划缺少解释(不可解释不得自动交易)"
        elif forced_confirm:
            result.result = "CONFIRM_REQUIRED"
            result.blocked_reason = "交易员标注需人工确认(特殊情况/高风险)"
        elif risk_level == "HIGH" and action != "SELL":
            result.result = "REJECT"
            result.blocked_reason = "高风险交易禁止执行"
        elif risk_level == "HIGH":
            # 高风险卖出(止损/止盈/减仓): 离场保护动作, 交人工确认而非直接拒绝
            result.result = "CONFIRM_REQUIRED"
            result.blocked_reason = f"高风险卖出(等级{risk_level}), 需人工确认"
        elif plan.get("confidence", 0) < float(
                (self.cfg.get("model_level") or {}).get("min_confidence", 0.55)):
            result.result = "CONFIRM_REQUIRED"
            result.blocked_reason = "模型置信度低于自动交易阈值"
        elif risk_level == "MEDIUM":
            result.result = "CONFIRM_REQUIRED"
            result.blocked_reason = "中风险交易, 需要人工确认"
        elif amount <= float(cp.get("auto_execute", {}).get("max_order_amount", 1000)):
            result.result = "APPROVE"          # 低风险+小额 → 自动
            # 降仓: 把金额折半等保护性动作由交易员回传
            result.approved_amount = amount
            result.approved_quantity = plan.get("estimated_quantity", 0)
        else:
            result.result = "APPROVE"
            result.approved_amount = amount
            result.approved_quantity = plan.get("estimated_quantity", 0)

        # REDUCE: 仅当尚未被 REJECT/CONFIRM_REQUIRED 时生效
        # (降仓是保护动作, 但不能绕过"需要人工确认"的分级)
        if sym.get("reduce_to") and result.result not in ("REJECT", "CONFIRM_REQUIRED"):
            result.result = "REDUCE"
            ratio = float(sym["reduce_to"])
            result.approved_amount = round(amount * ratio, 2)
            # 修复: 原实现按比例折算数量不做整手对齐, 非100整数倍的
            # 降仓数量被合规层 BLOCKED, 降仓计划必被拒。向下取整到交易单位。
            qty = int(plan.get("estimated_quantity", 0) * ratio)
            lot = int((get_settings().section("trading_rules").get("lot", {}) or {}).get("etf", 100))
            result.approved_quantity = qty // lot * lot
            if result.approved_quantity < lot:
                result.result = "REJECT"
                result.blocked_reason = f"降仓后数量{result.approved_quantity}不足1手({lot}), 拒绝"
            else:
                result.warnings.append(f"标的层降仓至 {ratio:.0%}: {sym.get('reason', '')}")

        # 需人工确认时把原因写入 warnings(前端链路页可直接看到分析原因)
        if result.result == "CONFIRM_REQUIRED" and result.blocked_reason \
                and result.blocked_reason not in result.warnings:
            result.warnings.append(f"需人工确认: {result.blocked_reason}")

        return self._finalize(plan, result)

    # ==================================================================
    def _finalize(self, plan, result: RiskCheckResult, forced="", layer=None) -> RiskCheckResult:
        """落库并返回。"""
        if forced == "REJECT":
            result.result = "REJECT"
            result.blocked_reason = layer.get("reason") or result.blocked_reason
            result.layer_results = {k: v for k, v in result.layer_results.items()}
            result.layer_results.setdefault("reject_layer", layer)
        repo.save_risk_check({
            "risk_check_id": result.risk_check_id,
            "decision_id": plan.get("decision_id", ""),
            "plan_id": plan.get("plan_id", ""),
            "result": result.result,
            "risk_level": result.risk_level,
            "approved_amount": result.approved_amount,
            "approved_quantity": result.approved_quantity,
            "warnings": result.warnings,
            "blocked_reason": result.blocked_reason,
            "layer_results": result.layer_results,
        })
        logger.info("风控结果 %s: %s %s", result.risk_check_id, result.result,
                    result.blocked_reason or "")
        return result

    # ------------------------------------------------------------------
    def _check_account(self, action, plan, account_view) -> Dict[str, Any]:
        acc_cfg = self.cfg.get("account_level", {})
        total_asset = float(account_view.get("total_asset", 0) or 0)
        cash = float(account_view.get("cash", 0) or 0)
        day_pnl = float(account_view.get("day_pnl", 0) or 0)
        max_pos = float(acc_cfg.get("max_total_position", 0.9))
        max_day_loss = float(acc_cfg.get("max_daily_loss", 0.03))
        min_cash = float(acc_cfg.get("min_cash_ratio", 0.1))
        out = {"result": "PASS", "warnings": []}

        if action == "BUY":
            if total_asset <= 0:
                # 账户未初始化(无行情估值)时无法判断比率 → 放行, 由订单层资金校验兜底
                out["warnings"].append("账户总资产为0, 跳过账户比率检查")
                return out
            # 总仓位检查
            market_value = float(account_view.get("market_value", 0) or 0)
            total_position = (market_value + float(plan.get("order_amount", 0))) / total_asset
            if total_position > max_pos:
                out["result"] = "REJECT"
                out["reason"] = f"加仓后总仓位{total_position:.0%}超过上限{max_pos:.0%}"
                return out
            # 现金比例(留底)
            if cash / total_asset < min_cash and cash < float(plan.get("order_amount", 0)):
                out["result"] = "REJECT"
                out["reason"] = f"现金比例{min_cash:.0%}保护, 不允许继续买入"
                return out
            # 单日亏损
            if day_pnl < 0 and abs(day_pnl) / total_asset >= max_day_loss:
                out["result"] = "REJECT"
                out["reason"] = f"单日亏损{abs(day_pnl)/total_asset:.1%}达到上限{max_day_loss:.1%}"
                return out
        return out

    def _check_symbol(self, plan, features, etf_features) -> Dict[str, Any]:
        """
        标的级风控。关键: 波动率/溢价/流动性规则只对 BUY 生效,
        卖出(止损/减仓/轮动)必须放行 —— 否则高波动标的无法止损离场。
        """
        sym_cfg = self.cfg.get("position_level", {})
        out = {"result": "PASS", "warnings": []}
        symbol = plan.get("symbol", "")
        action = plan.get("action", "HOLD")
        if symbol in sym_cfg.get("blacklist", []):
            if action == "BUY":
                out["result"] = "REJECT"
                out["reason"] = f"{symbol} 在黑名单中"
            else:
                # 卖出必须放行: 黑名单标的需要能止损离场
                out["warnings"].append(f"{symbol} 在黑名单中(卖出放行)")
            return out
        # ETF 溢价禁止(仅买入; 卖出不受溢价限制)
        if etf_features and action == "BUY":
            premium = float(etf_features.get("premium_rate", 0) or 0)
            if sym_cfg.get("forbid_high_premium_etf") and premium > float(sym_cfg.get("max_premium_rate", 0.03)):
                out["result"] = "REJECT"
                out["reason"] = f"ETF溢价率{premium:.2%}超过{float(sym_cfg.get('max_premium_rate', 0.03)):.1%}, 禁止买入"
                return out
            liquidity = float(etf_features.get("liquidity_score", 100) or 100)
            if liquidity < 40:
                out["warnings"].append(f"流动性评分{liquidity:.0f}偏低")
                out["reduce_to"] = 0.5
                out["reason"] = "流动性不足降仓"
        # 波动率限制(仅买入; 卖出止损不受波动率拦截)
        # 修复: volatility_20d 是年化值(×√252), 策略 max_vol 默认50%。
        # 原实现用 0.035 当阈值, 28.7%年化波动的标的全部被拒, 与策略严重不一致。
        # 现在以轮动策略的 active max_vol 为准(改策略自动跟随), yaml 可覆盖。
        if features and action == "BUY":
            vol = float(features.get("volatility_20d", 0) or 0)
            try:
                from strategies.rotation_executor import load_rotation_params
                _vol_default = float(load_rotation_params().get("max_vol", 0.5) or 0.5)
            except Exception:
                _vol_default = 0.5
            vol_limit = float(sym_cfg.get("max_volatility", _vol_default))
            if vol > vol_limit:
                out["result"] = "REJECT"
                out["reason"] = f"20日波动率{vol:.1%}超过上限{vol_limit:.0%}, 禁止买入"
                return out
            amount = float(features.get("amount_ma20", 0) or 0)
            if amount and amount < float(sym_cfg.get("min_avg_amount_20d", 3e7)):
                out["warnings"].append(f"20日均成交额{amount/1e4:.0f}万低于流动性标准")
        return out

    def _check_strategy(self, plan) -> Dict[str, Any]:
        # V1 简化: 单策略资金/频率保护由调度层控制, 此处保留接口
        return {"result": "PASS", "warnings": []}

    def _check_order(self, plan, account_view, features) -> Dict[str, Any]:
        ord_cfg = self.cfg.get("order_level", {})
        out = {"result": "PASS", "warnings": []}
        amount = float(plan.get("order_amount", 0) or 0)
        qty = int(plan.get("estimated_quantity", 0) or 0)
        if plan.get("action") != "SELL" and amount > float(ord_cfg.get("max_order_amount", 20000)):
            out["result"] = "REJECT"
            out["reason"] = f"单笔金额{amount:.0f}超过上限{ord_cfg.get('max_order_amount')}"
            return out
        if plan.get("action") == "SELL" and amount > float(ord_cfg.get("max_order_amount", 20000)):
            # 卖出(止损/止盈/减仓)不受单笔金额上限约束, 但记录告警
            out["warnings"].append(f"卖出金额{amount:.0f}超过常规限额(止损放行)")
        if qty > int(ord_cfg.get("max_order_quantity", 1000000)):
            out["result"] = "REJECT"
            out["reason"] = "单笔数量超限"
            return out
        # 价格偏离保护(方向性):
        #   BUY : 限价高于现价 2% 拒绝(防追高/手误)
        #   SELL: 限价低于现价 2% 拒绝(防手误低价贱卖)
        # 修复: 止损/止盈/风控降仓类卖出用放宽阈值 —— 行情急跌时快照
        # 陈旧的止损限价会被原规则误拒, 止损单永远无法提交
        limit_price = float(plan.get("limit_price", 0) or 0)
        if features and limit_price > 0:
            close = float(features.get("close", 0) or 0)
            if close > 0:
                dev = (limit_price - close) / close
                if plan.get("action") == "BUY" and dev > float(ord_cfg.get("max_price_deviation", 0.02)):
                    out["result"] = "REJECT"
                    out["reason"] = f"买入限价高于最新价{dev:.2%}超过2%"
                    return out
                if plan.get("action") == "SELL" and dev < -float(ord_cfg.get("max_price_deviation", 0.02)):
                    is_stop = any(kw in " ".join(plan.get("reasons", []) or [])
                                  for kw in ("止损", "止盈", "降仓", "风控"))
                    if not is_stop:
                        out["result"] = "REJECT"
                        out["reason"] = f"卖出限价低于最新价{abs(dev):.2%}超过2%"
                        return out
        # 卖出可用数量(T+1: 不能卖超过可用)
        if plan.get("action") == "SELL":
            positions = account_view.get("positions") or []
            pos = next((p for p in positions if p.get("symbol") == plan.get("symbol", "")), None)
            real_avail = int(pos.get("available_qty", 0)) if pos else 0
            if qty > real_avail:
                out["result"] = "REJECT"
                out["reason"] = f"卖出数量{qty}超过可用{real_avail}(T+1)"
                return out
        return out

    def _check_model(self, plan) -> Dict[str, Any]:
        model_cfg = self.cfg.get("model_level", {})
        out = {"result": "PASS", "warnings": []}
        if not plan.get("reasons"):
            out["result"] = "REJECT"
            out["reason"] = "交易计划缺少解释(不可解释不得自动交易)"
        return out

    def _check_portfolio(self, plan, account_view, broker) -> Dict[str, Any]:
        combo_cfg = self.cfg.get("portfolio_level", {})
        out = {"result": "PASS", "warnings": []}
        if plan.get("action") != "BUY":
            return out
        # 简化集中度: 同跟踪指数 ETF 合计仓位
        symbol = plan.get("symbol", "")
        positions = (account_view.get("positions") or [])
        related = [p for p in positions if p.get("symbol", "")[:3] == symbol[:3]]
        total_asset = float(account_view.get("total_asset", 0) or 0)
        if total_asset <= 0:
            return out
        add_value = float(plan.get("order_amount", 0) or 0)
        cur_ratio = sum(p.get("market_value", 0) for p in related) / total_asset
        new_ratio = (sum(p.get("market_value", 0) for p in related) + add_value) / total_asset
        if new_ratio > float(combo_cfg.get("max_related_ratio", 0.6)):
            out["result"] = "REJECT"
            out["reason"] = f"同类ETF合计仓位{new_ratio:.0%}超过集中度上限"
        return out

    def _risk_level(self, plan, features, etf_features) -> str:
        """风险等级评定(供确认分级)。"""
        score = 0
        if float(plan.get("confidence", 0) or 0) < 0.6:
            score += 1
        if features:
            vol = float(features.get("volatility_20d", 0) or 0)
            # 修复: 年化口径与策略一致(原 0.03 把28.7%年化波动也算高风险,
            # 几乎所有买入都被评为 MEDIUM 需人工确认)
            try:
                from strategies.rotation_executor import load_rotation_params
                _vmax = float(load_rotation_params().get("max_vol", 0.5) or 0.5)
            except Exception:
                _vmax = 0.5
            if vol > _vmax:
                score += 1
        if etf_features and float(etf_features.get("premium_rate", 0) or 0) > 0.02:
            score += 1
        if plan.get("risks"):
            score += 1
        if score >= 3:
            return "HIGH"
        if score >= 1:
            return "MEDIUM"
        return "LOW"


_engine: Optional[RiskEngine] = None


def get_risk_engine() -> RiskEngine:
    global _engine
    if _engine is None:
        _engine = RiskEngine()
    return _engine
