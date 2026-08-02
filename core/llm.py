# -*- coding: utf-8 -*-
"""
LLM 客户端 (OpenAI 兼容接口)
=============================
- 快/深模型路由: 按任务类型选择模型, 控制成本与质量
- 结构化输出: JSON + Pydantic 双重校验, 非法输出自动重试
- 幂等重试: 网络失败自动重试(指数退避), 连续失败触发通知
- 降级模式: 未配置 API Key 时自动切换到"规则模拟"输出,
  保证全链路可跑通, 填入真实配置后无缝切换
- 嵌入向量: 用于 RAG (pgvector 存储); 无 embedding 模型时用
  中文分词哈希向量兜底
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Type

import jieba
import numpy as np
from openai import AsyncOpenAI
from pydantic import BaseModel

from core.config import get_settings
from core.logging import get_logger, get_trace_id

logger = get_logger("agent.llm")


class LLMError(Exception):
    """LLM 调用异常(包含重试耗尽)。"""


class LLMClient:
    """OpenAI 兼容 LLM 客户端。"""

    def __init__(self):
        self.settings = get_settings()
        llm = self.settings.section("llm")
        self.mock = self.settings.mock_mode()
        self.client: Optional[AsyncOpenAI] = None
        if not self.mock:
            self.client = AsyncOpenAI(
                base_url=llm.get("base_url") or None,
                api_key=llm.get("api_key") or "EMPTY",
                timeout=llm.get("timeout_seconds", 60),
                max_retries=0,  # 由本类自控重试
            )
        self.fast_model = llm.get("fast_model") or "fast-model"
        self.deep_model = llm.get("deep_model") or "deep-model"
        self.embedding_model = llm.get("embedding_model") or ""
        self.temperature = float(llm.get("temperature", 0.2))
        self.max_retries = int(llm.get("max_retries", 3))
        self.cost_log = bool(llm.get("cost_log", True))
        self.total_cost = 0.0      # 累计成本(估算)
        self.call_count = 0
        self.fail_count = 0

    # ---------------------------------------------------------------
    def is_mock(self) -> bool:
        return self.mock

    def force_mock(self, flag: bool = True):
        """临时强制模拟模式(测试用, 避免真实调用污染数据/消耗token)。"""
        self.mock = flag

    def get_model(self, task: str = "fast") -> str:
        """
        按任务路由模型: deep 任务用深模型, 其余用快模型。
        容错: 深模型未配置时自动回退快模型(避免请求不存在的模型名)。
        """
        deep_tasks = set(self.settings.section("model_routes").get("deep_model_tasks", []))
        if task in deep_tasks and self.deep_model and self.deep_model not in ("deep-model", ""):
            return self.deep_model
        return self.fast_model

    # ---------------------------------------------------------------
    async def complete(
        self,
        messages: List[Dict[str, str]],
        task: str = "fast",
        schema: Optional[Type[BaseModel]] = None,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        发起一次聊天补全并返回结构化 JSON dict。
        - schema: Pydantic 模型, 输出会按其字段校验;
        - 返回 dict 已通过 schema 校验(字段合法)。
        """
        if self.mock:
            return self._mock_complete(messages, schema)
        if self.client is None:
            raise LLMError("LLM 未配置且非模拟模式")

        model = self.get_model(task)
        prompt_json = json.dumps(self._schema_hint(schema), ensure_ascii=False)
        # 告知模型必须输出 JSON (OpenAI json_object 模式要求提示中出现 json 字样)
        sys_msg = {
            "role": "system",
            "content": (
                "你是一个严谨的量化交易系统智能体。请只输出一个 JSON 对象, "
                f"不要输出任何其他文字。输出必须匹配以下 JSON Schema:\n{prompt_json}\n"
                "所有数值必须为有限数字, 字符串不能为空。"
            )
        }
        msgs = [sys_msg] + list(messages)

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=model,
                    messages=msgs,
                    temperature=temperature if temperature is not None else self.temperature,
                    response_format={"type": "json_object"},
                )
                self.call_count += 1
                self.fail_count = 0
                content = resp.choices[0].message.content or "{}"
                data = self._parse_and_validate(content, schema)
                self._log_cost(resp, model, data)
                return data
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.fail_count += 1
                if isinstance(e, (json.JSONDecodeError, ValueError)):
                    # 输出不合法: 把校验错误反馈给模型重试
                    msgs = msgs + [{
                        "role": "user",
                        "content": f"你的输出不符合要求: {e}\n请重新输出严格的 JSON。",
                    }]
                logger.warning("LLM 调用失败(第%d次): %s", attempt + 1, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)

        # 连续失败告警交给 notification 层(通过 audit 事件)
        from memory.audit_log import AuditLogger
        AuditLogger.instance().log("llm_failure", "llm", {
            "model": model, "task": task, "error": str(last_err),
            "consecutive": self.fail_count,
        })
        raise LLMError(f"LLM 调用失败: {last_err}")

    # ---------------------------------------------------------------
    def _parse_and_validate(self, content: str, schema: Optional[Type[BaseModel]]) -> Dict[str, Any]:
        # 容错: 去掉可能的 ```json 包裹
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        data = json.loads(content)
        if schema is not None:
            model = schema.model_validate(data)   # 字段级校验
            return model.model_dump()
        if not isinstance(data, dict):
            raise ValueError("LLM 输出不是 JSON 对象")
        return data

    @staticmethod
    def _schema_hint(schema: Optional[Type[BaseModel]]) -> Dict[str, Any]:
        """生成 JSON Schema 提示(供模型参考字段结构)。"""
        if schema is None:
            return {"type": "object"}
        return schema.model_json_schema()

    def _log_cost(self, resp, model: str, data: Dict[str, Any]):
        if not self.cost_log:
            return
        try:
            usage = resp.usage
            if usage is not None:
                in_t = usage.prompt_tokens or 0
                out_t = usage.completion_tokens or 0
                # 粗略估算成本: 深模型按 $2/M in, $8/M out; 快模型 1/10
                rate = 0.8 if model == self.deep_model else 0.08
                cost = (in_t * rate + out_t * 4 * rate) / 1_000_000
                self.total_cost += cost
                logger.debug("LLM 调用 model=%s in=%d out=%d cost≈$%.4f",
                             model, in_t, out_t, cost)
        except Exception:
            pass

    # ---------------------------------------------------------------
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """批量文本向量化。未配置 embedding 模型时用哈希词向量兜底。"""
        if self.mock or not self.embedding_model:
            return [self._hash_embed(t) for t in texts]
        try:
            resp = await self.client.embeddings.create(model=self.embedding_model, input=texts)
            return [d.embedding for d in resp.data]
        except Exception as e:
            logger.warning("embedding 接口失败, 降级为哈希向量: %s", e)
            return [self._hash_embed(t) for t in texts]

    @staticmethod
    def _hash_embed(text: str, dim: int = 1024) -> List[float]:
        """确定性中文分词哈希向量(无模型时的 RAG 兜底)。"""
        vec = np.zeros(dim, dtype=np.float32)
        tokens = list(jieba.cut(text))[:512]
        for tok in tokens:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:8], 16)
            vec[h % dim] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec.tolist()

    # ---------------------------------------------------------------
    def _mock_complete(self, messages, schema) -> Dict[str, Any]:
        """
        规则模拟输出: 不调用任何真实 LLM。
        - 解析用户消息中的上下文(通常含 JSON), 提取关键数字;
        - 按 schema 字段生成合理默认值。
        这样未配置 API Key 时全链路(Agent→风控→下单→回测)依然可运行。
        """
        self.call_count += 1
        hint = self._mock_hint_from_messages(messages)
        if schema is None:
            return {"view": hint.get("view", "neutral"),
                    "score": hint.get("score", 50.0),
                    "confidence": hint.get("confidence", 0.5)}
        # 按 schema 字段逐个生成默认值, 并尽量用 hint 覆盖
        return self._fill_schema(schema, hint)

    def _mock_hint_from_messages(self, messages) -> Dict[str, Any]:
        """从提示词里提取模拟判断依据(把特征数字变成中性偏多的结论)。"""
        text = " ".join(m.get("content", "") for m in messages if isinstance(m, dict))
        hint: Dict[str, Any] = {}
        # 寻找内嵌 JSON(特征摘要), 取若干数字字段
        try:
            idx = text.find("{")
            while idx != -1:
                # 尝试解析候选 JSON 片段
                for end in range(idx + 1, min(len(text), idx + 4000)):
                    if text[end] == "}":
                        try:
                            obj = json.loads(text[idx:end + 1])
                            if isinstance(obj, dict):
                                hint.update(obj)
                                break
                        except Exception:
                            pass
                idx = text.find("{", idx + 1)
        except Exception:
            pass
        return hint

    def _fill_schema(self, schema: Type[BaseModel], hint: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            default = self._field_default(field, name, hint)
            out[name] = default
        try:
            return schema.model_validate(out).model_dump()
        except Exception as e:
            logger.debug("模拟输出校验失败, 回退最小默认: %s", e)
            out2 = {n: (hint.get(n) if n in hint else None) for n in schema.model_fields}
            return out2

    def _field_default(self, field, name: str, hint: Dict[str, Any]) -> Any:
        import pydantic_core
        if name in hint and hint[name] is not None:
            return hint[name]
        ann = field.annotation
        if ann is float or "float" in str(ann):
            return 0.5
        if ann is int or "int" in str(ann):
            return 0
        if ann is bool or "bool" in str(ann):
            return False
        if ann is list or "list" in str(ann) or "List" in str(ann):
            return []
        if "Literal" in str(ann):
            # 枚举类型取第一个合法值
            return field.default
        if ann is str or "str" in str(ann):
            return "模拟输出: 无LLM配置, 依据特征中性判断"
        return field.default

    # ---------------------------------------------------------------
    def mock_allowed(self) -> bool:
        return True


_llm_instance: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """全局 LLM 客户端单例。"""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMClient()
    return _llm_instance


async def quick_test():
    """快速自检: 真实/模拟 模式各调一次。"""
    llm = get_llm()
    print("mock mode:", llm.is_mock(), "models:", llm.fast_model, "/", llm.deep_model)
    data = await llm.complete(
        [{"role": "user", "content": "技术指标: ma5=10.2 ma20=9.8 rsi=58, 请输出 JSON 判断"}],
        task="fast",
    )
    print("output:", json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(quick_test())
