# -*- coding: utf-8 -*-
"""
APScheduler 调度器
==================
按 agent_schedule.yaml 配置注册任务:
交易日历/标的池/盘前新闻/实时行情/盘口/资金流/舆情/Agent常规分析/收盘更新/日报/周报
非交易时段自动跳过(only_trading_hours)。
"""
import asyncio
import logging
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import get_settings

logger = logging.getLogger("scheduler")


def _in_trading_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
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
    def start(self):
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
            else:
                seconds = int(jc.get("interval_seconds", 60) or 0)
                if jc.get("interval_minutes"):
                    seconds = int(jc["interval_minutes"]) * 60
                trigger = IntervalTrigger(seconds=seconds)
            only_trading = jc.get("only_trading_hours", False)

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
                id=job_name, name=job_name, misfire_grace_time=300)
            registered += 1
        self.sched.start()
        logger.info("调度器启动, 已注册 %d 个任务", registered)
        for name, job in self._jobs.items():
            logger.info("  任务 %s: next_run=%s", name, job.next_run_time)

    def shutdown(self):
        if self.sched.running:
            self.sched.shutdown(wait=False)

    # ------------------------------------------------------------------
    # 任务实现
    # ------------------------------------------------------------------
    def job_update_trade_calendar(self):
        """交易日历更新(每日08:00)。"""
        from data_service.market_data_service import get_market_service
        from datetime import timedelta
        svc = get_market_service()
        today = date.today()
        dates = svc.get_trade_calendar(today - timedelta(days=30), today + timedelta(days=90))
        logger.info("交易日历更新: %d 个交易日", len(dates))

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
        """Agent 常规分析(默认30分钟): 扫描监控池(enabled标的, 按优先级/顺序限流)。"""
        from workflows.intraday_monitor_workflow import run_pool_scan
        from core.config import get_settings as gs
        from database import repository as repo
        interval = int(gs().get("intraday.analysis_interval_min", 30))
        watch = repo.get_watchlist(enabled_only=True)
        # 持仓优先(priority已加权), 每轮最多扫描 max_symbols 只(限流)
        max_syms = int(gs().get("universe.max_symbols_per_scan", 20))
        targets = [w for w in watch if w["enabled"]][:max_syms]
        if not targets:
            logger.info("监控池为空, 跳过常规分析")
            return
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
        """收盘数据更新(15:10): 日K落库。"""
        from datetime import timedelta
        from data_service.market_data_service import get_market_service
        svc = get_market_service()
        for s in self._current_universe(50):
            try:
                svc.get_daily_bars(s["symbol"], date.today() - timedelta(days=30),
                                   date.today(), "etf")
            except Exception:
                pass
        logger.info("收盘数据更新完成")

    def job_daily_report(self):
        """日终报告(17:00)。"""
        import asyncio
        from workflows.daily_review_workflow import run_daily_review
        asyncio.run(run_daily_review())

    def job_weekly_report(self):
        """周报(周五18:00)。"""
        from reports.report_generator import get_report_generator
        path = get_report_generator().generate_weekly_report()
        logger.info("周报生成: %s", path)


_scheduler: Optional[QuantScheduler] = None


def get_scheduler() -> QuantScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = QuantScheduler()
    return _scheduler
