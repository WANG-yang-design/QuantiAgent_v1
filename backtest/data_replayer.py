# -*- coding: utf-8 -*-
"""
回测数据回放器
==============
按时间顺序提供K线数据, 严格保证无未来函数:
- load_all_daily(symbol, start, end): 一次加载, 由引擎按 asof 截取
- 交易日历来自数据库/数据源
"""
import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from data_service.market_data_service import get_market_service
from database import repository as repo

logger = logging.getLogger("backtest.replayer")


class BacktestDataError(RuntimeError):
    """回测行情不完整或无法复现。"""


class DataReplayer:
    """回测数据回放: 全部从数据库读取(先由采集任务入库)。"""

    def __init__(self, universe: List[str], asset_type: str = "etf",
                 asset_types: Optional[Dict[str, str]] = None,
                 min_coverage: float = 0.98,
                 online_fill: bool = True):
        self.universe_list = universe
        self.asset_type = asset_type
        self.asset_types = {str(k): str(v) for k, v in (asset_types or {}).items()}
        self.min_coverage = min(max(float(min_coverage), 0.0), 1.0)
        self.online_fill = bool(online_fill)
        self._cal: List[date] = []
        self._daily_cache: Dict[str, List[dict]] = {}
        self._minute_cache: Dict[str, Dict[date, List[dict]]] = {}
        self._coverage: Dict[str, Dict[str, Any]] = {}
        self._snapshot: Dict[str, List[dict]] = {}
        self._price_adjustments: Dict[str, List[Dict[str, Any]]] = {}
        self._prepared_daily_key: Optional[tuple] = None

    def universe(self) -> List[str]:
        return self.universe_list

    def asset_type_for(self, symbol: str) -> str:
        return self.asset_types.get(symbol, self.asset_type)

    # ------------------------------------------------------------------
    def trade_dates(self, start: date, end: date) -> List[date]:
        if not self._cal:
            cal = get_market_service().get_trade_calendar(start, end)
            if cal:
                self._cal = cal
            else:
                # 修复: 数据源失败时的"工作日"兜底不再写入永久缓存 ——
                # 原实现节假日被当成交易日, asof 切片无新K线 → 相同数据重复
                # 产生信号(超量建仓), 且数据源恢复后依然用错误日历。
                logger.warning("交易日历获取失败, 本次按工作日近似")
                return [d for d in self._weekdays(start, end)]
        return [d for d in self._cal if start <= d <= end]

    @staticmethod
    def _weekdays(start: date, end: date) -> List[date]:
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    # ------------------------------------------------------------------
    def load_all_daily(self, symbol: str, start: date, end: date) -> List[dict]:
        """一次加载全部日K(引擎按 asof 截取, 防止未来函数)。"""
        key = f"{symbol}:{start}:{end}"
        if key in self._daily_cache:
            return self._daily_cache[key]
        bars = repo.get_daily_bars(symbol, start, end)
        # 不能以“数据库里有任意一根K线”作为完整性的依据。始终请求整个区间，
        # 由行情服务落库后再从仓库按来源优先级合并，补齐首尾及区间内缺口。
        if self.online_fill:
            try:
                get_market_service().get_daily_bars(
                    symbol, start, end, self.asset_type_for(symbol),
                    use_cache=False, save_db=True)
                refreshed = repo.get_daily_bars(symbol, start, end)
                if len(refreshed) >= len(bars):
                    bars = refreshed
                logger.info("回测行情准备 %s: %d 条", symbol, len(bars))
            except Exception as exc:
                logger.warning("回测行情在线补齐失败 %s: %s", symbol, exc)
        norm = []
        for b in bars:
            norm.append({
                "symbol": b.symbol, "trade_date": b.trade_date,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume, "amount": b.amount,
            })
        # 免费源的前复权基准会随公司行为变化。数据库按日期增量补抓时，旧行
        # 与新行可能来自不同复权基准，形成数倍断层。回测前统一到“最新一段”
        # 的价格口径，避免持仓市值凭空跳变；原始数据库行情仍保留用于审计。
        norm, adjustments = self._normalize_price_regimes(
            norm, self.asset_type_for(symbol))
        self._price_adjustments[symbol] = adjustments
        self._daily_cache[key] = norm
        return norm

    def prepare_daily(self, start: date, end: date,
                      warmup_start: Optional[date] = None,
                      gap_fill: bool = True) -> Dict[str, Dict[str, Any]]:
        """补齐、校验并冻结本次日线回测数据。

        覆盖率按标的上市后的交易区间计算；warmup 数据仅用于指标预热。
        上市前自然无行情不算缺失。上市后缺口先多源重试和落库，网络全部失败
        才阻断；数据源正常返回但确实无数据时保留警告并跳过对应交易日。
        """
        load_start = warmup_start or start
        prepare_key = (start, end, load_start, bool(gap_fill))
        # Reusing one replayer means reusing one immutable experiment snapshot.
        # Do not gap-fill again between parameter candidates.
        if self._prepared_daily_key == prepare_key and self._snapshot:
            return self._coverage
        expected = self.trade_dates(start, end)
        if not expected:
            raise BacktestDataError("无法获取交易日历，已阻断回测")
        expected_set = set(expected)
        reports: Dict[str, Dict[str, Any]] = {}
        available_symbols = 0
        for symbol in self.universe_list:
            bars = self.load_all_daily(symbol, load_start, end)
            # 上市日可能已由标的基础资料维护；已知时先裁掉上市前日期，避免
            # 对必然不存在的数据做多源补抓。未知时仍先补全请求区间，再用
            # 第一根可靠K线推断，兼容旧库。
            symbol_row = repo.get_symbol(symbol)
            listed_date = getattr(symbol_row, "listed_date", None) if symbol_row else None
            listing_source = "database" if listed_date else None
            actual = sorted({b["trade_date"] for b in bars
                             if start <= b["trade_date"] <= end})
            actual_set = set(actual)
            if listed_date is None and actual and actual[0] > expected[0]:
                # 不能把任意首根K线都当上市日：旧标的若仅缺区间第一天，会
                # 被错误“年轻化”。仅当前缀至少缺5个交易日、且从首根K线到
                # 区间末尾基本连续时，才把它视作新上市标的的有效上市日。
                prefix_days = [d for d in expected if d < actual[0]]
                post_first = {d for d in expected if d >= actual[0]}
                post_coverage = (len(actual_set & post_first) / len(post_first)
                                 if post_first else 0.0)
                if len(prefix_days) >= 5 and post_coverage >= self.min_coverage:
                    listed_date = actual[0]
                    listing_source = "inferred_first_available_bar"
                    try:
                        repo.set_symbol_listed_date(symbol, listed_date)
                    except Exception as exc:
                        logger.warning("补记上市日失败 %s/%s: %s", symbol, listed_date, exc)
            fetch_expected_set = {d for d in expected_set
                                  if not listed_date or d >= listed_date}
            missing = sorted(fetch_expected_set - actual_set)
            gap_diagnostics: Dict[str, Any] = {}
            if missing and gap_fill:
                # 主源即便返回了一大段历史，也要继续询问备用/实时源逐缺口补齐。
                get_market_service().fill_daily_gaps(
                    symbol, missing, self.asset_type_for(symbol),
                    diagnostics=gap_diagnostics)
                self._daily_cache.pop(f"{symbol}:{load_start}:{end}", None)
                bars = self.load_all_daily(symbol, load_start, end)
                actual = sorted({b["trade_date"] for b in bars
                                 if start <= b["trade_date"] <= end})
                actual_set = set(actual)
                missing = sorted(fetch_expected_set - actual_set)

            # 优先使用基础资料中的上市日；旧数据没有上市日时，若已取得一段
            # 连续行情，则以首根可用K线作为保守推断并补记数据库。这样新基金
            # 在上市前的自然空白不会被误报为行情丢失。
            # 数据库若意外记录了晚于真实首根K线的日期，以真实行情为准。
            if actual and listed_date and actual[0] < listed_date:
                listed_date = actual[0]
                listing_source = "first_available_bar"

            eligible_expected = [d for d in expected if not listed_date or d >= listed_date]
            eligible_set = set(eligible_expected)
            pre_listing = [d for d in expected if listed_date and d < listed_date]
            post_listing_missing = sorted(eligible_set - actual_set)
            if gap_diagnostics:
                gap_diagnostics["remaining_days"] = [str(d) for d in post_listing_missing]
            covered = actual_set & eligible_set
            coverage = len(covered) / len(eligible_set) if eligible_set else 0.0
            status = "complete"
            warning = None
            if not actual:
                status = "unavailable"
                warning = "所选区间无可用行情，已从交易信号中跳过"
            elif coverage < self.min_coverage:
                status = "partial"
                warning = (f"上市后仍缺失 {len(post_listing_missing)}/"
                           f"{len(eligible_expected)} 个交易日；回测仅使用已有行情，"
                           "缺失日不生成交易信号")
            if actual:
                available_symbols += 1
            report = {
                "symbol": symbol,
                "asset_type": self.asset_type_for(symbol),
                "requested_start": str(start), "requested_end": str(end),
                "actual_start": str(actual[0]) if actual else None,
                "actual_end": str(actual[-1]) if actual else None,
                "requested_expected_days": len(expected),
                "expected_days": len(eligible_expected), "actual_days": len(covered),
                "coverage": round(coverage, 6),
                "missing_days": [str(d) for d in post_listing_missing],
                "pre_listing_days": len(pre_listing),
                "listed_date": str(listed_date) if listed_date else None,
                "listing_date_source": listing_source,
                "status": status,
                "warning": warning,
                "gap_fill": gap_diagnostics,
                "price_adjustments": [
                    {**e,
                     "trade_date": str(e["trade_date"]),
                     "previous_trade_date": str(e["previous_trade_date"])}
                    for e in self._price_adjustments.get(symbol, [])
                    if start <= e["trade_date"] <= end
                ],
            }
            reports[symbol] = report
            boundary_ok = bool(actual and eligible_expected and
                               actual[0] <= eligible_expected[0] and
                               actual[-1] == eligible_expected[-1])
            report["boundary_ok"] = boundary_ok
            if (gap_fill and coverage < self.min_coverage
                    and gap_diagnostics.get("network_failed")):
                raise BacktestDataError(
                    f"{symbol} 行情补抓网络失败：所有可用数据源经 "
                    f"{gap_diagnostics.get('attempts', 1)} 轮重试均不可达；"
                    "数据库已有数据未达到完整性要求，请检查网络后重试")
            # 回测与图表共用这份内存快照。只记录用户选择区间，避免把预热K线
            # 混入买卖点图；快照随后随 metrics_json 一起持久化。
            self._snapshot[symbol] = [dict(b) for b in bars
                                      if start <= b["trade_date"] <= end]
        if not available_symbols:
            network_failed = any(r.get("gap_fill", {}).get("network_failed")
                                 for r in reports.values())
            if network_failed:
                raise BacktestDataError("所选标的行情补抓均因网络失败，数据库也无可用数据，请检查网络后重试")
            raise BacktestDataError("所选标的在该区间均无可用行情，可能尚未上市或免费数据源不提供历史数据")
        self._coverage = reports
        self._prepared_daily_key = prepare_key
        return reports

    def coverage_report(self) -> Dict[str, Dict[str, Any]]:
        return self._coverage

    def snapshot(self) -> Dict[str, List[dict]]:
        return self._snapshot

    def snapshot_hash(self) -> str:
        serializable = {
            sym: [{**b, "trade_date": str(b.get("trade_date"))} for b in bars]
            for sym, bars in sorted(self._snapshot.items())
        }
        raw = json.dumps(serializable, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _normalize_price_regimes(
            bars: List[dict], asset_type: str = "etf"
    ) -> tuple[List[dict], List[Dict[str, Any]]]:
        """把增量采集造成的混合复权区段统一到最后一段价格基准。

        只处理超过对应品种涨跌停幅度并额外留有 8% 安全垫的断层，且最低
        触发阈值为 25%，因此正常涨跌停、隔夜波动不会被平滑。算法从最新
        行情向前反向调整历史 OHLC，当前价格保持不变，经济价值连续。
        """
        if len(bars) < 2:
            return [dict(b) for b in bars], []
        ordered = sorted((dict(b) for b in bars), key=lambda b: b["trade_date"])
        factors = [1.0] * len(ordered)
        adjustments: List[Dict[str, Any]] = []
        current_factor = 1.0
        for i in range(len(ordered) - 1, 0, -1):
            cur = ordered[i]
            prev = ordered[i - 1]
            prev_close = float(prev.get("close") or 0)
            cur_open = float(cur.get("open") or 0)
            if prev_close <= 0 or cur_open <= 0:
                factors[i - 1] = current_factor
                continue
            raw_ratio = cur_open / prev_close
            try:
                from core.symbol_utils import price_limit_pct
                limit = float(price_limit_pct(
                    str(cur.get("symbol") or prev.get("symbol") or ""), asset_type))
            except Exception:
                limit = 0.10
            hard_gap = max(0.25, limit + 0.08)
            if raw_ratio < 1 - hard_gap or raw_ratio > 1 + hard_gap:
                previous_factor = cur_open * current_factor / prev_close
                adjustments.append({
                    "trade_date": cur["trade_date"],
                    "previous_trade_date": prev["trade_date"],
                    "raw_ratio": round(raw_ratio, 6),
                    "history_factor": round(previous_factor, 9),
                    "reason": "detected_adjustment_regime_change",
                })
                logger.warning(
                    "检测到复权口径断层 %s %s→%s: %.4f→%.4f (%.3fx)，"
                    "历史段按 %.6f 反向调整",
                    cur.get("symbol") or prev.get("symbol"),
                    prev.get("trade_date"), cur.get("trade_date"),
                    prev_close, cur_open, raw_ratio, previous_factor)
                current_factor = previous_factor
            factors[i - 1] = current_factor

        normalized: List[dict] = []
        for b, factor in zip(ordered, factors):
            out = dict(b)
            if abs(factor - 1.0) > 1e-12:
                for field in ("open", "high", "low", "close"):
                    out[field] = round(float(out[field]) * factor, 6)
                out["price_adjustment_factor"] = round(factor, 9)
            normalized.append(out)
        adjustments.reverse()
        return normalized, adjustments

    @staticmethod
    def _smooth_ex_dividend(bars: List[dict], jump_threshold: float = None) -> List[dict]:
        """
        除息平滑: 修正前复权(qfq)数据漏处理的 ETF 分红除息跳变。
        阈值语义: 单日跳空幅度超过 ±(1-jump_threshold) 判定为除息跳变并平滑。
        修复: 原硬编码 0.78(±22%) 会把 ±20% 涨跌幅品种(588xxx/159915等)的
        真实涨跌停/复牌跳空误判为除息并重算整个后续序列。默认 0.88(±12%)
        对 10% 品种安全(极限涨跌 0.90/1.111 不触发), 可在 config.yaml
        backtest.ex_dividend_jump_ratio 调整。
        """
        if jump_threshold is None:
            try:
                from core.config import get_settings
                jump_threshold = float(get_settings().get(
                    "backtest.ex_dividend_jump_ratio", 0.88))
            except Exception:
                jump_threshold = 0.88
        if not bars:
            return bars
        out: List[dict] = []
        factor = 1.0
        for i, b in enumerate(bars):
            if out:
                prev_close = out[-1]["close"]
                if prev_close > 0:
                    ratio = b["open"] / prev_close
                    # 修复: 阈值按标的涨跌幅动态取 —— ±20% 品种(588/159915等)
                    # 真实涨跌停跳空 ratio≈0.80/1.20, 原 0.88 阈值会误判为除息,
                    # 整个后续序列被错误缩放, 系统性污染回测数据。
                    thr = jump_threshold
                    try:
                        from core.symbol_utils import price_limit_pct
                        limit = price_limit_pct(b.get("symbol", ""), "etf")
                        if limit >= 0.20:
                            thr = min(jump_threshold, 0.80)
                    except Exception:
                        pass
                    if ratio < thr or ratio > (2 - thr):
                        # 除息跳变: 按开盘跳空比例调整(把分红算回持仓)
                        factor = prev_close / b["open"]
                        logger.info("检测到除息跳变 %s %s: 昨收%.4f→今开%.4f (%.1f%%), 已平滑",
                                    b["symbol"], b["trade_date"], prev_close, b["open"],
                                    (ratio - 1) * 100)
            if factor != 1.0:
                b = {**b,
                     "open": round(b["open"] * factor, 4),
                     "high": round(b["high"] * factor, 4),
                     "low": round(b["low"] * factor, 4),
                     "close": round(b["close"] * factor, 4)}
            out.append(b)
        return out

    # ------------------------------------------------------------------
    def load_all_minute(self, symbol: str, day: date, freq: str = "5m") -> List[dict]:
        """加载单日分钟K。"""
        key = f"{symbol}:{day}:{freq}"
        if key in self._minute_cache:
            return self._minute_cache[key]
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        rows = repo.get_minute_bars(symbol, start, end, freq)
        norm = [{
            "symbol": r.symbol, "bar_time": r.bar_time,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount,
        } for r in rows]
        self._minute_cache[key] = norm
        return norm

    # ------------------------------------------------------------------
    def load_benchmark(self, symbol: str, start: date, end: date) -> List[dict]:
        """基准指数日K(沪深300), 无未来函数(纯行情序列)。"""
        key = f"bench:{symbol}:{start}:{end}"
        if key in self._daily_cache:
            return self._daily_cache[key]
        if not self.online_fill:
            # Reproducible batch/dynamic runs must not change inputs halfway
            # through because an optional benchmark network call succeeded.
            bars = []
        else:
            try:
                bars = get_market_service().get_index_bars(symbol, start, end)
            except Exception as exc:
                logger.warning("基准指数获取失败 %s: %s", symbol, exc)
                bars = []
        self._daily_cache[key] = bars
        return bars
