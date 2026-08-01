# -*- coding: utf-8 -*-
"""
自动化测试套件
==============
运行: python -m tests.test_suite
覆盖: 技术指标 / 撮合 / T+1 / 风控五层 / 回测指标 / 数据质量 / 工作流冒烟
"""
import asyncio
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")

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
        self.assertTrue(0 <= f["momentum_20d"] or f["momentum_20d"] < 1)

    def test_market_summary(self):
        from features.technical_indicators import compute_technical_features
        from features.market_state import build_market_summary
        f = compute_technical_features(self._bars())
        s = build_market_summary("510300", "沪深300ETF", f)
        self.assertIn("510300", s)
        self.assertIn("均线", s)


# ================================================================
# 2. 撮合 + T+1 + 手续费 (模拟盘)
# ================================================================
class TestPaperTrading(unittest.TestCase):
    def test_buy_sell_t1(self):
        from paper_trading.paper_broker import PaperBroker
        import time as _t
        broker = PaperBroker(f"PA-TEST-{int(_t.time())}")   # 唯一账户保证幂等
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
        from paper_trading.paper_broker import PaperBroker
        import time as _t
        broker = PaperBroker(f"PA-TEST-{int(_t.time())}-S")
        try:
            broker.place_order({
                "symbol": "510300", "side": "SELL", "qty": 500,
                "order_type": "LIMIT", "price": 4.0,
            })
            self.fail("应当拒绝卖出无持仓")
        except ValueError:
            pass

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
        features = {"volatility_20d": 0.10}
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


# ================================================================
# 5. 数据质量
# ================================================================
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


# ================================================================
# 6. 工作流冒烟 (模拟模式, 使用缓存数据)
# ================================================================
class TestWorkflow(unittest.TestCase):
    def test_research_workflow(self):
        async def run():
            from workflows.research_workflow import run_research
            state = await run_research("510300")
            return state
        state = asyncio.run(run())
        self.assertIsNotNone(state.get("analyst_outputs"))
        self.assertIsNotNone(state.get("chief"))

    def test_circuit_breaker(self):
        from risk.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker.instance()
        cb.resume()  # 清理状态
        self.assertFalse(cb.is_paused())
        cb.pause("测试")
        self.assertTrue(cb.is_paused())
        cb.resume()


if __name__ == "__main__":
    unittest.main(verbosity=2)
