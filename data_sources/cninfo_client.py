# -*- coding: utf-8 -*-
"""
巨潮资讯客户端 (公告主源)
========================
公告权威来源, 通过 akshare 的巨潮接口获取。
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from data_sources.base import BaseDataSource
from data_sources.akshare_client import _safe_float, _safe_str

logger = logging.getLogger("data.cninfo")


class CninfoClient(BaseDataSource):
    """巨潮资讯公告客户端。"""

    name = "cninfo"

    def __init__(self):
        import akshare as ak
        self.ak = ak

    def get_announcements(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            # 按日期拉巨潮公告全表, 再过滤目标标的(免费接口粒度)
            df = self.ak.stock_notice_report(
                symbol="全部", date=datetime.now().strftime("%Y%m%d"))
            if df is None or df.empty:
                return rows
            sub = df[df["代码"].astype(str) == symbol].head(limit)
            for _, r in sub.iterrows():
                rows.append({
                    "announcement_id": f"cninfo_{symbol}_{_safe_str(r.get('公告标题'))}_{r.name}",
                    "symbol": symbol,
                    "title": _safe_str(r.get("公告标题")),
                    "url": _safe_str(r.get("网址")),
                    "publish_time": r.get("公告日期"),
                })
        except Exception as exc:
            logger.warning("巨潮公告失败 %s: %s", symbol, exc)
        return rows
