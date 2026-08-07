# -*- coding: utf-8 -*-
"""
新闻/公告/舆情 数据服务
======================
统一新闻公告入口, 支持入库去重与 RAG 索引联动。
情绪分由词典/LLM 计算, 供情绪分析师使用。
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from data_sources.hub import get_hub
from database import repository as repo
from memory.audit_log import AuditLogger

logger = logging.getLogger("data.news_service")

# 简易中英文情绪词典(规则兜底, LLM 可用时用 LLM 精算)
_POSITIVE = {"利好", "增长", "盈利", "中标", "突破", "大涨", "买入", "增持", "回购",
             "分红", "降息", "宽松", "超预期", "盈利超预期", "签订", "获批", "涨停"}
_NEGATIVE = {"减持", "利空", "亏损", "下降", "诉讼", "处罚", "退市", "立案", "暴跌",
             "卖出", "风险", "违约", "下调", "商誉减值", "质押", "解禁", "ST", "跌停",
             "造假", "调查", "暂停上市"}


class NewsService:
    """新闻/公告/舆情服务。"""

    def __init__(self):
        self.hub = get_hub()

    # ---------------- 新闻 ----------------
    def fetch_and_store_news(self, symbols: List[str]) -> int:
        """拉取并入库新闻, 返回实际新增条数(修复: 原把去重跳过的也计入)。"""
        added = 0
        for symbol in symbols:
            try:
                news, source = self.hub.get_news(symbol, limit=20)
            except Exception as exc:
                logger.warning("新闻获取失败 %s: %s", symbol, exc)
                continue
            for n in news:
                n["sentiment_score"] = self.rule_sentiment(n["title"] + n["content"])
            try:
                added += repo.upsert_news(news)
            except Exception as exc:
                logger.warning("新闻入库失败 %s: %s", symbol, exc)
        return added

    @staticmethod
    def rule_sentiment(text: str) -> float:
        """词典情绪打分 -1~1 (LLM 不可用时兜底)。"""
        if not text:
            return 0.0
        pos = sum(1 for w in _POSITIVE if w in text)
        neg = sum(1 for w in _NEGATIVE if w in text)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / max(total, 1)

    def get_recent_news(self, symbol: str, hours: int = 48, limit: int = 30) -> List[dict]:
        start = datetime.now() - timedelta(hours=hours)
        return repo.get_news(symbol=symbol, start=start, limit=limit)

    # ---------------- 公告 ----------------
    def fetch_and_store_announcements(self, symbols: List[str]) -> int:
        added = 0
        for symbol in symbols:
            try:
                anns, source = self.hub.get_announcements(symbol, limit=20)
            except Exception as exc:
                logger.warning("公告获取失败 %s: %s", symbol, exc)
                continue
            for a in anns:
                a["event_type"] = self._classify_event(a["title"])
                a["risk_level"] = self._risk_level(a["title"])
            try:
                added += repo.upsert_announcements(anns)
            except Exception as exc:
                logger.warning("公告入库失败 %s: %s", symbol, exc)
        return added

    def get_recent_announcements(self, symbol: str, days: int = 7, limit: int = 30) -> List[dict]:
        start = datetime.now() - timedelta(days=days)
        return repo.get_announcements(symbol=symbol, start=start, limit=limit)

    @staticmethod
    def _classify_event(title: str) -> str:
        rules = {
            "减持": "股东减持", "增持": "股东增持", "回购": "回购",
            "分红": "分红派息", "业绩": "业绩预告", "重组": "资产重组",
            "中标": "重大合同", "质押": "股权质押", "解禁": "限售解禁",
            "收购": "收购", "诉讼": "诉讼仲裁", "定增": "定向增发",
        }
        for kw, ev in rules.items():
            if kw in title:
                return ev
        return "其他"

    @staticmethod
    def _risk_level(title: str) -> str:
        high = ["退市", "立案", "造假", "处罚", "暂停上市", "破产", "调查"]
        for kw in high:
            if kw in title:
                return "high"
        medium = ["减持", "质押", "解禁", "诉讼", "亏损", "下调", "违约", "商誉减值"]
        for kw in medium:
            if kw in title:
                return "medium"
        return "low" if "ST" in title or "风险" in title else "none"

    # ---------------- 舆情 ----------------
    def fetch_and_store_sentiment(self, symbols: List[str]) -> int:
        added = 0
        for symbol in symbols:
            try:
                posts, source = self.hub.get_sentiment(symbol, limit=30)
            except Exception as exc:
                logger.warning("舆情获取失败 %s: %s", symbol, exc)
                continue
            for p in posts:
                p["score"] = self.rule_sentiment(p.get("content", ""))
            for p in posts:
                try:
                    repo.save_sentiment(p)
                    added += 1
                except Exception:
                    pass
        return added

    def get_sentiment_stats(self, symbol: str, hours: int = 24) -> Dict[str, Any]:
        """聚合情绪统计(供情绪分析师)。"""
        start = datetime.now() - timedelta(hours=hours)
        recs = repo.get_sentiment(symbol, start, datetime.now(), limit=300)
        if not recs:
            return {"count": 0, "avg_score": 0.0, "heat": 0.0, "negative_ratio": 0.0}
        scores = [r.score for r in recs]
        heat = sum(r.heat for r in recs if r.heat)
        return {
            "count": len(recs),
            "avg_score": sum(scores) / len(scores),
            "heat": heat,
            "negative_ratio": sum(1 for s in scores if s < -0.2) / len(scores),
        }


_news_service: Optional[NewsService] = None


def get_news_service() -> NewsService:
    global _news_service
    if _news_service is None:
        _news_service = NewsService()
    return _news_service
