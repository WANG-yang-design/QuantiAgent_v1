# -*- coding: utf-8 -*-
"""
情绪数据服务 (facade)
=====================
舆情聚合/情绪打分。实现位于 data_service.news_service.NewsService:
- rule_sentiment: 词典打分(LLM 未配置时兜底)
- fetch_and_store_sentiment: 采集入库
- get_sentiment_stats: 聚合统计
"""
from typing import Any, Dict

from data_service.news_service import get_news_service


def get_sentiment_stats(symbol: str, hours: int = 24) -> Dict[str, Any]:
    return get_news_service().get_sentiment_stats(symbol, hours)


def score_text(text: str) -> float:
    """文本情绪打分 -1~1。"""
    return get_news_service().rule_sentiment(text)
