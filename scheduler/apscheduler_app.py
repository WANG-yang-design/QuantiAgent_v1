# -*- coding: utf-8 -*-
"""
APScheduler 调度器
==================
按 agent_schedule.yaml 配置注册任务:
交易日历/标的池/盘前新闻/实时行情/盘口/资金流/舆情/Agent常规分析/收盘更新/日报/周报
非交易时段自动跳过(only_trading_hours)。

进程模型(修复: 以前只靠 start.bat 单独开一个调度器窗口, 窗口一关/重启时
只起 web 忘起调度器, 就出现"跑了一天一个决策链都没有"):
  - 支持"内嵌模式": `main.py serve`(web) 启动时自动在后台线程拉起调度器;
  - 单例锁(data/scheduler.pid): 全系统只允许一个调度器实例, 重复启动自动退出;
  - 错过任务补跑: interval 任务 coalesce=True + misfire_grace_time 1小时,
    电脑休眠错过的时间窗口在唤醒后补执行一轮, 不再静默丢弃。
"""
import asyncio
import logging
import os
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import get_settings, ROOT_DIR
from data_service.market_data_service import get_market_service

logger = logging.getLogger("scheduler")

_LOCK_FILE = ROOT_DIR / "data" / "scheduler.pid"


def _process_alive(pid: int) -> bool:
    """进程是否存在(Windows/Linux 通用)。
    修复: Python 3.13 在 Windows 上 os.kill(pid, 0) 抛 WinError 87(不支持信号0),
    导致单例锁/状态检查恒判"进程已死"。改用 OpenProcess+GetExitCodeProcess。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_scheduler_lock() -> bool:
    """获取调度器单例锁(pid 文件)。已有存活实例返回 False。"""
    try:
        if _LOCK_FILE.exists():
            old = int(_LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
            if _process_alive(old):
                return False
            logger.warning("发现失效调度器pid %s, 接管锁", old)
        _LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception as exc:
        logger.warning("调度器锁写入失败: %s(继续启动)", exc)
        return True


def release_scheduler_lock():
    try:
        if _LOCK_FILE.exists():
            pid = int(_LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
            if pid == os.getpid():
                _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _in_trading_hours(now: Optional[datetime] = None) -> bool:
    """交易时段判断: 统一使用配置时区(Asia/Shanghai)。
    修复: 原实现用服务器本地时区, UTC 服务器上'仅交易时段'任务全部错判。"""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(get_settings().get("agent_schedule.timezone", "Asia/Shanghai"))
    except Exception:
        tz = None
    now = now or (datetime.now(tz) if tz else datetime.now())
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (time(9, 30) <= t <= time(11, 30)) or (time(13, 0) <= t <= time(15, 0))


class QuantScheduler:
    """量化系统调度器。"""

    def __init__(self):
        self.settings = get_settings()
        self.cfg = self.settings.section("agent_schedule")
        self.sched = BackgroundScheduler(timezone=self.cfg.get("timezone", "Asia/Shanghai"))
        self._jobs: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    def start(self) -> bool:
        """启动调度器。返回 False 表示已有实例在运行(单例锁)。"""
        if not acquire_scheduler_lock():
            logger.warning("已有调度器实例在运行(data/scheduler.pid), 本实例退出")
            return False
        jobs_cfg = self.cfg.get("jobs", {})
        registered = 0
        for job_name, jc in jobs_cfg.items():
            if not jc.get("enabled", True):
                continue
            handler = getattr(self, f"job_{job_name}", None)
            if handler is None:
                logger.warning("未实现的任务: %s", job_name)
                continue
            if jc.get("cron"):
                trigger = CronTrigger.from_crontab(jc["cron"],
                                                   timezone=self.cfg.get("timezone", "Asia/Shanghai"))
            elif job_name == "agent_routine_analysis" and jc.get("interval_minutes"):
                # 例行分析: 与时钟对齐(修复: 原用 IntervalTrigger 相对启动时间计时,
                # 9:47 启动就 10:17 才扫; 且程序重启后重新计时)。
                # 现转成绝对时刻 cron: */N 9-15(交易时段) —— 每天 9:30 开盘后
                # 整点对齐: N=30 → 9:30/10:00/10:30/.../15:00, 重启不影响时间表。
                # 非交易时段(9:00/12:00/15:30)的触发由 only_trading_hours 过滤。
                n = max(int(jc["interval_minutes"]), 5)
                cron_expr = f"*/{n} 9-15 * * 1-5"
                trigger = CronTrigger.from_crontab(
                    cron_expr, timezone=self.cfg.get("timezone", "Asia/Shanghai"))
                logger.info("例行分析任务改为时钟对齐: %s (interval_minutes=%d)",
                            cron_expr, n)
            else:
                seconds = int(jc.get("interval_seconds", 60) or 0)
                if jc.get("interval_minutes"):
                    seconds = int(jc["interval_minutes"]) * 60
                if seconds <= 0 and job_name == "position_monitor":
                    # 持仓巡检间隔从 risk_limits.yaml 读取(可调)
                    seconds = int(get_settings().get(
                        "risk.position_monitor.check_interval_seconds", 300) or 300)
                if seconds <= 0:
                    logger.warning("任务 %s 间隔配置无效(%s), 使用默认60秒",
                                   job_name, jc.get("interval_seconds"))
                    seconds = 60
                trigger = IntervalTrigger(seconds=seconds)
            only_trading = jc.get("only_trading_hours", False)
            # 修复: 电脑休眠/进程卡顿错过的时间窗口, coalesce 补跑一次
            # (misfire_grace_time 1小时, 避免醒来后 30 分钟分析整日缺失)
            is_interval = not jc.get("cron")

            def wrap(fn, trading_only: bool):
                def runner():
                    if trading_only and not _in_trading_hours():
                        logger.debug("非交易时段, 跳过 %s", getattr(fn, "__name__", "task"))
                        return
                    try:
                        fn()
                    except Exception as exc:
                        logger.error("调度任务异常 %s: %s", getattr(fn, "__name__", "task"), exc)
                return runner

            self._jobs[job_name] = self.sched.add_job(
                wrap(handler, only_trading), trigger,
                id=job_name, name=job_name, misfire_grace_time=3600,
                coalesce=is_interval)
            registered += 1
        # 心跳: 每分钟落盘, 供 Web 显示调度器存活状态
        self.sched.add_job(self.job_heartbeat, "interval", seconds=60,
                           id="heartbeat", name="heartbeat",
                           misfire_grace_time=300, coalesce=True)
        self.sched.start()
        logger.info("调度器启动, 已注册 %d 个任务", registered)
        for name, job in self._jobs.items():
            logger.info("  任务 %s: next_run=%s", name, job.next_run_time)
        return True

    def shutdown(self):
        release_scheduler_lock()
        if self.sched.running:
            self.sched.shutdown(wait=False)

    # ------------------------------------------------------------------
    def job_heartbeat(self):
        """心跳落盘(Web 端据此判断调度器是否存活)。"""
        import json
        try:
            path = ROOT_DIR / "data" / "scheduler_heartbeat.json"
            jobs = []
            for name, job in self._jobs.items():
                jobs.append({"name": name,
                             "next_run": str(job.next_run_time) if job.next_run_time else ""})
            path.write_text(json.dumps({
                "pid": os.getpid(),
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "jobs": jobs,
            }, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning("心跳落盘失败: %s", exc)

    # ------------------------------------------------------------------
    # 任务实现
    # ------------------------------------------------------------------
    def job_update_trade_calendar(self):
        """交易日历更新(每日08:00): 拉取近30天+未来90天并落盘,
        供 is_trade_day/回测复用(修复: 原实现拉取后直接丢弃, 任务空转)。"""
        from data_service.market_data_service import get_market_service
        from datetime import timedelta
        import json
        from core.config import ROOT_DIR
        svc = get_market_service()
        today = date.today()
        dates = svc.get_trade_calendar(today - timedelta(days=30), today + timedelta(days=90))
        if not dates:
            logger.warning("交易日历更新: 数据源返回空, 保留旧缓存")
            return
        try:
            path = ROOT_DIR / "data" / "trade_calendar.json"
            path.write_text(json.dumps([str(d) for d in dates]),
                            encoding="utf-8")
            logger.info("交易日历更新: %d 个交易日 → %s", len(dates), path)
        except Exception as exc:
            logger.warning("交易日历落盘失败: %s", exc)

    def job_update_symbols(self):
        """ETF/股票基础信息(每日08:10) + 热门ETF自动加入监控池。"""
        from database import repository as repo
        svc = get_market_service()
        spot = svc.get_etf_spot()
        symbols = []
        for s in spot:
            exchange = "SH" if s["symbol"].startswith(("5", "6", "9")) else "SZ"
            repo.upsert_symbols([{
                "symbol": s["symbol"], "name": s["name"],
                "asset_type": "etf", "exchange": exchange, "status": "active",
            }])
            symbols.append(s["symbol"])
        # 热门ETF自动加入监控池(成交额Top20, 分类=hot)
        min_amount = float(self.settings.get("universe.min_etf_amount", 5e7))
        hot = sorted([s for s in spot if s.get("amount", 0) >= min_amount],
                     key=lambda x: x.get("amount", 0), reverse=True)[:20]
        for s in hot:
            repo.upsert_watch_item(s["symbol"], s["name"], "etf",
                                   categories=["hot"], enabled=True, priority=10)
        logger.info("ETF池更新: %d 只, 热门监控加入 %d 只", len(symbols), len(hot))

    def job_premarket_news(self):
        """盘前新闻公告扫描(08:30)。"""
        symbols = [s["symbol"] for s in self._current_universe(20)]
        from data_service.news_service import get_news_service
        added = get_news_service().fetch_and_store_news(symbols)
        added += get_news_service().fetch_and_store_announcements(symbols)
        logger.info("盘前新闻公告: 新增 %d 条", added)

    def _current_universe(self, limit: int = 20) -> List[dict]:
        """当前标的池(按成交额取前N)。"""
        svc = get_market_service()
        spot = svc.get_etf_spot()
        min_amount = float(self.settings.get("universe.min_etf_amount", 5e7))
        top = sorted([s for s in spot if s.get("amount", 0) >= min_amount],
                     key=lambda x: x.get("amount", 0), reverse=True)
        return top[:limit]

    def job_collect_realtime_quote(self):
        """实时行情采集(交易时段每10秒, 池内标的)。"""
        from data_service.market_data_service import get_market_service
        svc = get_market_service()
        uni = self._current_universe(20)
        for s in uni[:5]:   # 限流: 每轮5只
            try:
                svc.get_realtime_quote(s["symbol"], "etf")
            except Exception as exc:
                logger.warning("实时行情采集失败 %s: %s", s["symbol"], exc)

    def job_collect_order_book(self):
        """盘口快照(每60秒落库)。"""
        svc = get_market_service()
        for s in self._current_universe(10):
            try:
                svc.get_order_book(s["symbol"], "etf")
            except Exception as exc:
                logger.warning("盘口采集失败 %s: %s", s["symbol"], exc)

    def job_collect_money_flow(self):
        """资金流采集(每5分钟)。"""
        svc = get_market_service()
        for s in self._current_universe(5):
            try:
                svc.get_money_flow(s["symbol"], "etf")
            except Exception:
                pass

    def job_scan_news(self):
        """舆情新闻扫描(每10分钟)。"""
        from data_service.news_service import get_news_service
        ns = get_news_service()
        uni = self._current_universe(10)
        ns.fetch_and_store_news([s["symbol"] for s in uni])
        ns.fetch_and_store_sentiment([s["symbol"] for s in uni])

    def job_agent_routine_analysis(self):
        """Agent 常规分析(默认30分钟): 扫描监控池(enabled标的, 按优先级/顺序限流)。
        修复: 原实现每轮把监控池全部标的全量分析(20标的×11 Agent=220次LLM调用/轮,
        token 成本高)。smart_scan=true 时分层:
          Tier1(每轮必扫): 当前持仓 + 近3日轮动策略交易过的标的(买入的需持续跟踪)
          Tier2(轮询):     其余监控标的, 每 tier2_interval_rounds 轮覆盖一遍
        """
        from workflows.intraday_monitor_workflow import run_pool_scan
        from database import repository as repo
        from core.agent_switch import agent_enabled  # noqa: F401  (确保配置加载)
        # 修复: 原实现读 config.yaml 的 intraday.analysis_interval_min 只用于日志,
        # 实际间隔由 yaml 固定 30 分钟, 改配置无效 —— 改为读调度配置本身。
        interval = int(self.cfg.get("jobs", {}).get(
            "agent_routine_analysis", {}).get("interval_minutes", 30))
        watch = repo.get_watchlist(enabled_only=True)
        max_syms = int(self.settings.get("universe.max_symbols_per_scan", 20))
        targets = [w for w in watch if w["enabled"]][:max_syms]
        if not targets:
            logger.info("监控池为空, 跳过常规分析")
            return

        # ---- 智能分层扫描(成本控制) ----
        smart = bool(self.settings.get("universe.smart_scan", True))
        if smart:
            try:
                from workflows.intraday_monitor_workflow import get_broker
                broker = get_broker()
                tier1 = {p["symbol"] for p in broker.get_positions()}
                # 近3日轮动策略交易过的标的(买入标的需持续跟踪)
                from datetime import datetime as _dt, timedelta as _td
                since = _dt.now() - _td(days=3)
                for o in broker.get_orders():
                    if str(o.get("order_intent_id", "")).startswith("INTENT-ROT-") \
                            and o.get("submit_time") and o.get("submit_time") >= since:
                        tier1.add(o.get("symbol", ""))
                scan = [w for w in targets if w["symbol"] in tier1]
                tier2 = [w for w in targets if w["symbol"] not in tier1]
                # Tier2 轮询: 每轮取一段, 游标循环覆盖
                if tier2:
                    round_no = getattr(self, "_scan_round", 0) + 1
                    self._scan_round = round_no
                    per_round = max(int(self.settings.get(
                        "universe.tier2_max_per_round", 8)), 1)
                    n2 = len(tier2)
                    cursor = getattr(self, "_tier2_cursor", 0) % max(n2, 1)
                    picked = tier2[cursor:cursor + per_round]
                    self._tier2_cursor = (cursor + per_round) % max(n2, 1)
                    scan += picked
                    logger.info("智能扫描: 持仓/轮动%d只 + 轮询%d只(第%d轮)",
                                len(tier1), len(picked), round_no)
                else:
                    logger.info("智能扫描: 全部为持仓/轮动标的(%d只)", len(scan))
                targets = scan[:max_syms]
            except Exception as exc:
                logger.warning("智能扫描分层失败, 全量扫描: %s", exc)

        symbols = [w["symbol"] for w in targets]
        name_map = {w["symbol"]: w["name"] for w in targets}
        import asyncio
        try:
            results = asyncio.run(run_pool_scan(symbols, name_map, max_concurrent=3))
            done = sum(1 for r in results if r.get("chief"))
            logger.info("常规分析完成: 扫描%d只, 产生研究结论%d个(间隔%d分钟)",
                        len(symbols), done, interval)
        except Exception as exc:
            logger.error("常规分析失败: %s", exc)

    def job_eod_data_update(self):
        """收盘数据更新(15:10): 日K落库。
        修复: ①强制绕过缓存(use_cache=False), 原实现命中1小时缓存导致
        显示"已更新"但数据是旧的; ②失败不再静默吞掉(except: pass)。"""
        from datetime import timedelta
        from data_service.market_data_service import get_market_service
        svc = get_market_service()
        ok = 0
        for s in self._current_universe(50):
            try:
                bars, rep = svc.get_daily_bars(
                    s["symbol"], date.today() - timedelta(days=30),
                    date.today(), "etf", use_cache=False)
                if bars:
                    ok += 1
                elif rep.status in ("MISSING", "DELAYED"):
                    logger.warning("收盘数据更新失败 %s: %s", s["symbol"], rep.status)
            except Exception as exc:
                logger.warning("收盘数据更新异常 %s: %s", s["symbol"], exc)
        logger.info("收盘数据更新完成: %d/%d 只成功", ok, min(len(self._current_universe(50)), 50))

    def job_daily_report(self):
        """日终报告(17:00)。"""
        import asyncio
        from workflows.daily_review_workflow import run_daily_review
        asyncio.run(run_daily_review())

    def job_account_snapshot(self):
        """账户净值快照(交易时段每30分钟): 净值曲线数据来源。
        修复: 此前只在日终复盘(17:00)与手动"记录快照"时落库,
        一天只有1-2个点, 仪表盘净值曲线画成一条直线。"""
        from workflows.intraday_monitor_workflow import get_broker
        from data_service.market_data_service import get_market_service
        broker = get_broker()
        try:
            # 先刷新持仓现价(市值/总资产才是实时的)
            positions = broker.get_positions()
            if positions:
                prices: Dict[str, float] = {}
                for p in positions[:20]:
                    try:
                        quote, _ = get_market_service().get_realtime_quote(
                            p["symbol"], "etf")
                        price = float((quote or {}).get("latest_price", 0) or 0)
                        if price > 0:
                            prices[p["symbol"]] = price
                    except Exception:
                        continue
                if prices:
                    broker.mark_to_market(prices)
            broker.snapshot()
            acc = broker.get_account()
            logger.info("账户快照: 总资产 ¥%.2f (持仓%d只)",
                        float(acc.get("total_asset", 0) or 0),
                        len(acc.get("positions") or []))
        except Exception as exc:
            logger.warning("账户快照失败: %s", exc)

    def job_market_diagnosis(self):
        """市场牛熊诊断(交易时段每小时, 落库)。
        修复: 原实现硬编码 localhost:8080 与 Bearer 明文 token, 换端口/改token后静默失效。"""
        import httpx
        from core.config import get_settings as _gs
        web = _gs().section("web")
        base = f"http://127.0.0.1:{int(web.get('port', 8080))}"
        token = _gs().get("web.admin_token", "quantiagent-admin")
        try:
            resp = httpx.post(f"{base}/api/market/diagnosis?refresh=1",
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=60)
            if resp.status_code == 200:
                d = resp.json()
                logger.info("市场诊断更新: %s (%s)", d.get("label"), d.get("state"))
            else:
                logger.warning("市场诊断任务返回 %s", resp.status_code)
        except Exception as exc:
            logger.warning("市场诊断任务失败: %s", exc)

    def job_position_monitor(self):
        """持仓风控巡检(交易时段, 间隔见 risk_limits.yaml)。
        修复: 除配置 only_trading_hours 外再显式校验 —— 旧配置启动的调度器
        进程(配置未热加载)会在午休/收盘后继续自动卖持仓, 用户莫名发现
        "订单全部成交/持仓变化"。"""
        if not _in_trading_hours():
            return
        from risk.position_monitor import get_position_monitor
        try:
            r = get_position_monitor().check_once()
            if r.get("executed"):
                logger.warning("持仓巡检执行了 %d 笔止损/止盈/降仓单", len(r["executed"]))
            elif r.get("triggered"):
                logger.info("持仓巡检触发 %d 笔(未执行: %s)",
                            len(r["triggered"]), r.get("skipped")[:3])
        except Exception as exc:
            logger.error("持仓巡检异常: %s", exc, exc_info=True)

    def job_match_pending_orders(self):
        """盘中订单撮合+超时撤单: 对未成交订单用最新行情撮合。
        修复: 此前 match_order 无生产调用方, 下单后订单永远停在 SUBMITTED,
        现金/股数被永久冻结。"""
        # 修复: 显式校验交易时段(不依赖配置 only_trading_hours)——
        # 用旧配置启动的调度器进程会在午休/收盘后按缓存行情"撮合"未成交单,
        # 造成限价单在非交易时段成交(如 12:2x / 17:5x 的 FILLED)。
        if not _in_trading_hours():
            return
        from datetime import date as _date
        from data_service.market_data_service import get_market_service
        from workflows.intraday_monitor_workflow import get_broker
        try:
            broker = get_broker()
            active = [o for o in broker.get_orders()
                      if o.get("status") in ("SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED")]
            if not active:
                return
            svc = get_market_service()
            for o in active:
                try:
                    quote, _ = svc.get_realtime_quote(o.get("symbol", ""), "etf")
                    price = float((quote or {}).get("latest_price", 0) or 0)
                    if price <= 0:
                        continue
                    # 行情时效性(修复): 数据源缓存/故障时返回的旧价不能用于撮合
                    # —— 曾出现午休/收盘后用缓存价"撮合"限价单, 订单被瞬间成交
                    qtime = (quote or {}).get("quote_time")
                    if isinstance(qtime, datetime):
                        if (datetime.now() - qtime).total_seconds() > 120:
                            continue
                    elif isinstance(qtime, str):
                        try:
                            qt = datetime.fromisoformat(qtime.replace("Z", "+00:00"))
                            if (datetime.now() - qt).total_seconds() > 120:
                                continue
                        except Exception:
                            pass
                    bar = {"open": price, "high": price, "low": price,
                           "close": price, "volume": 0, "amount": 0,
                           "trade_date": _date.today()}
                    # 限价单走 medium(尊重限价约束), 市价单走 simple(现价±滑点)
                    mode = "medium" if o.get("order_type") == "LIMIT" else "simple"
                    trades = broker.match_order(o["order_id"], bar, mode=mode)
                    if trades:
                        logger.info("盘中撮合成交 %s: %s %d股 @%.3f (订单%s)",
                                    o["order_id"], o.get("side"),
                                    trades[0]["qty"], trades[0]["price"],
                                    o.get("symbol"))
                except Exception as exc:
                    logger.warning("盘中撮合失败 %s(%s): %s",
                                   o.get("order_id"), o.get("symbol"), exc)
            # 超时未成交撤单(未成交限价单)
            try:
                cancelled = broker.cancel_stale_orders()
                if cancelled:
                    logger.info("超时撤单 %d 笔: %s",
                                len(cancelled),
                                [c["order_id"] for c in cancelled])
            except Exception as exc:
                logger.warning("超时撤单异常: %s", exc)
            # 单日亏损熔断检查(day_pnl 由账户维护, 每日开盘重置基准)
            try:
                from risk.circuit_breaker import CircuitBreaker
                acc = broker.get_account()
                CircuitBreaker.instance().check_daily_loss(
                    float(acc.get("day_pnl", 0) or 0),
                    float(acc.get("total_asset", 0) or 0))
            except Exception as exc:
                logger.warning("日亏损熔断检查异常: %s", exc)
        except Exception as exc:
            logger.error("订单撮合任务异常: %s", exc, exc_info=True)

    def job_weekly_report(self):
        """周报(周五18:00)。"""
        from reports.report_generator import get_report_generator
        path = get_report_generator().generate_weekly_report()
        logger.info("周报生成: %s", path)

    def job_strategy_rotation(self):
        """ETF动量轮动实盘执行(收盘前, 修复: 回测策略此前从未应用于实盘)。
        与回测共用同一信号函数(rotation_executor), 按信号提交订单,
        受熔断保护; 是否启用见 config.yaml strategies.live_rotation。"""
        from strategies.live_rotation import run_live_rotation
        try:
            result = run_live_rotation()
            n = len(result.get("orders") or [])
            logger.info("轮动执行完成: 下单%d笔 %s",
                        n, result.get("skipped", [])[:3])
        except Exception as exc:
            logger.error("轮动执行异常: %s", exc, exc_info=True)

    def job_monthly_report(self):
        """月报(每月1日18:00, 生成上月)。"""
        from reports.report_generator import get_report_generator
        today = date.today()
        prev = (today.replace(day=1) - timedelta(days=1))
        path = get_report_generator().generate_monthly_report(
            prev.year, prev.month)
        logger.info("月报生成: %s", path)

    def job_annual_report(self):
        """年报(每年1月2日18:00, 生成上一年)。"""
        from reports.report_generator import get_report_generator
        today = date.today()
        path = get_report_generator().generate_annual_report(today.year - 1)
        logger.info("年报生成: %s", path)


_scheduler: Optional[QuantScheduler] = None


def get_scheduler() -> QuantScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = QuantScheduler()
    return _scheduler
