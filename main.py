# -*- coding: utf-8 -*-
"""
多Agent量化交易系统 V1 - 命令行入口
====================================
用法:
  python main.py init-db                    初始化数据库
  python main.py fetch-symbols              更新ETF池
  python main.py fetch-daily --symbols ...   拉取日K
  python main.py scan 510300                单标的盘中分析(完整Agent链路)
  python main.py scan-pool --top 20         扫描池内标的
  python main.py backtest --start 2024-01-01 --end 2025-12-31 --symbols ...
  python main.py serve                      启动Web管理台(8080)
  python main.py scheduler                  启动调度器
  python main.py review                     日终复盘+日报
  python main.py test-email                 测试邮件
  python main.py pause / resume             紧急按钮
  python main.py status                     系统状态
  python main.py confirm <id> [approve|reject]
  python main.py rag <query>                RAG 检索测试
"""
import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta

from core.config import get_settings, ensure_dirs
from core.logging import setup_logging, get_logger

# Windows GBK 控制台兼容: 强制 UTF-8 输出(中文不报错)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logger = None


def _init_logging():
    global logger
    setup_logging()
    logger = get_logger("cli")


# ================================================================
def cmd_init_db(args):
    from database.init_db import init_db
    init_db(seed=True)
    print("[OK] 数据库初始化完成")


def cmd_fetch_symbols(args):
    from database import repository as repo
    from data_service.market_data_service import get_market_service
    svc = get_market_service()
    spot = svc.get_etf_spot()
    items = []
    for s in spot:
        exchange = "SH" if s["symbol"].startswith(("5", "6", "9")) else "SZ"
        items.append({"symbol": s["symbol"], "name": s["name"], "asset_type": "etf",
                      "exchange": exchange, "status": "active"})
    repo.upsert_symbols(items)
    print(f"[OK] ETF池更新: {len(items)} 只")


def cmd_fetch_daily(args):
    from data_service.market_data_service import get_market_service
    from database import repository as repo
    svc = get_market_service()
    end = date.today()
    start = end - timedelta(days=args.days)
    symbols = args.symbols or [s.symbol for s in repo.get_universe("etf")][:args.limit]
    for sym in symbols:
        bars, rep = svc.get_daily_bars(sym, start, end, "etf")
        print(f"  {sym}: {len(bars)} 条 ({rep.status})")
    print(f"[OK] 日K更新完成, {len(symbols)} 只")


def cmd_scan(args):
    from workflows.intraday_monitor_workflow import run_intraday_scan
    result = asyncio.run(run_intraday_scan(args.symbol, "", "etf", force=True))
    print(json.dumps({
        "symbol": result.get("symbol"),
        "interrupted": result.get("interrupted"),
        "chief": (result.get("chief") or {}).get("research_decision"),
        "plan_action": (result.get("plan") or {}).get("action"),
        "risk": (result.get("risk") or {}).get("risk_decision"),
        "execution": (result.get("execution") or {}).get("status"),
    }, ensure_ascii=False, indent=2))


def cmd_scan_pool(args):
    from workflows.intraday_monitor_workflow import run_pool_scan
    from data_service.market_data_service import get_market_service
    svc = get_market_service()
    spot = svc.get_etf_spot()
    min_amount = float(get_settings().get("universe.min_etf_amount", 5e7))
    top = sorted([s for s in spot if s.get("amount", 0) >= min_amount],
                 key=lambda x: x.get("amount", 0), reverse=True)[:args.top]
    symbols = [s["symbol"] for s in top]
    name_map = {s["symbol"]: s["name"] for s in top}
    results = asyncio.run(run_pool_scan(symbols, name_map, max_concurrent=args.concurrent))
    summary = {r.get("symbol"): (r.get("execution") or {}).get("status", "SKIP")
               for r in results}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_backtest(args):
    from backtest.engine import BacktestEngine
    from backtest.data_replayer import DataReplayer
    from strategies.rotation_executor import build_rotation_signal_fn
    from reports.report_generator import get_report_generator
    from notification.notification_service import get_notification_service

    symbols = args.symbols or ["510300", "159915", "512100", "159949", "588000"]
    extra_params = {"top_n": args.top_n} if args.top_n else None
    signal_fn = build_rotation_signal_fn(initial_cash=100000, params=extra_params)
    engine = BacktestEngine(date.fromisoformat(args.start),
                            date.fromisoformat(args.end),
                            name=args.name, use_agents=args.agents)
    replayer = DataReplayer(symbols)
    if args.minute:
        metrics = engine.run_minute(replayer, signal_fn)
    else:
        metrics = engine.run_daily(replayer, signal_fn)
    path = get_report_generator().generate_backtest_report(metrics)
    print(f"[OK] 回测完成: {path}")
    print(f"   总收益 {metrics['total_return']:+.2%}  年化 {metrics['annual_return']:+.2%}  "
          f"回撤 {metrics['max_drawdown']:.2%}  夏普 {metrics['sharpe']:.2f}  "
          f"交易 {metrics['trade_count']} 笔")
    if not args.no_email:
        get_notification_service().send_backtest_report_email(metrics)


def cmd_serve(args):
    from web.api.main import start_web
    start_web(port=args.port)


def cmd_scheduler(args):
    from scheduler.apscheduler_app import QuantScheduler
    sched = QuantScheduler()
    sched.start()
    print("调度器运行中(Ctrl+C 退出)...")
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sched.shutdown()


def cmd_review(args):
    from workflows.daily_review_workflow import run_daily_review
    result = asyncio.run(run_daily_review())
    print(f"[OK] 复盘完成: {result.get('report_path')}")
    print(result.get("review", {}).get("review_summary", ""))


def cmd_status(args):
    from database.db_session import db_health
    from risk.circuit_breaker import CircuitBreaker
    from workflows.intraday_monitor_workflow import get_broker
    from database import repository as repo
    cb = CircuitBreaker.instance()
    broker = get_broker()
    acc = broker.get_account()
    print(f"数据库: {'正常' if db_health() else '异常!'}")
    print(f"LLM: {'真实模型(已配置)' if get_settings().llm_configured() else '规则模拟(未配置API Key)'}")
    print(f"熔断: {'暂停中 - ' + cb.paused_reason() if cb.is_paused() else '正常'}")
    print(f"账户 {acc['account_id']}: 总资产 ¥{acc['total_asset']:,.2f}  "
          f"现金 ¥{acc['cash']:,.2f} 持仓 {len(acc['positions'])} 只")
    print(f"今日订单: {len(repo.get_orders_today(date.today()))} 笔")


def cmd_pause(args):
    from risk.circuit_breaker import CircuitBreaker
    CircuitBreaker.instance().pause(args.reason)
    print("[OK] 已暂停全部交易")


def cmd_resume(args):
    from risk.circuit_breaker import CircuitBreaker
    CircuitBreaker.instance().resume()
    print("[OK] 已恢复交易")


def cmd_confirm(args):
    from database import repository as repo
    approved = args.decision == "approve"
    repo.decide_confirmation(args.id, approved, by="cli")
    print(f"[OK] 确认 {args.id} -> {'批准' if approved else '拒绝'}")


def cmd_test_email(args):
    from notification.notification_service import get_notification_service
    svc = get_notification_service()
    ok = svc.mail._send_sync("【量化测试】邮件系统验证",
                             "<h2>多Agent量化交易系统</h2><p>邮件通道正常。</p>")
    print(f"邮件发送: {'成功' if ok else '失败(检查 .env 配置)'}")


def cmd_init_portfolio(args):
    """导入初始模拟盘持仓(重置账户后写入真实持仓)。"""
    import json as _json
    from database import repository as repo
    from database.models import Account, Position, Order, Trade, AccountSnapshot
    from paper_trading.paper_account import PaperAccount

    path = args.file
    with open(path, "r", encoding="utf-8") as f:
        data = _json.load(f)
    acc_id = data.get("account_id", "PA-001")

    # 1. 重置: 清空该账户的订单/成交/快照/持仓
    with repo.get_session() as s:
        s.query(Trade).filter(Trade.order_id.in_(
            s.query(Order.order_id).filter(Order.account_id == acc_id))).delete(synchronize_session=False)
        s.query(Order).filter_by(account_id=acc_id).delete()
        s.query(Position).filter_by(account_id=acc_id).delete()
        s.query(AccountSnapshot).filter_by(account_id=acc_id).delete()

    # 2. 账户资金
    acc = repo.get_account(acc_id)
    if acc is None:
        acc = Account(account_id=acc_id, account_type="paper", init_cash=0)
    acc.init_cash = float(data["initial_cash"])
    acc.cash = float(data["cash"])
    acc.frozen_cash = 0.0
    acc.total_pnl = 0.0
    acc.day_pnl = 0.0
    acc.total_fee = 0.0
    repo.save_account(acc)

    # 3. 写入持仓(T+1: 全部可卖)
    total_mv = 0.0
    for p in data.get("positions", []):
        pos = Position(
            position_id=f"POS-{acc_id}-{p['symbol']}",
            account_id=acc_id, symbol=p["symbol"], name=p.get("name", ""),
            total_qty=int(p["total_qty"]), available_qty=int(p["available_qty"]),
            frozen_qty=0, today_buy_qty=0,
            cost_price=float(p["cost_price"]), latest_price=float(p["latest_price"]),
            market_value=float(p["total_qty"]) * float(p["latest_price"]),
            pnl=float(p.get("pnl", 0)), pnl_pct=float(p.get("pnl_pct", 0)),
        )
        repo.save_position(pos)
        total_mv += pos.market_value

    # 4. 刷新账户市值/总资产
    acc = repo.get_account(acc_id)
    acc.market_value = total_mv
    acc.total_asset = acc.cash + acc.market_value
    repo.save_account(acc)
    print(f"[OK] 模拟盘持仓初始化完成: 账户 {acc_id}")
    print(f"     总资产 ¥{acc.total_asset:,.2f} = 现金 ¥{acc.cash:,.2f} + 市值 ¥{total_mv:,.2f}")
    print(f"     持仓 {len(data.get('positions', []))} 只")


def cmd_rag(args):
    """RAG 检索测试: python main.py rag "ETF 溢价风险" """
    from data_service.rag_service import get_rag_service
    svc = get_rag_service()
    results = svc.search(args.query)
    for r in results:
        print(f"[{r['score']:.3f}] {r['title']}")
        print(f"   {r['content'][:120]}\n")


# ================================================================
def main():
    parser = argparse.ArgumentParser(description="多Agent量化交易系统 V1")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sub.add_parser("fetch-symbols").set_defaults(fn=cmd_fetch_symbols)

    p = sub.add_parser("fetch-daily")
    p.add_argument("--symbols", nargs="*", default=[])
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_fetch_daily)

    p = sub.add_parser("scan")
    p.add_argument("symbol")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("scan-pool")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--concurrent", type=int, default=3)
    p.set_defaults(fn=cmd_scan_pool)

    p = sub.add_parser("backtest")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--symbols", nargs="*", default=[])
    p.add_argument("--name", default="")
    p.add_argument("--top-n", type=int, default=0, help="动量轮动持有数量(默认读config)")
    p.add_argument("--minute", action="store_true")
    p.add_argument("--agents", action="store_true", help="关键节点调用Agent分析")
    p.add_argument("--no-email", action="store_true")
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(fn=cmd_serve)

    sub.add_parser("scheduler").set_defaults(fn=cmd_scheduler)
    sub.add_parser("review").set_defaults(fn=cmd_review)
    sub.add_parser("status").set_defaults(fn=cmd_status)

    p = sub.add_parser("pause")
    p.add_argument("--reason", default="cli暂停")
    p.set_defaults(fn=cmd_pause)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)

    p = sub.add_parser("confirm")
    p.add_argument("id")
    p.add_argument("decision", choices=["approve", "reject"])
    p.set_defaults(fn=cmd_confirm)

    sub.add_parser("test-email").set_defaults(fn=cmd_test_email)

    p = sub.add_parser("init-portfolio", help="导入初始模拟盘持仓(data/portfolio_init.json)")
    p.add_argument("--file", default="data/portfolio_init.json")
    p.set_defaults(fn=cmd_init_portfolio)

    p = sub.add_parser("rag")
    p.add_argument("query")
    p.set_defaults(fn=cmd_rag)

    args = parser.parse_args()
    _init_logging()
    ensure_dirs()
    args.fn(args)


if __name__ == "__main__":
    main()
