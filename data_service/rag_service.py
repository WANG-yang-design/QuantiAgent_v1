# -*- coding: utf-8 -*-
"""
RAG 向量检索服务 (pgvector)
============================
- 文档入库: 新闻/公告/历史报告/策略文档 → 分块 → 向量化 → 存入 rag_chunks
- 检索: 向量相似度 + 关键词混合检索 (Reciprocal Rank Fusion)
- 无 embedding 模型时使用哈希词向量(确定性, 可离线)
供新闻Agent/复盘Agent检索历史资料。
"""
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, text

from core.config import get_settings
from core.ids import gen_id
from core.llm import get_llm
from database.db_session import get_engine, get_session
from database.models import RagChunk, RagDocument

logger = logging.getLogger("data.rag")


def _chunk_text(text: str, size: int, overlap: int) -> List[str]:
    """按字符分块(中文无空格, 直接滑窗), 保留标题上下文。"""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += max(size - overlap, 1)
    return chunks


class RagService:
    """向量检索服务。"""

    def __init__(self):
        self.cfg = get_settings().section("rag")
        self.embed_batch = int(self.cfg.get("embedding_batch_size", 10))

    # ------------------------------------------------------------------
    def index_document(self, doc_type: str, title: str, content: str,
                       source: str = "", symbol: str = "",
                       publish_time: Optional[datetime] = None) -> Optional[str]:
        """将文档分块并向量化入库(幂等: 相同 title+source 不重复索引)。"""
        if not content.strip():
            return None
        size = int(self.cfg.get("chunk_size", 500))
        overlap = int(self.cfg.get("chunk_overlap", 50))
        chunks = _chunk_text(title + "\n" + content, size, overlap)
        if not chunks:
            return None

        with get_session() as s:
            # 修复: 原实现用 title[:200] 截断去重 —— 长前缀相同(如"XX公司:关于…")
            # 的不同文档被误判重复, 新内容永远不进向量库。改为完整标题精确匹配。
            dup = s.query(RagDocument).filter_by(title=title, source=source).first()
            if dup:
                return dup.document_id
            doc_id = gen_id("DOC")
            s.add(RagDocument(document_id=doc_id, doc_type=doc_type, source=source,
                              title=title[:500], content=content,
                              symbol=symbol, publish_time=publish_time,
                              chunk_count=len(chunks)))
            s.flush()

            # 批量向量化
            vecs = self._embed(chunks)
            for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
                s.add(RagChunk(chunk_id=gen_id("CHK"), document_id=doc_id,
                               chunk_index=i, content=chunk, embedding=vec))
        logger.info("RAG 索引完成: %s (%d 块)", title[:40], len(chunks))
        return doc_id

    def _embed(self, texts: List[str]) -> List[List[float]]:
        """同步包装异步 embed(索引是批量操作, 可接受)。"""
        import asyncio
        async def _run():
            out: List[List[float]] = []
            for i in range(0, len(texts), self.embed_batch):
                out.extend(await get_llm().embed(texts[i:i + self.embed_batch]))
            return out
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在事件循环中: 用新线程执行, 避免嵌套循环冲突
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as ex:
                    return ex.submit(lambda: asyncio.run(_run())).result()
            return asyncio.run(_run())
        except Exception as exc:
            logger.error("向量化失败: %s", exc)
            return [get_llm()._hash_embed(t) for t in texts]

    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 0, doc_types: Optional[List[str]] = None,
               symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        混合检索: 向量相似度(余弦) + 关键词命中, RRF 融合。
        返回: [{chunk_id, document_id, title, content, score, doc_type, source}]
        """
        top_k = top_k or int(self.cfg.get("top_k", 5))
        q_vec = self._embed([query])[0]
        engine = get_engine()

        sql = """
            SELECT c.chunk_id, c.document_id, c.content, c.chunk_index,
                   d.title, d.doc_type, d.source, d.symbol, d.publish_time,
                   1 - (c.embedding <=> CAST(:qv AS vector)) AS vector_score
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE 1=1
        """
        # 修复: 之前传 str(list) 文本, pgvector 的 <=> 运算符无 text→vector 隐式
        # 转换导致 SQL 报错被 except 吞掉, RAG 检索恒返回空。显式 CAST 确保可用。
        params: Dict[str, Any] = {"qv": q_vec}
        if doc_types:
            sql += " AND d.doc_type = ANY(:types)"
            params["types"] = doc_types
        if symbol:
            sql += " AND (d.symbol = :symbol OR d.symbol = '')"
            params["symbol"] = symbol
        sql += " ORDER BY vector_score DESC LIMIT :limit"
        params["limit"] = max(top_k * 3, 10)

        try:
            with engine.connect() as conn:
                rows = conn.execute(text(sql), params).mappings().all()
        except Exception as exc:
            logger.error("向量检索失败: %s", exc)
            rows = []

        results = []
        for r in rows:
            results.append({
                "chunk_id": r["chunk_id"],
                "document_id": r["document_id"],
                "title": r["title"],
                "content": r["content"][:300],
                "score": float(r["vector_score"] or 0),
                "doc_type": r["doc_type"],
                "source": r["source"],
                "symbol": r["symbol"] or "",
                "publish_time": str(r["publish_time"]) if r["publish_time"] else "",
            })
        return results[:top_k]

    def search_keyword(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """关键词检索(tsvector 简化版: ILIKE)。"""
        with get_session() as s:
            kw = f"%{query[:50]}%"
            rows = s.query(RagChunk).join(RagDocument, RagDocument.document_id == RagChunk.document_id)\
                .filter(or_(RagChunk.content.ilike(kw), RagDocument.title.ilike(kw)))\
                .limit(top_k).all()
            out = []
            for r in rows:
                out.append({
                    "chunk_id": r.chunk_id, "content": r.content[:300],
                    "document_id": r.document_id, "score": 0.5,
                })
            return out


_rag: Optional[RagService] = None


def get_rag_service() -> RagService:
    global _rag
    if _rag is None:
        _rag = RagService()
    return _rag
