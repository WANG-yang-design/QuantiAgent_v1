# -*- coding: utf-8 -*-
"""Agent 影子评估。

Agent 不拥有下单权。本模块把正式策略信号、当时最近一次首席观点和实际订单结果
固化为同一条样本，并用本地已落库日 K 计算 1/3/5/10 个交易日远期收益。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from core.ids import gen_id
from core.timeutil import now as business_now
from database import repository as repo
from database.db_session import get_session
from database.models import AgentOutput, AgentRun

HORIZONS = (1, 3, 5, 10)


def latest_chief_view(symbol: str, max_age_hours: int = 24,
                      at: Optional[datetime] = None) -> Dict[str, Any]:
    """取得信号产生前最近一次 Agent 首席观点；只观察，不触发任何交易动作。"""
    at = at or business_now()
    with get_session() as s:
        run = s.query(AgentRun).filter(
            AgentRun.agent_name == "chief_researcher",
            AgentRun.symbol == symbol,
            AgentRun.status == "OK",
            AgentRun.start_time <= at,
            AgentRun.start_time >= at - timedelta(hours=max_age_hours),
        ).order_by(AgentRun.start_time.desc()).first()
        if not run:
            return {"decision": "NO_VIEW", "confidence": 0.0, "trace_id": "",
                    "decision_time": None, "age_minutes": None}
        out = s.query(AgentOutput).filter_by(run_id=run.run_id).first()
        payload = (out.output_json or {}) if out else {}
        age = max(0.0, (at - run.start_time).total_seconds() / 60)
        return {
            "decision": payload.get("research_decision") or "NO_VIEW",
            "confidence": float(payload.get("confidence", 0) or 0),
            "trace_id": run.trace_id or "",
            "decision_time": run.start_time,
            "age_minutes": round(age, 2),
        }


def classify_agreement(strategy_action: str, agent_decision: str) -> str:
    action = str(strategy_action or "").upper()
    decision = str(agent_decision or "NO_VIEW").upper()
    if decision == "NO_VIEW":
        return "NO_VIEW"
    if action == "BUY":
        if decision == "BUY_CANDIDATE":
            return "AGREE"
        if decision in ("SELL_CANDIDATE", "EXCLUDE"):
            return "DISAGREE"
        return "NEUTRAL"
    if action == "SELL":
        if decision in ("SELL_CANDIDATE", "EXCLUDE"):
            return "AGREE"
        if decision == "BUY_CANDIDATE":
            return "DISAGREE"
        return "NEUTRAL"
    return "NEUTRAL"


def record_strategy_observation(*, strategy_id: str, strategy_name: str,
                                universe_snapshot_id: str, symbol: str, name: str,
                                signal_date: date, strategy_action: str,
                                strategy_reason: str, signal_price: float,
                                quantity: int, order_id: str = "",
                                execution_status: str = "SIGNALLED",
                                max_agent_age_hours: int = 24) -> Dict[str, Any]:
    signal_time = business_now()
    view = latest_chief_view(symbol, max_agent_age_hours, signal_time)
    agreement = classify_agreement(strategy_action, view["decision"])
    row = repo.upsert_agent_shadow_observation({
        "shadow_id": gen_id("SHADOW"),
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "universe_snapshot_id": universe_snapshot_id,
        "symbol": symbol, "name": name,
        "signal_date": signal_date, "signal_time": signal_time,
        "strategy_action": strategy_action,
        "strategy_reason": strategy_reason,
        "signal_price": float(signal_price or 0), "quantity": int(quantity or 0),
        "order_id": order_id or "", "execution_status": execution_status,
        "agent_decision": view["decision"],
        "agent_confidence": view["confidence"],
        "agent_trace_id": view["trace_id"],
        "agent_decision_time": view["decision_time"],
        "agent_age_minutes": view["age_minutes"],
        "agreement": agreement,
        "forward_returns": {}, "evaluated_horizons": [],
        "evaluation_status": "PENDING",
    })
    return shadow_to_dict(row)


def evaluate_pending(horizons: Iterable[int] = HORIZONS,
                     limit: int = 5000) -> Dict[str, int]:
    """仅用已经落库且位于信号日之后的交易日收盘价更新远期收益。"""
    hs = sorted({int(h) for h in horizons if int(h) > 0})
    rows = repo.list_agent_shadow_observations(limit=limit)
    updated = complete = 0
    for row in rows:
        if not row.signal_price or row.signal_price <= 0:
            continue
        bars = repo.get_daily_bars(
            row.symbol, row.signal_date + timedelta(days=1),
            row.signal_date + timedelta(days=max(hs, default=10) * 3 + 15))
        by_date = {}
        for bar in bars:
            if bar.trade_date > row.signal_date:
                by_date.setdefault(bar.trade_date, bar)
        ordered = [by_date[d] for d in sorted(by_date)]
        returns = dict(row.forward_returns or {})
        evaluated = set(int(x) for x in (row.evaluated_horizons or []))
        for h in hs:
            if len(ordered) >= h:
                returns[str(h)] = round(float(ordered[h - 1].close) / row.signal_price - 1, 8)
                evaluated.add(h)
        status = "COMPLETE" if all(h in evaluated for h in hs) else (
            "PARTIAL" if evaluated else "PENDING")
        if returns != (row.forward_returns or {}) or status != row.evaluation_status:
            repo.update_agent_shadow_evaluation(
                row.shadow_id, returns, sorted(evaluated), status)
            updated += 1
        complete += int(status == "COMPLETE")
    return {"scanned": len(rows), "updated": updated, "complete": complete}


def shadow_summary(days: int = 365, limit: int = 5000) -> Dict[str, Any]:
    start = business_now() - timedelta(days=max(1, min(int(days), 3650)))
    rows = repo.list_agent_shadow_observations(start=start, limit=limit)
    groups: Dict[str, List[Any]] = defaultdict(list)
    action_groups: Dict[str, List[Any]] = defaultdict(list)
    for row in rows:
        groups[row.agreement].append(row)
        action_groups[f"{row.strategy_action}:{row.agreement}"].append(row)

    def metrics(label: str, sample: List[Any]) -> Dict[str, Any]:
        item: Dict[str, Any] = {"group": label, "samples": len(sample)}
        for h in HORIZONS:
            # SELL 信号按价格下跌为正贡献，便于赞同/反对组横向比较策略方向收益。
            vals = [float((r.forward_returns or {}).get(str(h))) *
                    (1 if r.strategy_action == "BUY" else -1) for r in sample
                    if (r.forward_returns or {}).get(str(h)) is not None]
            item[f"n_{h}d"] = len(vals)
            item[f"avg_{h}d"] = round(sum(vals) / len(vals), 6) if vals else None
            item[f"win_{h}d"] = round(sum(v > 0 for v in vals) / len(vals), 4) if vals else None
        return item

    recent = [shadow_to_dict(r) for r in rows[:100]]
    return {
        "mode": "SHADOW_ONLY", "days": days,
        "total_samples": len(rows),
        "evaluated_5d": sum(1 for r in rows if "5" in (r.forward_returns or {})),
        "groups": [metrics(k, groups[k]) for k in ("AGREE", "NEUTRAL", "DISAGREE", "NO_VIEW")],
        "by_action": [metrics(k, v) for k, v in sorted(action_groups.items())],
        "recent": recent,
        "horizons": list(HORIZONS),
    }


def shadow_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "shadow_id": row.shadow_id, "strategy_id": row.strategy_id,
        "strategy_name": row.strategy_name,
        "universe_snapshot_id": row.universe_snapshot_id,
        "symbol": row.symbol, "name": row.name,
        "signal_date": str(row.signal_date), "signal_time": str(row.signal_time)[:19],
        "strategy_action": row.strategy_action,
        "strategy_reason": row.strategy_reason,
        "signal_price": row.signal_price, "quantity": row.quantity,
        "order_id": row.order_id, "execution_status": row.execution_status,
        "agent_decision": row.agent_decision,
        "agent_confidence": row.agent_confidence,
        "agent_trace_id": row.agent_trace_id,
        "agent_decision_time": str(row.agent_decision_time)[:19] if row.agent_decision_time else "",
        "agent_age_minutes": row.agent_age_minutes,
        "agreement": row.agreement,
        "forward_returns": row.forward_returns or {},
        "evaluated_horizons": row.evaluated_horizons or [],
        "evaluation_status": row.evaluation_status,
    }
