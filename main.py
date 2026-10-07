# -*- coding: utf-8 -*-
"""
多Agent量化交易系统 V1 - 命令行入口
====================================
用法:
  python main.py init-db                    初始化数据库
  python main.py fetch-symbols              更新ETF池
  python main.py fetch-daily --symbols ...   拉取日K
  python main.py backfill --days 700         回填历史日K(腾讯直连, 修复回测缺失)
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
  python main.py reset-paper --yes          重置模拟盘(自动归档上一轮运行记录)
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


def cmd_backfill(args):
    """一次性回填历史日K(腾讯直连, 前复权), 修复回测行情缺失。"""
    from scripts.backfill_history import backfill_history
    result = backfill_history(days=args.days, top=args.top,
                              all_universe=args.all, symbols=args.symbols,
                              workers=args.workers)
    print(f"[OK] 回填完成: 目标 {result['total']} 只, 成功 {result['ok']}, "
          f"失败 {result['failed']}, 共写入 {result['bars']} 根K线, "
          f"耗时 {result['elapsed_seconds']}s")
    for err in result.get("errors", [])[:10]:
        print(f"  (失败) {err}")



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
    from strategies.rotation_executor import build_rotation_signal_fn, resolve_rotation_params
    from reports.report_generator import get_report_generator
    from notification.notification_service import get_notification_service

    symbols = args.symbols or ["510300", "159915", "512100", "159949", "588000"]
    extra_params = {"top_n": args.top_n} if args.top_n else None
    initial_cash = float(get_settings().get("paper_account.initial_cash", 100000))
    resolved_params = resolve_rotation_params(extra_params, use_live_preset=False)
    signal_fn = build_rotation_signal_fn(initial_cash=initial_cash, params=resolved_params)
    engine = BacktestEngine(date.fromisoformat(args.start),
                            date.fromisoformat(args.end),
                            initial_cash=initial_cash,
                            name=args.name, use_agents=args.agents)
    engine.params = resolved_params
    replayer = DataReplayer(symbols)
    if args.minute:
        metrics = engine.run_minute(replayer, signal_fn)
    else:
        metrics = engine.run_daily(replayer, signal_fn)
    if metrics.get("error"):
        print(f"[ERROR] 回测失败: {metrics['error']}")
        return
    path = get_report_generator().generate_backtest_report(metrics)
    print(f"[OK] 回测完成: {path}")
    print(f"   总收益 {metrics['total_return']:+.2%}  年化 {metrics['annual_return']:+.2%}  "
          f"回撤 {metrics['max_drawdown']:.2%}  夏普 {metrics['sharpe']:.2f}  "
          f"交易 {metrics['trade_count']} 笔")
    if not args.no_email:
        get_notification_service().send_backtest_report_email(metrics)


def cmd_serve(args):
    from web.api.main import start_web
    # 默认开启热重载(本地工具, 改代码自动生效); 需要关闭时用 --no-reload
    start_web(port=args.port, reload=not args.no_reload)


def cmd_scheduler(args):
    from scheduler.apscheduler_app import QuantScheduler
    try:
        # 防自动睡眠(电脑休眠会冻结整个系统, 调度器停摆)
        try:
            from core.power_guard import prevent_sleep
            prevent_sleep()
        except Exception:
            pass
        sched = QuantScheduler()
        started = sched.start()
        if not started:
            # 已有实例(web内嵌或其他窗口)在运行, 本窗口直接退出
            print("[INFO] 已有调度器实例在运行(data/scheduler.pid), 本窗口退出。")
            print("       Web 服务已默认内嵌调度器; 如需独立运行请先停掉其他实例。")
            return
    except Exception as exc:
        # 修复: 原实现 sched.start() 抛错时裸 traceback 直接退出, 无任何可读提示
        logger.error("调度器启动失败: %s", exc, exc_info=True)
        print(f"[ERROR] 调度器启动失败: {exc}\n"
              f"       请检查 config/agent_schedule.yaml 任务配置(如 interval_seconds 非法值)")
        sys.exit(1)
    print("调度器运行中(Ctrl+C 或直接关闭本窗口退出)...")
    import signal
    import time

    def _shutdown(*_):
        logger.info("收到退出信号, 正在关闭调度器...")
        try:
            sched.shutdown()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    # 窗口点 X 关闭时 Windows 发送 CTRL_CLOSE → SIGBREAK: 同样干净退出, 不留孤儿进程
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _shutdown)
    while True:
        time.sleep(1)


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
    archives = repo.list_paper_run_archives(limit=1)
    if archives:
        a = archives[0]
        print(f"历史运行记录: {len(repo.list_paper_run_archives(limit=200))} 条 · "
              f"最近 {a.archive_id} (至 {str(a.ended_at)[:16]}, "
              f"盈亏 {a.total_pnl:+,.2f})")
    else:
        print("历史运行记录: 无(重置模拟盘时自动归档)")


def cmd_pause(args):
    from risk.circuit_breaker import CircuitBreaker
    CircuitBreaker.instance().pause(args.reason)
    print("[OK] 已暂停全部交易")


def cmd_resume(args):
    from risk.circuit_breaker import CircuitBreaker
    CircuitBreaker.instance().resume()
    print("[OK] 已恢复交易")


def cmd_confirm(args):
    from workflows.intraday_monitor_workflow import get_broker
    from workflows.trading_workflow import resume_confirmed_plan
    approved = args.decision == "approve"
    result = asyncio.run(resume_confirmed_plan(
        args.id, approved, get_broker(), by="cli"))
    print(f"[OK] 确认 {args.id} -> {result.get('status')}: {result.get('reason', '')}")


def cmd_test_email(args):
    from notification.notification_service import get_notification_service
    svc = get_notification_service()
    ok = svc.mail._send_sync("【量化测试】邮件系统验证",
                             "<h2>多Agent量化交易系统</h2><p>邮件通道正常。</p>")
    print(f"邮件发送: {'成功' if ok else '失败(检查 .env 配置)'}")


def cmd_init_portfolio(args):
    """导入初始模拟盘持仓(先归档上一轮运行记录, 再写入真实持仓)。"""
    import json as _json
    from database import repository as repo
    from database.models import Position
    from paper_trading.paper_reset import reset_paper_account

    path = args.file
    with open(path, "r", encoding="utf-8") as f:
        data = _json.load(f)
    acc_id = data.get("account_id", "PA-001")

    # 1. 归档上一轮 + 清空订单/成交/快照/持仓(不再直接删除, 历史可回溯)
    init_cash = float(data.get("initial_cash", 0) or 0)
    if init_cash <= 0:
        total_mv = sum(int(p["total_qty"]) * float(p["latest_price"])
                       for p in data.get("positions", []))
        init_cash = round(float(data["cash"]) + total_mv, 2)
    result = reset_paper_account(initial_cash=init_cash,
                                 note=f"导入持仓 {path}",
                                 run_name="导入真实持仓前",
                                 account_id=acc_id)
    print(f"[OK] 上一轮已归档: {result['archive_id']}")

    # 2. 写入持仓(T+1: 全部可卖; 峰值价=成本价, 移动止盈以此为起点)
    total_mv = 0.0
    for p in data.get("positions", []):
        cost = float(p["cost_price"])
        last = float(p["latest_price"])
        qty = int(p["total_qty"])
        total_mv += qty * last
        pos = Position(
            position_id=f"POS-{acc_id}-{p['symbol']}",
            account_id=acc_id, symbol=p["symbol"], name=p.get("name", ""),
            total_qty=qty, available_qty=int(p["available_qty"]),
            frozen_qty=0, today_buy_qty=0,
            cost_price=cost, latest_price=last,
            market_value=round(qty * last, 2),
            pnl=round(qty * last - qty * cost, 2),
            pnl_pct=round((last / cost - 1) if cost else 0, 4),
            peak_price=cost if cost else last,
        )
        repo.save_position(pos)

    # 3. 刷新账户市值/总资产/盈亏(现金按导入文件回填, 不能沿用重置后的初始资金)
    acc = repo.get_account(acc_id)
    acc.cash = round(float(data["cash"]), 2)
    acc.frozen_cash = 0.0
    acc.total_fee = 0.0
    acc.init_cash = round(init_cash, 2)
    acc.market_value = round(total_mv, 2)
    acc.total_asset = round(acc.cash + acc.market_value, 2)
    acc.total_pnl = round(acc.total_asset - acc.init_cash, 2)
    acc.day_pnl = 0.0
    repo.save_account(acc)
    print(f"[OK] 模拟盘持仓初始化完成: 账户 {acc_id}")
    print(f"     总资产 ¥{acc.total_asset:,.2f} = 现金 ¥{acc.cash:,.2f} + 市值 ¥{total_mv:,.2f}")
    print(f"     初始资金(现金+持仓成本) ¥{acc.init_cash:,.2f} · 当前盈亏 ¥{acc.total_pnl:,.2f}")
    print(f"     持仓 {len(data.get('positions', []))} 只")


def cmd_reset_paper(args):
    """重置模拟盘(自动归档上一轮运行记录)。"""
    from paper_trading.paper_reset import reset_paper_account
    if not args.yes:
        print("该操作会清空当前模拟盘交易数据(重置前自动归档运行记录)。")
        print("确认请追加 --yes, 例如: python main.py reset-paper --yes")
        return
    result = reset_paper_account(initial_cash=args.initial_cash, note=args.note,
                                 run_name=args.name)
    s = result["summary"]
    print(f"[OK] 模拟盘已重置: 账户 {result['account_id']} 初始资金 ¥{result['initial_cash']:,.2f}")
    print(f"     上轮运行记录已归档: {result['archive_id']}")
    print(f"     上轮总资产 ¥{s['final_asset']:,.2f}  盈亏 {s['total_pnl']:+,.2f} "
          f"({s['total_return']:+.2%})  最大回撤 {s['max_drawdown']:.2%}")
    print(f"     清理: {result['purged']}")
    print(f"     备份文件: {result['export_path']}")



def cmd_rotate(args):
    """手动执行一轮 ETF 动量轮动(与回测共用信号函数, 落单到模拟盘)。"""
    from strategies.live_rotation import run_live_rotation
    result = run_live_rotation(notify=False)
    orders = result.get("orders") or []
    print(f"轮动信号 {len(result.get('signals', {}))} 个, 下单 {len(orders)} 笔")
    for o in orders:
        print(f"  {o['side']} {o['symbol']} {o['qty']}份 @ {o['price']:.3f} — {o['reason']}")
    for s in result.get("skipped", []):
        print(f"  (跳过) {s}")


def cmd_universe(args):
    """查看当前动态ETF候选池(母池规模/本期成员/入选理由/持仓重叠)。"""
    from core.timeutil import today as biz_today
    from strategies.dynamic_etf_universe import (
        build_current_paper_snapshot, dynamic_pool_config, latest_paper_universe,
    )
    from database import repository as repo
    today = biz_today()
    cfg = dynamic_pool_config()
    mother = repo.get_etf_history_symbols(today, min_bars=20, recent_days=15)
    print(f"母池(全市场有近15日行情的ETF): {len(mother)} 只")
    print(f"池配置: 每期最多 {cfg['max_candidates']} 只(两阶段筛选), "
          f"流动性下限 {cfg['min_avg_amount']/1e4:.0f}万, 波动率上限 {cfg['max_annualized_volatility']:.0%}, "
          f"排除债券/货币ETF={cfg.get('exclude_cash_like')}")
    snap = latest_paper_universe(today)
    if args.refresh:
        snap = build_current_paper_snapshot(today, cfg, force=True)
    elif not snap:
        print("(库中暂无本期候选池快照, 使用 --refresh 生成)")
    if snap:
        members = snap.get("members") or []
        holdings = {p.symbol for p in repo.get_positions()}
        print(f"\n本期候选池 {snap.get('snapshot_id')} "
              f"asof={snap.get('asof_date')} effective={snap.get('effective_date')} "
              f"共 {len(members)} 只 (★=当前持仓)")
        print(f"{'#':>3} {'代码':<8} {'名称':<16} {'20日均额(万)':>12} {'年化波动':>8} {'覆盖率':>7} {'主题':<12}")
        for m in members:
            star = "★" if str(m.get("symbol")) in holdings else " "
            print(f"{m.get('rank', 0):>3} {star}{str(m.get('symbol')):<7} "
                  f"{str(m.get('name') or '')[:14]:<16} "
                  f"{(m.get('avg_amount') or 0)/1e4:>12,.0f} "
                  f"{(m.get('annualized_volatility') or 0)*100:>7.1f}% "
                  f"{(m.get('recent_coverage') or 0)*100:>6.1f}% "
                  f"{str(m.get('theme') or '')[:12]:<12}")
        inside = [m for m in members if str(m.get("symbol")) in holdings]
        print(f"\n策略实际持仓 {len(holdings)} 只, 其中 {len(inside)} 只在候选池内")



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

    p = sub.add_parser("backfill", help="回填历史日K(腾讯直连, 修复回测数据缺失)")
    p.add_argument("--days", type=int, default=700, help="回填天数(默认700)")
    p.add_argument("--top", type=int, default=150, help="按成交额回填Top N ETF")
    p.add_argument("--all", action="store_true", help="回填全部已登记标的(较慢)")
    p.add_argument("--symbols", nargs="*", default=[], help="额外指定标的")
    p.add_argument("--workers", type=int, default=4, help="并发数(默认4)")
    p.set_defaults(fn=cmd_backfill)

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
    p.add_argument("--no-reload", action="store_true",
                   help="关闭热重载(默认开启: 修改 .py 代码自动重启)")
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

    p = sub.add_parser("reset-paper", help="重置模拟盘(自动归档上一轮运行记录)")
    p.add_argument("--initial-cash", type=float, default=None,
                   help="新初始资金(默认读 config.yaml paper_account.initial_cash)")
    p.add_argument("--note", default="", help="重置备注")
    p.add_argument("--name", default="", help="本轮运行名称")
    p.add_argument("--yes", action="store_true", help="确认执行")
    p.set_defaults(fn=cmd_reset_paper)

    p = sub.add_parser("rotate", help="手动执行一轮ETF动量轮动(回测策略实盘落地)")
    p.set_defaults(fn=cmd_rotate)

    p = sub.add_parser("universe", help="查看当前动态ETF候选池/母池/持仓重叠")
    p.add_argument("--refresh", action="store_true", help="强制重新生成候选池快照")
    p.set_defaults(fn=cmd_universe)

    p = sub.add_parser("rag")
    p.add_argument("query")
    p.set_defaults(fn=cmd_rag)

    args = parser.parse_args()
    _init_logging()
    ensure_dirs()
    args.fn(args)


if __name__ == "__main__":
    main()
