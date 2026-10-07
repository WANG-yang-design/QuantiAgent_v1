# -*- coding: utf-8 -*-
"""
模拟盘重置 + 运行记录归档
==========================
设计目标: 重置绝不可丢失历史。
- 重置前: 把当前一轮的账户/持仓/订单/成交/净值/确认单完整归档
  (DB 表 paper_run_archives + reports/paper_archive/<archive_id>.json 双备份)
- 重置时: 只清空交易数据与账户资金, 审计日志/归档记录永久保留
- 重置后: 账户回到指定初始资金, 内存缓存与订单簿同步清空

调用入口:
- CLI:  python main.py reset-paper --initial-cash 100000 --note "策略V2上线"
- Web:  POST /api/paper/reset
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import ROOT_DIR, get_settings
from core.ids import gen_id
from database import repository as repo
from memory.audit_log import AuditLogger

logger = logging.getLogger("paper.reset")

ARCHIVE_DIR = ROOT_DIR / "reports" / "paper_archive"


def _max_drawdown(assets: list) -> float:
    """最大回撤(峰值到谷底)。"""
    peak = None
    mdd = 0.0
    for v in assets:
        try:
            v = float(v or 0)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        peak = v if peak is None else max(peak, v)
        if peak:
            mdd = max(mdd, (peak - v) / peak)
    return round(mdd, 6)


def _export_json(archive_id: str, payload: Dict[str, Any]) -> str:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{archive_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


def reset_paper_account(broker=None, initial_cash: Optional[float] = None,
                        note: str = "", run_name: str = "",
                        account_id: str = "") -> Dict[str, Any]:
    """归档当前一轮并重置模拟盘。

    返回 {ok, archive_id, export_path, summary:{...}, purged:{...}}
    """
    from workflows.intraday_monitor_workflow import get_broker
    broker = broker or get_broker()
    cfg = get_settings()
    account_id = account_id or broker.account_id or str(
        cfg.get("paper_account.account_id", "PA-001"))
    if initial_cash is None:
        initial_cash = float(cfg.get("paper_account.initial_cash", 100000))
    initial_cash = round(float(initial_cash), 2)
    if initial_cash <= 0:
        raise ValueError("初始资金必须大于 0")

    # 1. 归档前取最新行情估值(保证最终资产数字准确)
    try:
        acc_before = broker.get_account()
    except Exception as exc:
        logger.warning("重置前账户读取失败(使用DB值): %s", exc)
        acc_before = {}

    data = repo.collect_paper_run_data(account_id)
    account = data.get("account") or {}
    snapshots = data.get("snapshots") or []
    trades = data.get("trades") or []
    orders = data.get("orders") or []
    positions = data.get("positions") or []

    final_asset = float(acc_before.get("total_asset")
                        or account.get("total_asset") or 0)
    init_cash = float(account.get("init_cash") or 0) or initial_cash
    total_pnl = round(final_asset - init_cash, 2)
    total_return = round(total_pnl / init_cash, 6) if init_cash else 0.0
    assets = [s.get("total_asset") for s in snapshots] + [final_asset]
    # 本轮起点: 所有可用时间戳中最早的一个(快照/成交/订单/账户更新时间)
    candidates = ([s.get("snapshot_time") for s in snapshots] +
                  [t.get("trade_time") for t in trades] +
                  [o.get("created_at") for o in orders] +
                  [account.get("update_time")])
    parsed = [d for d in (_parse_dt(x) for x in candidates) if d is not None]
    started_at = min(parsed) if parsed else None

    summary = {
        "account": account,
        "positions": positions,
        "orders": orders,
        "trades": trades,
        "snapshots": snapshots,
        "confirmations": data.get("confirmations") or [],
        "stats": {
            "initial_cash": round(init_cash, 2),
            "final_asset": round(final_asset, 2),
            "total_pnl": total_pnl,
            "total_return": total_return,
            "max_drawdown": _max_drawdown(assets),
            "total_fee": round(float(account.get("total_fee") or 0), 2),
            "order_count": len(orders),
            "trade_count": len(trades),
            "position_count": len(positions),
            "snapshot_count": len(snapshots),
        },
    }
    archive_id = gen_id("ARCH")
    export_path = _export_json(archive_id, {"archive_id": archive_id,
                                            "account_id": account_id,
                                            "created_at": datetime.now().isoformat(),
                                            "note": note, **summary})

    archive = repo.save_paper_run_archive({
        "archive_id": archive_id,
        "account_id": account_id,
        "run_name": run_name or f"运行至{datetime.now():%Y-%m-%d %H:%M}",
        "note": note,
        "started_at": started_at,
        "ended_at": datetime.now(),
        "initial_cash": round(init_cash, 2),
        "final_asset": round(final_asset, 2),
        "total_pnl": total_pnl,
        "total_return": total_return,
        "max_drawdown": summary["stats"]["max_drawdown"],
        "total_fee": summary["stats"]["total_fee"],
        "order_count": len(orders),
        "trade_count": len(trades),
        "position_count": len(positions),
        "summary_json": summary,
        "export_path": export_path,
    })

    # 2. 清空交易数据
    purged = repo.purge_paper_account_data(account_id)

    # 3. 重置账户资金
    from database.models import Account
    acc = repo.get_account(account_id)
    if acc is None:
        acc = Account(account_id=account_id, account_type="paper",
                      init_cash=initial_cash)
    acc.cash = initial_cash
    acc.frozen_cash = 0.0
    acc.market_value = 0.0
    acc.total_asset = initial_cash
    acc.total_pnl = 0.0
    acc.day_pnl = 0.0
    acc.total_fee = 0.0
    acc.init_cash = initial_cash
    acc.status = "normal"
    repo.save_account(acc)

    # 4. 同步内存缓存(账户/持仓/订单簿), 避免重置后仍显示旧数据
    try:
        broker.account._sync_from_db(force=True)
        broker.orders._orders.clear()
        broker.orders._load_orders()
    except Exception as exc:
        logger.warning("重置后内存缓存同步失败(重启后自动恢复): %s", exc)
    try:
        from risk.position_monitor import get_position_monitor
        pm = get_position_monitor()
        pm._executed_today = set()
        pm._today_stop_orders = 0
    except Exception:
        pass

    AuditLogger.instance().log("paper_account_reset", "paper_reset", {
        "account_id": account_id, "archive_id": archive_id,
        "initial_cash": initial_cash, "final_asset": round(final_asset, 2),
        "total_pnl": total_pnl, "purged": purged, "note": note,
    })
    logger.warning("模拟盘已重置: 账户=%s 新初始资金=%.2f 归档=%s "
                   "(上轮: 总资产%.2f 盈亏%+.2f, 清理 %s)",
                   account_id, initial_cash, archive_id, final_asset,
                   total_pnl, purged)

    return {
        "ok": True,
        "archive_id": archive_id,
        "export_path": export_path,
        "account_id": account_id,
        "initial_cash": initial_cash,
        "summary": summary["stats"],
        "purged": purged,
        "archive": _archive_view(archive),
    }


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _archive_view(row) -> Dict[str, Any]:
    """归档摘要(不含全量明细, 列表用)。"""
    return {
        "archive_id": row.archive_id,
        "account_id": row.account_id,
        "run_name": row.run_name,
        "note": row.note,
        "started_at": str(row.started_at or ""),
        "ended_at": str(row.ended_at or ""),
        "initial_cash": row.initial_cash,
        "final_asset": row.final_asset,
        "total_pnl": row.total_pnl,
        "total_return": row.total_return,
        "max_drawdown": row.max_drawdown,
        "total_fee": row.total_fee,
        "order_count": row.order_count,
        "trade_count": row.trade_count,
        "position_count": row.position_count,
        "export_path": row.export_path,
        "created_at": str(row.created_at or ""),
    }
