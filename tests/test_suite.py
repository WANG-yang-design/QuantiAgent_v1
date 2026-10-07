# -*- coding: utf-8 -*-
"""
自动化测试套件
==============
运行: python -m tests.test_suite
覆盖: 技术指标 / 撮合 / T+1 / 风控五层 / 回测指标 / 数据质量 / 工作流冒烟
"""
import asyncio
import os
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# 修复: 测试套件会真实下单/撮合成交, 成交后订单层会发"模拟成交确认"邮件
# (用户手机收到过 N 封"BUY 510300 1000份"的邮件, 全是跑测试触发的)。
# 测试必须静默: 禁用邮件 + 跳过 PA-TEST 账户的通知。
os.environ["EMAIL_ENABLED"] = "false"

from core.logging import setup_logging

setup_logging("WARNING")


# ================================================================
# 1. 技术指标
# ================================================================
class TestTechnicalIndicators(unittest.TestCase):
    def _bars(self, n=120):
        bars = []
        base = 3.0
        d = date.today() - timedelta(days=n + 20)
        for i in range(n):
            d = d + timedelta(days=1)
            if d.weekday() >= 5:
                continue
            close = base + i * 0.01
            bars.append({
                "symbol": "510300", "trade_date": d,
                "open": close - 0.01, "high": close + 0.02,
                "low": close - 0.02, "close": close,
                "volume": 1e6 + i * 1000, "amount": (1e6 + i * 1000) * close,
            })
        return bars

    def test_features(self):
        from features.technical_indicators import compute_technical_features
        f = compute_technical_features(self._bars())
        self.assertIn("ma20", f)
        self.assertGreater(f["ma20"], 0)
        self.assertIn("rsi", f)
        self.assertIn("momentum_20d", f)
        # 修复: 原断言 `0 <= x or x < 1` 恒为真, 动量计算错误无法被发现。
        # 测试数据为单调上涨序列(120天, 每日+0.01), 20日动量应显著为正。
        self.assertGreater(f["momentum_20d"], 0.05)

    def test_market_summary(self):
        from features.technical_indicators import compute_technical_features
        from features.market_state import build_market_summary
        f = compute_technical_features(self._bars())
        s = build_market_summary("510300", "沪深300ETF", f)
        self.assertIn("510300", s)
        self.assertIn("均线", s)


class TestSchedulerConfiguration(unittest.TestCase):
    def test_hourly_agent_cron_contains_only_real_session_runs(self):
        from scheduler.apscheduler_app import _routine_analysis_crons
        self.assertEqual(_routine_analysis_crons(60),
                         ["0 10,11,13,14,15 * * mon-fri"])

    def test_half_hour_compatibility_has_no_fake_session_runs(self):
        from scheduler.apscheduler_app import _routine_analysis_crons
        self.assertEqual(_routine_analysis_crons(30), [
            "30 9,10,11,13,14 * * mon-fri",
            "0 10,11,13,14,15 * * mon-fri",
        ])

    def test_agent_cron_runs_monday_and_skips_saturday(self):
        from apscheduler.triggers.cron import CronTrigger
        from scheduler.apscheduler_app import _routine_analysis_crons
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
        trigger = CronTrigger.from_crontab(
            _routine_analysis_crons(60)[0], timezone=tz)
        monday = datetime(2026, 8, 17, 9, 59, tzinfo=tz)
        first = trigger.get_next_fire_time(None, monday)
        self.assertEqual(first, datetime(2026, 8, 17, 10, 0, tzinfo=tz))
        friday_close = datetime(2026, 8, 21, 15, 1, tzinfo=tz)
        next_run = trigger.get_next_fire_time(friday_close, friday_close)
        self.assertEqual(next_run.weekday(), 0)
        self.assertEqual(next_run.date(), date(2026, 8, 24))

    def test_unsupported_agent_interval_fails_loudly(self):
        from scheduler.apscheduler_app import _routine_analysis_crons
        with self.assertRaisesRegex(ValueError, "30 或 60"):
            _routine_analysis_crons(45)

    def test_tier2_batch_honors_coverage_rounds_and_cap(self):
        from scheduler.apscheduler_app import _tier2_batch_size
        self.assertEqual(_tier2_batch_size(24, 4, 8), 6)
        self.assertEqual(_tier2_batch_size(40, 4, 8), 8)
        self.assertEqual(_tier2_batch_size(0, 4, 8), 0)

    def test_candidate_job_uses_dry_run_and_only_preview_symbols(self):
        from scheduler.apscheduler_app import QuantScheduler
        from unittest.mock import AsyncMock, patch
        preview = {
            "candidate_symbols": ["510300", "159611"],
            "names": {"510300": "沪深300ETF", "159611": "电力ETF"},
            "asset_types": {"510300": "etf", "159611": "etf"},
        }
        scan = AsyncMock(return_value=[{"chief": {}}, {"chief": {}}])
        with patch("core.agent_switch.agent_system_enabled", return_value=True), \
             patch("strategies.live_rotation.run_live_rotation",
                   return_value=preview) as rotate, \
             patch("workflows.intraday_monitor_workflow.run_pool_scan", scan):
            QuantScheduler().job_agent_strategy_candidate_analysis()
        rotate.assert_called_once_with(notify=False, dry_run=True)
        scan.assert_awaited_once_with(
            ["510300", "159611"], preview["names"], max_concurrent=3,
            asset_type_map=preview["asset_types"])

    def test_agent_master_switch_skips_candidate_scan(self):
        from scheduler.apscheduler_app import QuantScheduler
        from unittest.mock import AsyncMock, patch
        scan = AsyncMock()
        with patch("core.agent_switch.agent_system_enabled", return_value=False), \
             patch("strategies.live_rotation.run_live_rotation") as rotate, \
             patch("workflows.intraday_monitor_workflow.run_pool_scan", scan):
            QuantScheduler().job_agent_strategy_candidate_analysis()
        rotate.assert_not_called()
        scan.assert_not_awaited()


class TestAgentSwitches(unittest.TestCase):
    def test_master_switch_persists_and_overrides_required_agents(self):
        import core.agent_switch as switches
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td, \
             patch.object(switches, "_SWITCH_FILE", Path(td) / "switches.json"), \
             patch.object(switches, "_override_cache", {}), \
             patch.object(switches, "_override_ts", 0.0):
            self.assertTrue(switches.agent_system_enabled())
            switches.set_agent_enabled("news_analyst", False)
            switches.set_agent_system_enabled(False)
            self.assertFalse(switches.agent_system_enabled())
            self.assertFalse(switches.agent_enabled("chief_researcher"))
            self.assertFalse(switches.agent_enabled("news_analyst"))
            states = {row["agent"]: row for row in switches.all_agent_states()}
            self.assertTrue(states["chief_researcher"]["enabled"])
            self.assertFalse(states["chief_researcher"]["effective_enabled"])
            switches.set_agent_system_enabled(True)
            self.assertTrue(switches.agent_enabled("chief_researcher"))
            self.assertFalse(switches.agent_enabled("news_analyst"))


# ================================================================
# 2. 撮合 + T+1 + 手续费 (模拟盘)
# ================================================================
class TestPaperTrading(unittest.TestCase):
    def setUp(self):
        self.account_ids = []

    def _broker(self, suffix=""):
        from paper_trading.paper_broker import PaperBroker
        aid = f"PA-TEST-{uuid.uuid4().hex[:12]}{suffix}"
        self.account_ids.append(aid)
        return PaperBroker(aid)

    def tearDown(self):
        from database.db_session import get_session
        from database.models import Account, AccountSnapshot, AuditLog, Order, Position, Trade
        with get_session() as s:
            order_ids = [oid for (oid,) in s.query(Order.order_id).filter(
                Order.account_id.in_(self.account_ids)).all()]
            if order_ids:
                s.query(Trade).filter(Trade.order_id.in_(order_ids)).delete(
                    synchronize_session=False)
                s.query(AuditLog).filter(
                    AuditLog.payload_json["order_id"].as_string().in_(order_ids)
                ).delete(synchronize_session=False)
            s.query(Order).filter(Order.account_id.in_(self.account_ids)).delete(
                synchronize_session=False)
            s.query(Position).filter(Position.account_id.in_(self.account_ids)).delete(
                synchronize_session=False)
            s.query(AccountSnapshot).filter(AccountSnapshot.account_id.in_(self.account_ids)).delete(
                synchronize_session=False)
            s.query(Account).filter(Account.account_id.in_(self.account_ids)).delete(
                synchronize_session=False)

    def test_buy_sell_t1(self):
        broker = self._broker()
        acc0 = broker.get_account()
        # 买入
        order = broker.place_order({
            "symbol": "510300", "side": "BUY", "qty": 1000,
            "order_type": "LIMIT", "price": 4.0,
        })
        self.assertEqual(order["status"], "SUBMITTED")
        # 撮合(简单模式: 按开盘价)
        bar = {"open": 4.02, "high": 4.05, "low": 3.98, "close": 4.03}
        broker.match_order(order["order_id"], bar, mode="simple")
        o = broker.query_order(order["order_id"])
        self.assertEqual(o["status"], "FILLED")
        # T+1: 今日买入不可卖
        pos = broker.account.get_position("510300")
        self.assertEqual(pos["total_qty"], 1000)
        self.assertEqual(pos["available_qty"], 0)      # T+1 锁定
        self.assertEqual(pos["today_buy_qty"], 1000)
        # 冻结释放检查
        acc = broker.get_account()
        self.assertAlmostEqual(acc["cash"] + acc["frozen_cash"] + acc["market_value"],
                               acc0["total_asset"], delta=100)

    def test_sell_over_available_rejected(self):
        broker = self._broker("-S")
        try:
            broker.place_order({
                "symbol": "510300", "side": "SELL", "qty": 500,
                "order_type": "LIMIT", "price": 4.0,
            })
            self.fail("应当拒绝卖出无持仓")
        except ValueError:
            pass

    def test_mark_to_market_persists_position_and_account(self):
        """实时价必须穿透到持仓和账户；防止盯市异常被吞后页面长期停在买入价。"""
        from database import repository as repo
        broker = self._broker("-MTM")
        order = broker.place_order({
            "symbol": "510300", "side": "BUY", "qty": 100,
            "order_type": "LIMIT", "price": 4.0,
        })
        broker.match_order(order["order_id"],
                           {"open": 4.0, "high": 4.1, "low": 3.9, "close": 4.0},
                           mode="simple")
        broker.mark_to_market({"510300": 4.2})
        position = repo.get_position(broker.account.account_id, "510300")
        account = repo.get_account(broker.account.account_id)
        self.assertAlmostEqual(position.latest_price, 4.2, places=4)
        self.assertAlmostEqual(position.market_value, 420.0, places=2)
        self.assertAlmostEqual(position.pnl,
                               (4.2 - position.cost_price) * position.total_qty,
                               places=2)
        self.assertAlmostEqual(account.market_value, 420.0, places=2)

    def test_position_view_includes_intraday_pnl(self):
        """持仓当日盈亏包含今日价格变化和买入费用。"""
        from unittest.mock import patch
        broker = self._broker("-DAYPNL")
        order = broker.place_order({
            "symbol": "510300", "side": "BUY", "qty": 100,
            "order_type": "LIMIT", "price": 4.0,
        })
        broker.match_order(order["order_id"],
                           {"open": 4.0, "high": 4.1, "low": 3.9, "close": 4.0},
                           mode="simple")
        broker.mark_to_market({"510300": 4.2})
        trade = broker.get_trades(limit=1)[0]
        expected = 420.0 - (trade["price"] * trade["qty"] + trade["fee"])
        with patch.object(broker.account, "_prev_close", return_value=4.0):
            position = broker.get_positions()[0]
        self.assertIn("day_pnl", position)
        self.assertAlmostEqual(position["day_pnl"], expected, places=2)

    def test_concurrent_reserve_cannot_overspend(self):
        """两个 broker 实例并发使用同一账户时，只有一笔 6 万元买单能冻结。"""
        aid = f"PA-TEST-{uuid.uuid4().hex[:12]}-C"
        self.account_ids.append(aid)
        first, second = self._broker_for(aid), self._broker_for(aid)

        def submit(pair):
            broker, intent = pair
            try:
                return broker.place_order({
                    "symbol": "510300", "side": "BUY", "qty": 100,
                    "order_type": "LIMIT", "price": 600,
                    "order_intent_id": intent,
                })
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(submit, [(first, f"I-{uuid.uuid4().hex}"),
                                           (second, f"I-{uuid.uuid4().hex}")]))
        self.assertEqual(sum(r is not None for r in results), 1)
        snapshot = first.get_account()
        self.assertGreaterEqual(snapshot["cash"], 0)
        self.assertAlmostEqual(snapshot["cash"] + snapshot["frozen_cash"],
                               snapshot["total_asset"], delta=0.01)

    def test_partial_fill_then_cancel_releases_exact_remainder(self):
        from core.symbol_utils import is_t0_etf
        from database import repository as repo
        broker = self._broker("-P")
        before = broker.get_account()["cash"]
        order = broker.place_order({
            "symbol": "513500", "side": "BUY", "qty": 200,
            "order_type": "LIMIT", "price": 4.0,
            "order_intent_id": f"I-{uuid.uuid4().hex}",
        })
        repo.fill_order(order["order_id"], 3.99, 100, 0.1, datetime.now(),
                        is_t0=is_t0_etf("513500"))
        broker.cancel_order(order["order_id"])
        after = broker.get_account()
        self.assertAlmostEqual(after["frozen_cash"], 0.0, places=4)
        self.assertAlmostEqual(after["cash"], before - 399.1, places=2)

    def test_concurrent_fill_is_idempotent(self):
        from database import repository as repo
        broker = self._broker("-F")
        order = broker.place_order({
            "symbol": "513500", "side": "BUY", "qty": 100,
            "order_type": "LIMIT", "price": 4.0,
            "order_intent_id": f"I-{uuid.uuid4().hex}",
        })

        def fill(_):
            return repo.fill_order(order["order_id"], 4.0, 100, 0.1,
                                   datetime.now(), is_t0=True)

        with ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(fill, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        from database import repository as repo
        pos = repo.get_position(broker.account.account_id, "513500")
        self.assertEqual(pos.total_qty, 100)

    @staticmethod
    def _broker_for(account_id):
        from paper_trading.paper_broker import PaperBroker
        return PaperBroker(account_id)

    def test_fee_calc(self):
        from paper_trading.order_manager import OrderManager
        # ETF: 佣金万2.5=2.5元, 无最低5元门槛, 过户费0.1
        fee = OrderManager._calc_fee("BUY", 100.0, 100, asset_type="etf")
        self.assertAlmostEqual(fee, 2.6, places=2)
        # 股票: 佣金最低5元
        fee_stock = OrderManager._calc_fee("BUY", 100.0, 100, asset_type="stock")
        self.assertAlmostEqual(fee_stock, 5.1, places=2)
        # 大额: 佣金=250 + 过户费10
        fee2 = OrderManager._calc_fee("BUY", 100.0, 10000, asset_type="etf")
        self.assertAlmostEqual(fee2, 260.0, places=2)


# ================================================================
# 3. 风控五层
# ================================================================
class TestRiskEngine(unittest.TestCase):
    def _plan(self, **kw):
        p = {
            "plan_id": "PLAN-T1", "decision_id": "DEC-T1", "symbol": "510300",
            "name": "沪深300ETF", "action": "BUY", "target_weight": 0.2,
            "order_amount": 20000, "estimated_quantity": 5000,
            "order_type": "LIMIT", "limit_price": 4.0, "confidence": 0.7,
            "reasons": ["测试"], "risks": [],
        }
        p.update(kw)
        return p

    def _account(self, total=100000, cash=50000, mv=50000, day_pnl=0):
        return {"total_asset": total, "cash": cash, "frozen_cash": 0,
                "market_value": mv, "day_pnl": day_pnl, "positions": []}

    def test_approve(self):
        from risk.risk_engine import get_risk_engine
        from features.technical_indicators import compute_technical_features
        bars = []
        d = date.today() - timedelta(days=100)
        for i in range(100):
            d += timedelta(days=1)
            bars.append({"trade_date": d, "open": 3 + i * 0.01,
                         "high": 3.02 + i * 0.01, "low": 2.98 + i * 0.01,
                         "close": 3 + i * 0.01, "volume": 1e6, "amount": 1e6 * 3})
        features = compute_technical_features(bars)
        # 该序列波动极小 → 不应因波动率拒绝
        r = get_risk_engine().check_plan(self._plan(order_amount=800),
                                         self._account(), features)
        self.assertIn(r.result, ["APPROVE", "REDUCE"])

    def test_reject_high_vol(self):
        from risk.risk_engine import get_risk_engine
        # 修复: 波动率阈值已从写死的 3.5% 改为与策略一致的年化 max_vol(50%,
        # 见 risk_limits.yaml max_volatility / rotation_executor max_vol)。
        # 原测试用 10% 期望被拒, 但 10% < 50% 不再属于高波动 —— 改为超过阈值。
        features = {"volatility_20d": 0.60}
        r = get_risk_engine().check_plan(self._plan(), self._account(), features)
        self.assertEqual(r.result, "REJECT")
        self.assertIn("波动率", r.blocked_reason)

    def test_reject_high_premium(self):
        from risk.risk_engine import get_risk_engine
        etf = {"premium_rate": 0.06, "liquidity_score": 80}
        r = get_risk_engine().check_plan(self._plan(), self._account(),
                                         None, etf)
        self.assertEqual(r.result, "REJECT")
        self.assertIn("溢价", r.blocked_reason)

    def test_confirm_required_low_conf(self):
        from risk.risk_engine import get_risk_engine
        r = get_risk_engine().check_plan(self._plan(confidence=0.3),
                                         self._account())
        self.assertEqual(r.result, "CONFIRM_REQUIRED")


# ================================================================
# 4. 回测指标
# ================================================================
class TestBacktestMetrics(unittest.TestCase):
    def test_metrics_basic(self):
        from backtest.metrics import compute_metrics
        eq = [100000, 102000, 101000, 105000, 108000]
        trades = [
            {"pnl": 1500, "fee": 10, "slippage_cost": 5, "side": "SELL",
             "amount": 10000, "hold_days": 5, "date": "2024-01-01"},
            {"pnl": -800, "fee": 10, "slippage_cost": 5, "side": "SELL",
             "amount": 10000, "hold_days": 3, "date": "2024-01-10"},
        ]
        m = compute_metrics(eq, trades)
        self.assertAlmostEqual(m["total_return"], 0.08, places=4)
        self.assertLess(m["max_drawdown"], 0)
        self.assertEqual(m["trade_count"], 2)
        self.assertAlmostEqual(m["win_rate"], 0.5)


class TestBacktestReliability(unittest.TestCase):
    def test_stock_fees_include_minimum_and_sell_stamp_tax(self):
        from backtest.engine import BacktestEngine
        engine = BacktestEngine(date(2024, 1, 1), date(2024, 2, 1),
                                asset_type="stock")
        self.assertEqual(engine._calc_fee(1000, "002176", "BUY"), 5.01)
        self.assertEqual(engine._calc_fee(1000, "002176", "SELL"), 5.51)
        etf = BacktestEngine(date(2024, 1, 1), date(2024, 2, 1),
                             asset_type="etf")
        self.assertEqual(etf._calc_fee(1000, "159611", "SELL"), 0.26)

    def test_asset_type_inference(self):
        from core.symbol_utils import infer_asset_type, price_limit_pct
        self.assertEqual(infer_asset_type("002176"), "stock")
        self.assertEqual(infer_asset_type("159611"), "etf")
        self.assertAlmostEqual(price_limit_pct("300750", "stock"), 0.20)

    def test_gap_fill_continues_after_primary_has_history(self):
        from data_service.market_data_service import MarketDataService
        from unittest.mock import MagicMock, patch
        missing = date(2026, 8, 12)
        primary = MagicMock()
        primary.get_daily_bars.return_value = []
        backup = MagicMock()
        backup.get_daily_bars.return_value = [{
            "symbol": "159611", "trade_date": missing,
            "open": 1.06, "high": 1.07, "low": 1.05, "close": 1.065,
            "volume": 100, "amount": 100, "source": "backup",
        }]
        hub = MagicMock()
        hub._chain.side_effect = lambda category: (
            ["primary", "backup"] if category == "daily_gap" else [])
        hub._get_client.side_effect = lambda name: primary if name == "primary" else backup
        svc = MarketDataService(hub)
        with patch("data_service.market_data_service.repo.upsert_daily_bars") as save:
            rows = svc.fill_daily_gaps("159611", [missing], "etf")
        self.assertEqual([r["trade_date"] for r in rows], [missing])
        backup.get_daily_bars.assert_called_once()
        save.assert_called_once()

    def test_repository_prefers_complete_daily_bar(self):
        """同日多源时，成交额完整的行应优先于 amount=0 的高优先级行。"""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from database import repository
        d = date(2026, 8, 12)
        incomplete = SimpleNamespace(trade_date=d, source="akshare", volume=100, amount=0)
        complete = SimpleNamespace(trade_date=d, source="tencent", volume=100, amount=1000)
        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value.all.return_value = [incomplete, complete]
        session = MagicMock()
        session.query.return_value = query
        from contextlib import contextmanager

        @contextmanager
        def fake_session():
            yield session

        with patch("database.repository.get_session", fake_session):
            rows = repository.get_daily_bars("159611", d, d)
        self.assertIs(rows[0], complete)

    def test_daily_stop_executes_next_open(self):
        from backtest.engine import BacktestEngine

        class Loader:
            def universe(self):
                return ["002176"]

            def trade_dates(self, start, end):
                return [date(2024, 1, 2), date(2024, 1, 3),
                        date(2024, 1, 4), date(2024, 1, 5)]

            def prepare_daily(self, start, end, warmup_start=None):
                return {"002176": {"coverage": 1.0}}

            def load_all_daily(self, symbol, start, end):
                return [
                    {"symbol": symbol, "trade_date": date(2024, 1, 2), "open": 10,
                     "high": 10.2, "low": 9.8, "close": 10, "volume": 1, "amount": 1},
                    {"symbol": symbol, "trade_date": date(2024, 1, 3), "open": 10,
                     "high": 10.1, "low": 8.9, "close": 9, "volume": 1, "amount": 1},
                    {"symbol": symbol, "trade_date": date(2024, 1, 4), "open": 8,
                     "high": 8.5, "low": 7.9, "close": 8.2, "volume": 1, "amount": 1},
                    {"symbol": symbol, "trade_date": date(2024, 1, 5), "open": 7.5,
                     "high": 7.8, "low": 7.4, "close": 7.6, "volume": 1, "amount": 1},
                ]

            def snapshot(self):
                return {}

            def snapshot_hash(self):
                return "test"

            def load_benchmark(self, symbol, start, end):
                return []

        engine = BacktestEngine(date(2024, 1, 2), date(2024, 1, 5),
                                initial_cash=100000, asset_type="stock")

        def signal(asof, prices, d, broker):
            if d == date(2024, 1, 2):
                return {"002176": {"action": "BUY", "qty": 100}}
            if d == date(2024, 1, 4) and broker.positions:
                return {"002176": {"action": "SELL", "qty": 100,
                                    "stop": True, "reason": "止损"}}
            return {}

        # 测试只关心撮合时点，避免写入开发数据库。
        from unittest.mock import patch
        with patch("backtest.engine.repo.save_backtest_run"), \
             patch("backtest.engine.repo.save_backtest_result"), \
             patch("backtest.engine.repo.update_backtest_run"):
            metrics = engine.run_daily(Loader(), signal)
        self.assertEqual(metrics["run_id"], engine.run_id)
        sell = [t for t in metrics["trade_details"]
                if t["side"] == "SELL" and t["reason"] == "止损"][0]
        self.assertEqual(sell["date"], "2024-01-05")
        self.assertAlmostEqual(sell["price"], 7.5 * (1 - engine.slippage), places=6)

    def test_coverage_allows_small_missing_boundary_above_threshold(self):
        from backtest.data_replayer import DataReplayer
        from unittest.mock import patch
        replayer = DataReplayer(["159611"], min_coverage=0.5)
        replayer.trade_dates = lambda start, end: [
            date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        replayer.load_all_daily = lambda symbol, start, end: [
            {"symbol": symbol, "trade_date": date(2024, 1, 3), "open": 1,
             "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
            {"symbol": symbol, "trade_date": date(2024, 1, 4), "open": 1,
             "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
        ]
        from types import SimpleNamespace
        with patch("backtest.data_replayer.get_market_service") as svc, \
             patch("backtest.data_replayer.repo.get_symbol",
                   return_value=SimpleNamespace(listed_date=date(2024, 1, 2))):
            svc.return_value.fill_daily_gaps.return_value = []
            report = replayer.prepare_daily(date(2024, 1, 2), date(2024, 1, 4))
        self.assertAlmostEqual(report["159611"]["coverage"], 2 / 3, places=6)
        self.assertFalse(report["159611"]["boundary_ok"])

    def test_coverage_warns_when_source_reachable_but_history_is_partial(self):
        from backtest.data_replayer import DataReplayer
        from types import SimpleNamespace
        from unittest.mock import patch
        replayer = DataReplayer(["159611"], min_coverage=0.98)
        replayer.trade_dates = lambda start, end: [
            date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        replayer.load_all_daily = lambda symbol, start, end: [
            {"symbol": symbol, "trade_date": date(2024, 1, 3), "open": 1,
             "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
        ]
        def reachable_gap_fill(symbol, missing, asset_type, diagnostics=None):
            diagnostics.update({"network_failed": False, "reachable_sources": ["mock"]})
            return []
        with patch("backtest.data_replayer.get_market_service") as svc, \
             patch("backtest.data_replayer.repo.get_symbol",
                   return_value=SimpleNamespace(listed_date=date(2024, 1, 2))):
            svc.return_value.fill_daily_gaps.side_effect = reachable_gap_fill
            report = replayer.prepare_daily(date(2024, 1, 2), date(2024, 1, 4))
        self.assertEqual(report["159611"]["status"], "partial")
        self.assertEqual(report["159611"]["missing_days"],
                         ["2024-01-02", "2024-01-04"])

    def test_coverage_excludes_pre_listing_days(self):
        from backtest.data_replayer import DataReplayer
        from unittest.mock import patch
        replayer = DataReplayer(["159141"], min_coverage=0.98)
        replayer.trade_dates = lambda start, end: [
            date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
            date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9),
            date(2024, 1, 10)]
        replayer.load_all_daily = lambda symbol, start, end: [
            {"symbol": symbol, "trade_date": d, "open": 1, "high": 1,
             "low": 1, "close": 1, "volume": 1, "amount": 1}
            for d in (date(2024, 1, 9), date(2024, 1, 10))
        ]
        with patch("backtest.data_replayer.get_market_service") as svc, \
             patch("backtest.data_replayer.repo.get_symbol", return_value=None), \
             patch("backtest.data_replayer.repo.set_symbol_listed_date") as save_date:
            svc.return_value.fill_daily_gaps.return_value = []
            report = replayer.prepare_daily(date(2024, 1, 2), date(2024, 1, 10))
        item = report["159141"]
        self.assertEqual(item["listed_date"], "2024-01-09")
        self.assertEqual(item["pre_listing_days"], 5)
        self.assertEqual(item["coverage"], 1.0)
        self.assertEqual(item["status"], "complete")
        save_date.assert_called_once_with("159141", date(2024, 1, 9))

    def test_coverage_blocks_only_when_gap_sources_all_network_fail(self):
        from backtest.data_replayer import BacktestDataError, DataReplayer
        from types import SimpleNamespace
        from unittest.mock import patch
        replayer = DataReplayer(["159611"], min_coverage=0.98)
        replayer.trade_dates = lambda start, end: [
            date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        replayer.load_all_daily = lambda symbol, start, end: [
            {"symbol": symbol, "trade_date": date(2024, 1, 3), "open": 1,
             "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
        ]
        def failed_gap_fill(symbol, missing, asset_type, diagnostics=None):
            diagnostics.update({"network_failed": True, "attempts": 2})
            return []
        with patch("backtest.data_replayer.get_market_service") as svc, \
             patch("backtest.data_replayer.repo.get_symbol",
                   return_value=SimpleNamespace(listed_date=date(2024, 1, 2))):
            svc.return_value.fill_daily_gaps.side_effect = failed_gap_fill
            with self.assertRaisesRegex(BacktestDataError, "网络失败"):
                replayer.prepare_daily(date(2024, 1, 2), date(2024, 1, 4))

    def test_prepare_daily_freezes_reused_experiment_snapshot(self):
        from backtest.data_replayer import DataReplayer
        from types import SimpleNamespace
        from unittest.mock import patch
        replayer = DataReplayer(["159611"], min_coverage=0.98)
        replayer.trade_dates = lambda start, end: [date(2024, 1, 2)]
        replayer.load_all_daily = lambda symbol, start, end: [
            {"symbol": symbol, "trade_date": date(2024, 1, 2), "open": 1,
             "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
        ]
        with patch("backtest.data_replayer.get_market_service") as svc, \
             patch("backtest.data_replayer.repo.get_symbol",
                   return_value=SimpleNamespace(listed_date=date(2024, 1, 2))):
            first = replayer.prepare_daily(date(2024, 1, 2), date(2024, 1, 2))
            second = replayer.prepare_daily(date(2024, 1, 2), date(2024, 1, 2))
        self.assertIs(first, second)
        svc.return_value.fill_daily_gaps.assert_not_called()

    def test_dynamic_universe_prepare_uses_local_snapshot_without_gap_fill(self):
        from backtest.data_replayer import DataReplayer
        from types import SimpleNamespace
        from unittest.mock import patch
        replayer = DataReplayer(["510001"], min_coverage=0.98)
        replayer.trade_dates = lambda start, end: [
            date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]
        replayer.load_all_daily = lambda symbol, start, end: [
            {"symbol": symbol, "trade_date": date(2026, 8, 10),
             "open": 1, "high": 1, "low": 1, "close": 1,
             "volume": 1, "amount": 1},
        ]
        with patch("backtest.data_replayer.get_market_service") as svc, \
             patch("backtest.data_replayer.repo.get_symbol",
                   return_value=SimpleNamespace(listed_date=date(2026, 8, 10))):
            report = replayer.prepare_daily(
                date(2026, 8, 10), date(2026, 8, 12), gap_fill=False)
        svc.return_value.fill_daily_gaps.assert_not_called()
        self.assertEqual(report["510001"]["status"], "partial")

    def test_legacy_stop_maps_to_both_new_stop_models(self):
        from strategies.rotation_executor import resolve_rotation_params
        resolved = resolve_rotation_params({"stop_loss_pct": 0.07})
        self.assertEqual(resolved["hard_stop_pct"], 0.07)
        self.assertEqual(resolved["trailing_stop_pct"], 0.07)

    def test_active_legacy_preset_is_not_loaded_into_dynamic_paper(self):
        import json
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from strategies.rotation_executor import load_rotation_params
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "strategy_presets.json").write_text(json.dumps({
                "active_live": "old",
                "presets": {"old": {"stop_loss_pct": 0.07}},
            }), encoding="utf-8")
            settings = type("S", (), {"get": lambda self, key, default=None: {
                "hard_stop_pct": 0.10, "trailing_stop_pct": 0.08,
            }})()
            with patch("strategies.rotation_executor.get_settings", return_value=settings), \
                 patch("core.config.ROOT_DIR", root):
                resolved = load_rotation_params(include_live=True)
        self.assertEqual(resolved["hard_stop_pct"], 0.10)
        self.assertEqual(resolved["trailing_stop_pct"], 0.08)

    def test_paper_rotation_uses_all_tradable_watch_items_and_holdings(self):
        from strategies.live_rotation import _paper_rotation_universe
        watch = [
            {"symbol": f"51{i:04d}", "name": str(i), "asset_type": "etf"}
            for i in range(25)
        ] + [{"symbol": "000001", "name": "上证指数", "asset_type": "index"}]
        symbols, _, asset_types = _paper_rotation_universe(watch, ["002176"])
        self.assertEqual(len(symbols), 26)
        self.assertNotIn("000001", symbols)
        self.assertIn("002176", symbols)
        self.assertEqual(asset_types["002176"], "stock")

    def test_paper_rebalance_counter_survives_rebuild_and_ignores_same_day_rerun(self):
        from strategies.rotation_executor import build_rotation_signal_fn
        state = {"signal_day_no": 0, "last_signal_date": ""}
        for d in (date(2026, 8, 10), date(2026, 8, 10),
                  date(2026, 8, 11), date(2026, 8, 12)):
            fn = build_rotation_signal_fn(params={"_rebalance_state": state})
            fn({}, {}, d)
        self.assertEqual(state["signal_day_no"], 3)
        self.assertEqual(state["last_signal_date"], "2026-08-12")

    def test_dynamic_etf_selector_is_point_in_time_and_theme_capped(self):
        from strategies.dynamic_etf_universe import DynamicEtfUniverseSelector
        from unittest.mock import patch
        bars = {}
        for idx, sym in enumerate(("510001", "510002", "510003")):
            rows = []
            for n in range(80):
                d = date(2026, 1, 1) + timedelta(days=n)
                rows.append({"symbol": sym, "trade_date": d,
                             "open": 1 + n / 1000, "high": 1.01 + n / 1000,
                             "low": .99 + n / 1000, "close": 1 + n / 1000,
                             "volume": 1_000_000,
                             "amount": (100 - idx * 10) * 1_000_000})
            # A future bar must not alter the as-of selection metrics.
            rows.append({**rows[-1], "trade_date": date(2027, 1, 1),
                         "amount": 999_000_000})
            bars[sym] = rows
        selector = DynamicEtfUniverseSelector({
            "min_listing_days": 60, "max_candidates": 10,
            "min_candidates": 2, "max_per_theme": 2,
            "min_avg_amount": 1, "coverage_window": 30,
            # 合成K线的年化波动率仅~2.5%, 关闭下限避免误伤
            "min_annualized_volatility": 0,
        })
        symbols = {s: {"name": s, "asset_type": "etf", "status": "active"}
                   for s in bars}
        metadata = {s: {"name": s, "tracking_index": "同一指数"} for s in bars}
        with patch("strategies.dynamic_etf_universe.repo.get_symbol_metadata",
                   return_value=symbols), \
             patch("strategies.dynamic_etf_universe.repo.get_etf_metadata",
                   return_value=metadata):
            result = selector.select(
                bars, date(2026, 3, 21), date(2026, 3, 22),
                persist=False, apply_manual_overrides=False)
        members = result.snapshot["members"]
        self.assertEqual([x["symbol"] for x in members], ["510001", "510002"])
        self.assertEqual(members[0]["last_bar_date"], "2026-03-21")
        self.assertEqual(members[0]["avg_amount"], 100_000_000)

    def test_dynamic_universe_excludes_nonmembers_from_buy_ranking(self):
        from strategies.rotation_executor import build_rotation_signal_fn
        from unittest.mock import patch
        class Broker:
            cash = 100000
            positions = {}
            def position_value(self, prices):
                return 0
        features = {
            "close": 1, "ma20": 1, "amount_ma20": 100_000_000,
            "volatility_20d": .1, "momentum_20d": .10,
        }
        fn = build_rotation_signal_fn(
            params={"top_n": 2, "mom_window": 20, "min_momentum": 0,
                    "market_filter": False, "warmup_days": 1,
                    "require_above_ma20": False, "initial_ratio": 1,
                    "target_weight": .2},
            universe_provider=lambda d, asof: {"510001"})
        bars = {s: [{"trade_date": date(2026, 8, 12), "low": 1}]
                for s in ("510001", "510002")}
        with patch("strategies.rotation_executor.compute_technical_features",
                   return_value=features):
            signals = fn(bars, {"510001": 1, "510002": 1},
                         date(2026, 8, 12), Broker())
        self.assertIn("510001", signals)
        self.assertNotIn("510002", signals)

    def test_recent_range_filter_blocks_chasing_near_high(self):
        from strategies.rotation_executor import build_rotation_signal_fn
        from unittest.mock import patch
        class Broker:
            cash = 100000
            positions = {}
            def position_value(self, prices):
                return 0
        features = {
            "close": 1.19, "ma20": 1.10, "amount_ma20": 100_000_000,
            "volatility_20d": .1, "momentum_20d": .10,
        }
        fn = build_rotation_signal_fn(params={
            "top_n": 1, "mom_window": 20, "min_momentum": 0,
            "market_filter": False, "warmup_days": 1,
            "initial_ratio": 1, "target_weight": .2,
            "max_position_in_recent_range": .85,
        })
        bars = {"510001": [
            {"trade_date": date(2026, 7, i + 1), "low": 1.0, "high": 1.2}
            for i in range(20)
        ]}
        with patch("strategies.rotation_executor.compute_technical_features",
                   return_value=features):
            signals = fn(bars, {"510001": 1.19}, date(2026, 8, 12), Broker())
        self.assertNotIn("510001", signals)

    def test_price_regime_normalization_removes_adjustment_jumps(self):
        from backtest.data_replayer import DataReplayer
        bars = [
            {"symbol": "515880", "trade_date": date(2025, 12, 31),
             "open": 0.519, "high": 0.52, "low": 0.50, "close": 0.513,
             "volume": 1, "amount": 1},
            {"symbol": "515880", "trade_date": date(2026, 1, 5),
             "open": 3.125, "high": 3.15, "low": 3.09, "close": 3.143,
             "volume": 1, "amount": 1},
            {"symbol": "515880", "trade_date": date(2026, 2, 2),
             "open": 3.287, "high": 3.3, "low": 3.1, "close": 3.16,
             "volume": 1, "amount": 1},
            {"symbol": "515880", "trade_date": date(2026, 2, 3),
             "open": 1.074, "high": 1.095, "low": 1.046, "close": 1.084,
             "volume": 1, "amount": 1},
        ]
        normalized, events = DataReplayer._normalize_price_regimes(bars, "etf")
        self.assertEqual(len(events), 2)
        self.assertEqual(normalized[-1]["close"], bars[-1]["close"])
        gaps = [b["open"] / a["close"] for a, b in zip(normalized, normalized[1:])]
        self.assertTrue(all(0.75 <= ratio <= 1.25 for ratio in gaps))

    def test_price_regime_normalization_keeps_legal_limit_move(self):
        from backtest.data_replayer import DataReplayer
        bars = [
            {"symbol": "588000", "trade_date": date(2026, 1, 2),
             "open": 1, "high": 1, "low": 1, "close": 1,
             "volume": 1, "amount": 1},
            {"symbol": "588000", "trade_date": date(2026, 1, 5),
             "open": 1.20, "high": 1.20, "low": 1.20, "close": 1.20,
             "volume": 1, "amount": 1},
        ]
        normalized, events = DataReplayer._normalize_price_regimes(bars, "etf")
        self.assertEqual(events, [])
        self.assertEqual(normalized, bars)

    def test_backtest_params_do_not_inherit_active_live_preset(self):
        from strategies.rotation_executor import build_rotation_signal_fn
        from unittest.mock import patch
        with patch("strategies.rotation_executor.load_rotation_params") as load:
            load.return_value = {"top_n": 3, "mom_window": 20,
                                 "trend_ma_window": 20}
            build_rotation_signal_fn(params={"top_n": 5})
        load.assert_called_once_with(include_live=False)

    def test_rotation_param_validation_rejects_unsupported_window(self):
        from strategies.rotation_executor import validate_rotation_params
        with self.assertRaisesRegex(ValueError, "mom_window"):
            validate_rotation_params({"mom_window": 17})


# ================================================================
# 5. 数据质量
# ================================================================
class TestQuoteConsensus(unittest.TestCase):
    class _FakeQuoteClient:
        def __init__(self, quotes):
            self.quotes = quotes

        def get_realtime_quotes_batch(self, symbols):
            return {s: dict(self.quotes[s]) for s in symbols if s in self.quotes}

        def get_realtime_quote(self, symbol, asset_type="etf", **kwargs):
            return dict(self.quotes[symbol])

    @staticmethod
    def _quote(price, source):
        return {
            "symbol": "513120", "name": "港股创新药ETF广发",
            "latest_price": price, "prev_close": 1.268,
            "open": 1.27, "high": 1.29, "low": 1.26,
            "change_pct": (price / 1.268 - 1) * 100,
            "quote_time": datetime.now(), "source": source,
        }

    def test_two_matching_sources_are_accepted(self):
        from data_service.live_quote_service import LiveQuoteService
        tx = self._FakeQuoteClient({"513120": self._quote(1.279, "tencent")})
        sina = self._FakeQuoteClient({"513120": self._quote(1.279, "sina")})
        service = LiveQuoteService(tx, sina, quote_diff_threshold=0.005)
        quote = service.get_quotes(["513120"], max_age=0)["513120"]
        self.assertEqual(quote["verified_sources"], 2)
        self.assertEqual(quote["source_prices"], {"sina": 1.279, "tencent": 1.279})
        self.assertEqual(quote["quality_status"], "VALID")

    def test_conflicting_sources_do_not_publish_a_price(self):
        from data_service.live_quote_service import LiveQuoteService
        tx = self._FakeQuoteClient({"513120": self._quote(1.279, "tencent")})
        sina = self._FakeQuoteClient({"513120": self._quote(1.100, "sina")})
        service = LiveQuoteService(tx, sina, quote_diff_threshold=0.005)
        self.assertEqual(service.get_quotes(["513120"], max_age=0), {})
        self.assertIn("quote conflict", service.last_error)

    def test_sina_parser_uses_exchange_timestamp(self):
        from data_sources.sina_client import _parse_sina_quote
        fields = ["港股创新药ETF广发", "1.270", "1.268", "1.279",
                  "1.290", "1.260", "1.278", "1.279", "10000", "12790"]
        fields.extend(["0"] * 20)
        fields.extend(["2026-08-18", "11:35:23", "00"])
        quote = _parse_sina_quote("513120", ",".join(fields))
        self.assertEqual(quote["quote_time"], datetime(2026, 8, 18, 11, 35, 23))


class TestDataQuality(unittest.TestCase):
    def test_missing_blocked(self):
        from data_service.data_quality import get_quality_checker
        rep = get_quality_checker().check_daily_bars("510300", [])
        self.assertEqual(rep.status, "MISSING")
        self.assertIsNotNone(rep.blocked_reason)

    def test_delayed(self):
        from data_service.data_quality import get_quality_checker
        bars = [{"trade_date": date(2024, 1, 1), "open": 3, "high": 3.1,
                 "low": 2.9, "close": 3.05}]
        rep = get_quality_checker().check_daily_bars("510300", bars,
                                                     expect_trade_date=date(2024, 2, 1))
        self.assertEqual(rep.status, "DELAYED")

    def test_single_source_quote_is_blocked(self):
        from data_service.data_quality import get_quality_checker
        quote = {"latest_price": 1.279, "high": 1.29, "low": 1.26,
                 "quote_time": datetime.now(), "verified_sources": 1}
        rep = get_quality_checker().check_realtime_quote("513120", quote)
        self.assertEqual(rep.status, "SUSPICIOUS")
        self.assertIsNotNone(rep.blocked_reason)


# ================================================================
# 6. 工作流冒烟 (模拟模式, 避免真实LLM调用污染数据/消耗token)
# ================================================================
class TestWorkflow(unittest.TestCase):
    def test_agent_shadow_mode_never_runs_trading_workflow(self):
        """Agent 有方向结论时也只能留档，不能创建计划或订单。"""
        from unittest.mock import AsyncMock, patch
        from workflows.graph import WorkflowState
        from workflows.intraday_monitor_workflow import run_intraday_scan

        state = WorkflowState(symbol="510300")
        state.set("chief", {"research_decision": "BUY_CANDIDATE", "confidence": 0.9})
        state.set("data_snapshot", {"latest_price": 4.5, "quality_status": "VALID"})

        class FakeGraph:
            progress_cb = None
            async def run(self, _state):
                return state

        with patch("workflows.intraday_monitor_workflow.build_research_graph",
                   return_value=FakeGraph()), \
             patch("memory.audit_log.AuditLogger.log"), \
             patch("core.agent_switch.agent_system_enabled", return_value=True):
            result = asyncio.run(run_intraday_scan("510300", "沪深300ETF"))
        self.assertEqual(result["mode"], "SHADOW_ONLY")
        self.assertEqual(result["execution"]["status"], "OBSERVED_ONLY")
        self.assertIsNone(result["plan"])

    def test_shadow_agreement_classification(self):
        from analytics.agent_shadow import classify_agreement
        self.assertEqual(classify_agreement("BUY", "BUY_CANDIDATE"), "AGREE")
        self.assertEqual(classify_agreement("BUY", "SELL_CANDIDATE"), "DISAGREE")
        self.assertEqual(classify_agreement("BUY", "HOLD"), "NEUTRAL")
        self.assertEqual(classify_agreement("SELL", "BUY_CANDIDATE"), "DISAGREE")
        self.assertEqual(classify_agreement("SELL", "SELL_CANDIDATE"), "AGREE")
        self.assertEqual(classify_agreement("SELL", "NO_VIEW"), "NO_VIEW")

    def test_research_workflow(self):
        # 强制 LLM 模拟模式 + 测试后清理, 避免污染"最近工作流"与消耗token
        from core.llm import get_llm
        llm = get_llm()
        was_mock = llm.is_mock()
        llm.force_mock(True)
        state = None
        try:
            async def run():
                from workflows.research_workflow import run_research
                # 模拟模式必须是确定性的离线测试；真实上游短暂断连不应让
                # 工作流结构测试失败。历史切片也同时验证无未来数据路径。
                bars = self._offline_research_bars()
                from unittest.mock import patch
                with patch("workflows.research_workflow._market_env_bars",
                           return_value=bars), \
                     patch("core.agent_switch.agent_system_enabled",
                           return_value=True):
                    st = await run_research("510300", asof_override={
                        "bars": bars,
                        "quote": {
                            "latest_price": bars[-1]["close"],
                            "prev_close": bars[-2]["close"],
                            "change_pct": bars[-1]["close"] / bars[-2]["close"] - 1,
                            "source": "unit_test",
                        },
                        "order_book": {},
                        "money_flow": {},
                    })
                return st
            state = asyncio.run(run())
            self.assertIsNotNone(state.get("analyst_outputs"))
            self.assertIsNotNone(state.get("chief"))
        finally:
            llm.force_mock(was_mock)
            # 清理本次测试产生的运行记录(不污染"最近工作流"列表)
            if state is not None:
                from database.models import AgentRun, AgentOutput
                from database.db_session import get_session
                with get_session() as s:
                    runs = s.query(AgentRun).filter(
                        AgentRun.trace_id == state.trace_id).all()
                    for r in runs:
                        s.query(AgentOutput).filter_by(run_id=r.run_id).delete()
                        s.delete(r)
                    s.commit()

    @staticmethod
    def _offline_research_bars():
        bars = []
        d = date(2026, 1, 1)
        for i in range(80):
            d += timedelta(days=1)
            if d.weekday() >= 5:
                continue
            close = 4.0 + i * 0.002
            bars.append({
                "symbol": "510300", "trade_date": d,
                "open": close - 0.005, "high": close + 0.01,
                "low": close - 0.01, "close": close,
                "volume": 10_000_000, "amount": close * 10_000_000,
            })
        return bars

    def test_circuit_breaker(self):
        from risk.circuit_breaker import CircuitBreaker
        from unittest.mock import patch
        cb = CircuitBreaker()
        cb._read_shared = lambda: {}
        cb._mutate_shared = lambda _fn: None
        self.assertFalse(cb.is_paused())
        with patch("memory.audit_log.AuditLogger.log"):
            cb.pause("测试")
            self.assertTrue(cb.is_paused())
            cb.resume()


if __name__ == "__main__":
    unittest.main(verbosity=2)
