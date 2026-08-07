# -*- coding: utf-8 -*-
"""
AkShare 数据源客户端 (主源, 免费)
=================================
覆盖: 日K/分钟K/实时行情/盘口/资金流/新闻/公告/舆情/交易日历/指数/ETF。
所有接口做列名容错映射 + 单位统一(成交量→股/份, 金额→元)。
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import ROOT_DIR
from data_sources.base import BaseDataSource

logger = logging.getLogger("data.akshare")

# 全市场ETF列表缓存(该接口分页拉取约20-60秒, 必须缓存: 内存60s + 磁盘120s)
_spot_cache: Dict[str, Any] = {"ts": 0.0, "df": None}
_spot_lock = threading.RLock()
_SPOT_TTL = 60.0
_SPOT_FILE = ROOT_DIR / "data" / "etf_spot_cache.json"
_SPOT_FILE_TTL = 120.0
_spot_refreshing = False
_spot_last_attempt_ts = 0.0          # 后台刷新最小重试间隔(修复: 限流时不锤东财)
_SPOT_REFRESH_MIN_INTERVAL = 30.0


def _disk_save(df):
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            df.to_json(_SPOT_FILE, orient="split", force_ascii=False)
    except Exception as exc:
        logger.debug("ETF spot 磁盘缓存写入失败: %s", exc)


def _disk_load():
    try:
        import pandas as pd
        if _SPOT_FILE.exists() and time.time() - _SPOT_FILE.stat().st_mtime < _SPOT_FILE_TTL:
            df = pd.read_json(_SPOT_FILE, orient="split")
            if not df.empty:
                return df
    except Exception as exc:
        logger.debug("ETF spot 磁盘缓存读取失败: %s", exc)
    return None


def _safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default      # NaN 检查
    except Exception:
        return default


def _safe_str(v, default: str = "") -> str:
    try:
        return str(v).strip() if v is not None else default
    except Exception:
        return default


class AkShareClient(BaseDataSource):
    """主源: AkShare (底层多数接口来自东方财富)。"""

    name = "akshare"

    # ------------------------------------------------------------------
    def __init__(self):
        import akshare as ak
        self.ak = ak

    # ---------------- 日K ----------------
    def get_daily_bars(self, symbol: str, start: date, end: date,
                       asset_type: str = "etf") -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        try:
            if asset_type == "etf":
                df = self.ak.fund_etf_hist_em(symbol=symbol, period="daily",
                                              start_date=s, end_date=e, adjust="qfq")
            else:
                df = self.ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                             start_date=s, end_date=e, adjust="qfq")
        except Exception as exc:
            raise RuntimeError(f"AkShare 日K失败 {symbol}: {exc}") from exc
        if df is None or df.empty:
            return rows
        for _, r in df.iterrows():
            d = r.get("日期") or r.get("date")
            if d is None:
                continue
            if isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            rows.append({
                "symbol": symbol,
                "trade_date": d,
                "open": _safe_float(r.get("开盘") or r.get("open")),
                "high": _safe_float(r.get("最高") or r.get("high")),
                "low": _safe_float(r.get("最低") or r.get("low")),
                "close": _safe_float(r.get("收盘") or r.get("close")),
                "volume": _safe_float(r.get("成交量") or r.get("volume")) * 100,  # 手→股
                "amount": _safe_float(r.get("成交额") or r.get("amount")),
                "source": self.name,
            })
        return rows

    # ---------------- 分钟K ----------------
    def get_minute_bars(self, symbol: str, start: datetime, end: datetime,
                        freq: str = "5m", asset_type: str = "etf") -> List[Dict[str, Any]]:
        period_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
        period = period_map.get(freq, "5")
        rows: List[Dict[str, Any]] = []
        try:
            if asset_type == "etf":
                df = self.ak.fund_etf_hist_min_em(
                    symbol=symbol, period=period,
                    start_date=start.strftime("%Y-%m-%d 09:30:00"),
                    end_date=end.strftime("%Y-%m-%d 15:00:00"), adjust="")
            else:
                df = self.ak.stock_zh_a_hist_min_em(
                    symbol=symbol, period=period,
                    start_date=start.strftime("%Y-%m-%d 09:30:00"),
                    end_date=end.strftime("%Y-%m-%d 15:00:00"), adjust="")
        except Exception as exc:
            raise RuntimeError(f"AkShare 分钟K失败 {symbol}: {exc}") from exc
        if df is None or df.empty:
            return rows
        for _, r in df.iterrows():
            t = r.get("时间") or r.get("day") or r.get("time")
            if t is None:
                continue
            if isinstance(t, str):
                t = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
            elif hasattr(t, "to_pydatetime"):
                t = t.to_pydatetime()
            # 修复: 原实现未按请求起止时间过滤, 请求任意窗口都返回当天全部分钟K
            if not (start <= t <= end):
                continue
            rows.append({
                "symbol": symbol,
                "bar_time": t,
                "freq": freq,
                "open": _safe_float(r.get("开盘") or r.get("open")),
                "high": _safe_float(r.get("最高") or r.get("high")),
                "low": _safe_float(r.get("最低") or r.get("low")),
                "close": _safe_float(r.get("收盘") or r.get("close")),
                "volume": _safe_float(r.get("成交量") or r.get("volume") or r.get("vol")) * 100,
                "amount": _safe_float(r.get("成交额") or r.get("amount")),
                "source": self.name,
            })
        return rows

    # ---------------- 实时行情 ----------------
    def get_realtime_quote(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        # ETF/股票 全市场 spot 一次拉全, 这里按 symbol 过滤
        df, fetched_ts = self._spot_df(asset_type)
        row = df[df["代码"].astype(str) == symbol]
        if row.empty:
            raise RuntimeError(f"AkShare 无 {symbol} 实时行情")
        r = row.iloc[0]
        # 修复: 行情时间用数据实际获取时刻而非读取时刻 —— 原实现用 datetime.now(),
        # 陈旧缓存(磁盘120s+内存60s)被盖上当前时间戳, 数据质量层的 DELAYED
        # 检查永远不触发, 盘中策略会拿几分钟前的价格当实时价进入决策链。
        quote_time = datetime.fromtimestamp(fetched_ts) if fetched_ts else datetime.now()
        return {
            "symbol": symbol,
            "quote_time": quote_time,
            "latest_price": _safe_float(r.get("最新价")),
            "change_pct": _safe_float(r.get("涨跌幅")),
            "volume": _safe_float(r.get("成交量")),
            "amount": _safe_float(r.get("成交额")),
            "high": _safe_float(r.get("最高")),
            "low": _safe_float(r.get("最低")),
            "open": _safe_float(r.get("今开")),
            "prev_close": _safe_float(r.get("昨收")),
            "iopv": _safe_float(r.get("IOPV实时估值")),
            "source": self.name,
        }

    def _spot_df(self, asset_type: str):
        """
        全市场 spot 缓存(内存60秒 + 磁盘120秒, 陈旧数据先返回 + 后台刷新)。
        东财限流时全量分页需1分钟+, 无缓存会导致首次请求极慢。
        返回 (df, 数据实际获取时刻的epoch秒), 供实时行情时间戳使用。
        """
        global _spot_refreshing
        if asset_type != "etf":
            return self.ak.stock_zh_a_spot_em(), time.time()
        with _spot_lock:
            if _spot_cache["df"] is not None and time.time() - _spot_cache["ts"] < _SPOT_TTL:
                return _spot_cache["df"], _spot_cache["ts"]
        # 磁盘缓存(陈旧可先用, 后台刷新)
        disk = _disk_load()
        if disk is not None:
            with _spot_lock:
                _spot_cache["df"] = disk
                _spot_cache["ts"] = time.time()
            if not _spot_refreshing:
                _spot_refreshing = True
                threading.Thread(target=self._refresh_spot_background, daemon=True).start()
            return disk, _spot_cache["ts"]
        # 修复(重要): 磁盘缓存过期(>120s)且内存有旧数据时, 原实现直接落到
        # "首次同步拉取"分支, 但 `_spot_cache["df"] is None` 恒为 False ——
        # 既跳过拉取, 也不再启动后台刷新, 全市场ETF列表永久冻结。
        # 服务器上 akshare 被东财限流后, 盯盘/监控页行情从此不再更新。
        # 现在: 磁盘过期 → 先用旧数据(不阻塞请求), 同时始终启动后台刷新
        # (带最小重试间隔, 限流时不会每10秒锤一次东财)。
        with _spot_lock:
            stale_df = _spot_cache["df"]
        if stale_df is not None:
            if not _spot_refreshing and \
                    time.time() - _spot_last_attempt_ts >= _SPOT_REFRESH_MIN_INTERVAL:
                _spot_last_attempt_ts = time.time()
                _spot_refreshing = True
                threading.Thread(target=self._refresh_spot_background, daemon=True).start()
            with _spot_lock:
                return _spot_cache["df"], _spot_cache["ts"]
        # 完全无缓存: 同步拉取(仅此一次, 之后都有缓存)
        df = self.ak.fund_etf_spot_em()
        with _spot_lock:
            _spot_cache["df"] = df
            _spot_cache["ts"] = time.time()
        _disk_save(df)
        return df, _spot_cache["ts"]

    def _refresh_spot_background(self):
        """后台刷新全市场ETF列表(限流时静默失败, 保留旧数据)。"""
        global _spot_refreshing
        try:
            df = self.ak.fund_etf_spot_em()
            with _spot_lock:
                _spot_cache["df"] = df
                _spot_cache["ts"] = time.time()
            _disk_save(df)
            logger.info("全市场ETF列表后台刷新完成: %d 行", len(df))
        except Exception as exc:
            logger.warning("全市场ETF后台刷新失败(沿用旧数据): %s", exc)
        finally:
            _spot_refreshing = False

    # ---------------- 五档盘口 ----------------
    def get_order_book(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        df = self.ak.stock_bid_ask_em(symbol=symbol)
        if df is None or df.empty:
            raise RuntimeError(f"AkShare 无 {symbol} 盘口")
        data = dict(zip(df["item"].astype(str).tolist(), df["value"].tolist()))
        bid1 = _safe_float(data.get("买一价"))
        ask1 = _safe_float(data.get("卖一价"))
        spread = (ask1 - bid1) / ask1 if ask1 > 0 else 0.0
        ob_json = {str(k): _safe_float(v) for k, v in data.items()}
        return {
            "symbol": symbol,
            "snapshot_time": datetime.now(),
            "bid1": bid1,
            "ask1": ask1,
            "bid_vol1": _safe_float(data.get("买一量")),
            "ask_vol1": _safe_float(data.get("卖一量")),
            "spread": spread,
            "order_book_json": ob_json,
            "source": self.name,
        }

    # ---------------- 资金流 ----------------
    def get_money_flow(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        market = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
        df = self.ak.stock_individual_fund_flow(stock=symbol, market=market)
        if df is None or df.empty:
            raise RuntimeError(f"AkShare 无 {symbol} 资金流")
        last = df.iloc[-1]
        return {
            "symbol": symbol,
            "record_time": datetime.now(),
            "main_inflow": _safe_float(last.get("主力净流入-净额")),
            "net_inflow": _safe_float(last.get("主力净流入-净额")),
            "super_inflow": _safe_float(last.get("超大单净流入-净额")),
            "large_inflow": _safe_float(last.get("大单净流入-净额")),
            "medium_inflow": _safe_float(last.get("中单净流入-净额")),
            "small_inflow": _safe_float(last.get("小单净流入-净额")),
            "main_inflow_ratio": _safe_float(last.get("主力净流入-净占比")),
            "source": self.name,
        }

    # ---------------- 新闻 / 公告 / 舆情 ----------------
    @staticmethod
    def _dedup_id(*parts) -> str:
        """内容哈希 ID: 修复: 原实现用 DataFrame 行号(r.name)生成 ID,
        不同日期行号相同 → 被 unique 约束去重丢弃(漏数据)。"""
        import hashlib
        raw = "|".join(str(x) for x in parts)
        return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]

    @staticmethod
    def _parse_pub_time(v, year: Optional[int] = None) -> Optional[datetime]:
        """
        解析来源发布时间(容忍股吧的"MM-DD HH:MM"、公告的"MM-DD"、纯"HH:MM"等
        无年份格式)。修复: 原实现原样透传, 无年份时间戳入库 PG 时报错,
        且被上层静默吞掉, 舆情/新闻数据大部分永久丢失。
        """
        if v is None or v == "" or v != v:   # 含 NaN
            return None
        if isinstance(v, datetime):
            return v
        s = str(v).strip()
        if not s:
            return None
        year = year or datetime.now().year
        formats = (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
        )
        for fmt in formats:
            try:
                return datetime.strptime(s[:len(fmt)], fmt)
            except ValueError:
                continue
        # 无年份: MM-DD HH:MM / MM-DD / HH:MM
        try:
            if ":" in s and "-" in s and len(s) <= 12:
                return datetime.strptime(f"{year}-{s}", "%Y-%m-%d %H:%M")
            if "-" in s and len(s) <= 5:
                return datetime.strptime(f"{year}-{s}", "%Y-%m-%d")
            if ":" in s and len(s) <= 5:
                return datetime.strptime(f"{year}-01-01 {s}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass
        logger.debug("无法解析发布时间: %r", s)
        return None

    def get_news(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            df = self.ak.stock_news_em(symbol=symbol)
            for _, r in df.head(limit).iterrows():
                title = _safe_str(r.get("新闻标题"))
                pub = _safe_str(r.get("发布时间"))
                pub_dt = self._parse_pub_time(r.get("发布时间"))
                rows.append({
                    "news_id": f"{self.name}_{symbol}_{self._dedup_id(symbol, title, pub)}",
                    "symbol": symbol,
                    "title": title,
                    "content": _safe_str(r.get("新闻内容")),
                    "publish_time": pub_dt or datetime.now(),
                    "source": "eastmoney",
                    "url": _safe_str(r.get("新闻链接")),
                })
        except Exception as exc:
            logger.warning("AkShare 新闻失败 %s: %s", symbol, exc)
        return rows

    def get_announcements(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            # 修复: 原实现只拉"当天"公告 —— 盘前/节假日补扫拿不到近几日公告,
            # 公告数据系统性缺失。改为回退近5个自然日逐日尝试。
            today = datetime.now()
            for back in range(5):
                day = today - timedelta(days=back)
                df = self.ak.stock_notice_report(symbol="全部", date=day.strftime("%Y%m%d"))
                if df is None or df.empty:
                    continue
                sub = df[df["代码"].astype(str) == symbol].head(limit)
                for _, r in sub.iterrows():
                    title = _safe_str(r.get("公告标题"))
                    pub = _safe_str(r.get("公告日期"))
                    rows.append({
                        "announcement_id": f"cninfo_{symbol}_{self._dedup_id(symbol, title, pub)}",
                        "symbol": symbol,
                        "title": title,
                        "url": _safe_str(r.get("网址")),
                        "publish_time": self._parse_pub_time(r.get("公告日期")) or datetime.now(),
                    })
                if rows:
                    break
        except Exception as exc:
            logger.warning("AkShare 公告失败 %s: %s", symbol, exc)
        return rows

    def get_sentiment(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        """东方财富股吧帖子(热度+标题)。"""
        rows: List[Dict[str, Any]] = []
        try:
            df = self.ak.stock_guba_em(symbol=symbol)
            for _, r in df.head(limit).iterrows():
                title = _safe_str(r.get("标题"))
                pub = _safe_str(r.get("发布时间"))
                rows.append({
                    "record_id": f"guba_{symbol}_{self._dedup_id(symbol, title, pub)}",
                    "symbol": symbol,
                    "platform": "guba",
                    "content": title,
                    "score": 0.0,            # 情绪分由 sentiment_service 用 LLM/词典计算
                    "heat": _safe_float(r.get("阅读")),
                    "publish_time": self._parse_pub_time(r.get("发布时间")) or datetime.now(),
                })
        except Exception as exc:
            logger.warning("AkShare 舆情失败 %s: %s", symbol, exc)
        return rows

    # ---------------- 交易日历 / 指数 / ETF ----------------
    def get_trade_calendar(self, start: date, end: date) -> List[date]:
        df = self.ak.tool_trade_date_hist_sina()
        dates = []
        for v in df["trade_date"].tolist():
            d = datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
            if start <= d <= end:
                dates.append(d)
        return dates

    def get_index_bars(self, index_code: str, start: date, end: date) -> List[Dict[str, Any]]:
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        df = self.ak.index_zh_a_hist(symbol=index_code, period="daily", start_date=s, end_date=e)
        rows = []
        for _, r in df.iterrows():
            d = r.get("日期")
            if isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            rows.append({
                "symbol": index_code,
                "trade_date": d,
                "open": _safe_float(r.get("开盘")),
                "high": _safe_float(r.get("最高")),
                "low": _safe_float(r.get("最低")),
                "close": _safe_float(r.get("收盘")),
                "volume": _safe_float(r.get("成交量")) * 100,
                "amount": _safe_float(r.get("成交额")),
                "source": self.name,
            })
        return rows

    def get_etf_spot(self) -> List[Dict[str, Any]]:
        # 复用缓存的全市场ETF列表(该接口分页拉取约20-60秒, 必须缓存)
        df, _ = self._spot_df("etf")
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "symbol": _safe_str(r.get("代码")),
                "name": _safe_str(r.get("名称")),
                "latest_price": _safe_float(r.get("最新价")),
                "change_pct": _safe_float(r.get("涨跌幅")),
                "amount": _safe_float(r.get("成交额")),
                "volume": _safe_float(r.get("成交量")),
                "iopv": _safe_float(r.get("IOPV实时估值")),
                "premium_rate": _safe_float(r.get("溢折率")),
                "high": _safe_float(r.get("最高")),
                "low": _safe_float(r.get("最低")),
                "open": _safe_float(r.get("今开")),
                "prev_close": _safe_float(r.get("昨收")),
            })
        return rows

    def get_etf_info(self, symbol: str) -> Dict[str, Any]:
        # 基础信息从全市场 ETF 列表过滤 + 附加接口(跟踪指数等)尽力获取
        spot = self.get_etf_spot()
        hit = next((x for x in spot if x["symbol"] == symbol), None)
        # 修复: 原实现用 startswith("1599") 判 QDII, 会把 159915/159949 等
        # 创业板(T+1)ETF 误标为 QDII → 风控跨市场/溢价规则错误。
        # 与 T+0 规则表一致: 跨境QDII均T+0(513xxx/511xxx/518xxx/519xxx + 精确表)
        try:
            from core.symbol_utils import is_t0_etf
            is_qdii = is_t0_etf(symbol)
        except Exception:
            is_qdii = symbol.startswith("513")
        info = {
            "symbol": symbol,
            "name": hit["name"] if hit else "",
            "tracking_index": "",
            "scale": 0.0,
            "fee_rate": 0.0,
            "fund_company": "",
            "is_qdii": is_qdii,
        }
        # 详情页接口限流时可能耗时60秒+, 用线程超时保护(10秒拿不到就返回基础信息)
        import concurrent.futures as cf
        ex = cf.ThreadPoolExecutor(1)
        try:
            fut = ex.submit(self.ak.fund_etf_fund_info_em, fund=symbol)
            df = fut.result(timeout=10)
            if df is not None and not df.empty:
                data = dict(zip(df["item"].tolist(), df["value"].tolist()))
                info["tracking_index"] = _safe_str(data.get("跟踪标的"))
                info["fund_company"] = _safe_str(data.get("基金公司"))
        except cf.TimeoutError:
            logger.warning("ETF详情页获取超时(限流), 使用基础信息: %s", symbol)
        except Exception as exc:
            logger.debug("ETF详情获取失败 %s: %s", symbol, exc)
        finally:
            ex.shutdown(wait=False)   # 后台线程继续跑, 不阻塞主流程
        return info

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        """股票基本面(个股指标, ETF 用不上)。"""
        try:
            df = self.ak.stock_individual_info_em(symbol=symbol)
            data = dict(zip(df["item"].tolist(), df["value"].tolist()))
            return {
                "symbol": symbol,
                "report_date": datetime.now().date(),
                "pe": _safe_float(data.get("市盈率(动态)")),
                "pb": _safe_float(data.get("市净率")),
                "roe": 0.0, "revenue_growth": 0.0, "profit_growth": 0.0,
                "source": self.name,
            }
        except Exception as exc:
            raise RuntimeError(f"AkShare 基本面失败 {symbol}: {exc}") from exc
