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
import re
import threading
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
        # 修复: 原实现用单例 AsyncOpenAI 客户端跨事件循环复用 —— 调度器/Web
        # 各自线程 asyncio.run() 每次新建并关闭 loop, httpx 连接池绑定首个
        # 使用它的 loop, 后续请求抛 "Event loop is closed", 所有 Agent 的
        # LLM 调用静默失败, 交易链路停止。改为按事件循环惰性创建/复用。
        self._clients: Dict[int, Any] = {}   # id(loop) -> (loop, AsyncOpenAI)
        self._client_lock = threading.Lock()
        self.fast_model = llm.get("fast_model") or "fast-model"
        self.deep_model = llm.get("deep_model") or "deep-model"
        self.embedding_model = llm.get("embedding_model") or ""
        self.temperature = float(llm.get("temperature", 0.2))
        self.max_retries = int(llm.get("max_retries", 3))
        # 修复: 原 2048 上限让模型每调用最多可生成 2000+ 输出token,
        # 模型常在 JSON 外夹带解释文字(被 regex 剥离但已计费),
        # 2500 次调用 → 输出 token 高达 200 万+。收紧到 1024 并配
        # 紧凑系统提示(列表字段已由 Prompt 限幅), 输出成本约降一半。
        self.max_tokens = int(llm.get("max_tokens", 1024) or 1024)
        self.max_concurrent = max(int(llm.get("max_concurrent", 5) or 5), 1)
        self._in_flight = 0            # 在途请求计数(简单并发门控)
        self.cost_log = bool(llm.get("cost_log", True))
        self.total_cost = 0.0      # 累计成本(估算)
        self.call_count = 0
        self.fail_count = 0

    # ---------------------------------------------------------------
    def _client_for(self):
        """获取当前事件循环对应的 AsyncOpenAI 客户端(惰性创建)。"""
        import asyncio
        loop = asyncio.get_running_loop()
        if loop.is_closed():
            raise LLMError("当前事件循环已关闭")
        key = id(loop)
        with self._client_lock:
            entry = self._clients.get(key)
            if entry is not None and entry[0] is loop:
                return entry[1]
            # 清理已关闭 loop 的旧客户端, 防止连接池泄漏
            for k in [k for k, (l, _) in list(self._clients.items()) if l is not loop and l.is_closed()]:
                del self._clients[k]
            llm = self.settings.section("llm")
            client = AsyncOpenAI(
                base_url=llm.get("base_url") or None,
                api_key=llm.get("api_key") or "EMPTY",
                timeout=llm.get("timeout_seconds", 60),
                max_retries=0,  # 由本类自控重试
            )
            self._clients[key] = (loop, client)
            return client

    # ---------------------------------------------------------------
    def is_mock(self) -> bool:
        return self.mock

    def force_mock(self, flag: bool = True):
        """临时强制模拟模式(测试用, 避免真实调用污染数据/消耗token)。"""
        self.mock = flag

    def get_model(self, task: str = "fast") -> str:
        """
        按任务路由模型: deep 任务用深模型, 其余用快模型。
        task 传 Agent 名(如 "trader"/"news_analyst"), 通过 model_routes.agent_task_map
        映射到 fast/deep; 也兼容直接传语义任务名(如 "bull_bear_debate")或字面 fast/deep。
        修复: 原实现拿字面 "fast"/"deep" 与语义任务名集合比对, 深模型路由永远失效。
        容错: 深模型未配置时自动回退快模型(避免请求不存在的模型名)。
        """
        routes = self.settings.section("model_routes") or {}
        agent_map = routes.get("agent_task_map", {}) or {}
        route = str(agent_map.get(task, task))
        deep_tasks = set(routes.get("deep_model_tasks", []))
        if route == "deep" or task in deep_tasks:
            if self.deep_model and self.deep_model not in ("deep-model", ""):
                return self.deep_model
        return self.fast_model

    # ---------------------------------------------------------------
    async def complete(
        self,
        messages: List[Dict[str, str]],
        task: str = "fast",
        schema: Optional[Type[BaseModel]] = None,
        temperature: Optional[float] = None,
        usage_cb=None,
    ) -> Dict[str, Any]:
        """
        发起一次聊天补全并返回结构化 JSON dict。
        - schema: Pydantic 模型, 输出会按其字段校验;
        - usage_cb: 回调(prompt_tokens, completion_tokens), 供落库统计真实用量;
        - 返回 dict 已通过 schema 校验(字段合法)。
        """
        if self.mock:
            return self._mock_complete(messages, schema)
        client = self._client_for()

        model = self.get_model(task)
        prompt_json = json.dumps(self._schema_hint(schema), ensure_ascii=False)
        # 告知模型必须输出 JSON (OpenAI json_object 模式要求提示中出现 json 字样)
        sys_msg = {
            "role": "system",
            "content": (
                "你是一个严谨的量化交易系统智能体。请只输出一个 JSON 对象, "
                "不要输出任何其他文字、解释、思考过程或 markdown 代码块。"
                f"输出必须匹配以下 JSON Schema:\n{prompt_json}\n"
                "所有数值必须为有限数字, 字符串不能为空。"
                "输出必须紧凑: 列表字段按 Schema 中的字段名给出最少必要条数, "
                "每条文字尽量短, 总输出控制在 600 token 以内。"
            )
        }
        msgs = [sys_msg] + list(messages)

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                # 简单并发门控: 超过在途上限时等待(防止 7 分析师并行+池扫描 触发 ~35 路并发)
                while self._in_flight >= self.max_concurrent:
                    await asyncio.sleep(0.2)
                self._in_flight += 1
                try:
                    resp = await client.chat.completions.create(
                        model=model,
                        messages=msgs,
                        temperature=temperature if temperature is not None else self.temperature,
                        max_tokens=self.max_tokens,
                        response_format={"type": "json_object"},
                    )
                finally:
                    self._in_flight -= 1
                self.call_count += 1
                self.fail_count = 0
                content = resp.choices[0].message.content or "{}"
                data = self._parse_and_validate(content, schema)
                # 真实用量上报(供审计/成本统计落库)
                if usage_cb is not None:
                    try:
                        usage = resp.usage
                        if usage is not None:
                            usage_cb(int(usage.prompt_tokens or 0),
                                     int(usage.completion_tokens or 0))
                    except Exception:
                        pass
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
                # 重试分类: 网络/超时/服务端错误才重试; 4xx 客户端错误不重试(白等)
                retriable = self._is_retriable_error(e)
                logger.warning("LLM 调用失败(第%d次): %s", attempt + 1, e)
                if attempt < self.max_retries and retriable:
                    await asyncio.sleep(2 ** attempt + (time.time() % 0.5))   # 退避+jitter

        # 连续失败告警交给 notification 层(通过 audit 事件)
        from memory.audit_log import AuditLogger
        AuditLogger.instance().log("llm_failure", "llm", {
            "model": model, "task": task, "error": str(last_err),
            "consecutive": self.fail_count,
        })
        raise LLMError(f"LLM 调用失败: {last_err}")

    # ---------------------------------------------------------------
    @staticmethod
    def _is_retriable_error(e: Exception) -> bool:
        """是否值得重试: 网络/超时/服务端 5xx 重试; 4xx 客户端错误不重试。"""
        name = type(e).__name__.lower()
        if isinstance(e, (json.JSONDecodeError, ValueError)):
            return True   # 输出格式问题: 反馈后让模型重出
        if any(k in name for k in ("apiconnection", "apitimeout", "timeout",
                                   "apiconnectionerror", "internal")):
            return True
        code = getattr(e, "status_code", None) or getattr(e, "code", None)
        try:
            code = int(code or 0)
        except (TypeError, ValueError):
            code = 0
        return code >= 500 or code == 429 or code == 0

    # ---------------------------------------------------------------
    @staticmethod
    def _repair_json(content: str) -> str:
        """修复模型输出的常见截断(修复: "Unterminated string" 一类失败):
        1. 若内容截断在未闭合字符串内 → 从该字符串起点起补成 ""(结构合法);
        2. 括号未闭合 → 按开闭顺序补全右括号([→], {→})。"""
        s = content.rstrip()
        # 1. 字符串状态跟踪: 若扫描结束仍在字符串内, 说明被截断
        in_str = False
        esc = False
        last_open = -1
        for i, ch in enumerate(s):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                if in_str:
                    in_str = False
                else:
                    in_str = True
                    last_open = i
        if in_str and last_open >= 0:
            s = s[:last_open] + '""'
        # 2. 括号补齐(跳过字符串内的括号)
        stack = []
        in_str = False
        esc = False
        for ch in s:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "[{":
                stack.append("]" if ch == "[" else "}")
            elif ch in "]}":
                if stack:
                    stack.pop()
        if stack:
            s += "".join(reversed(stack))
        return s

    def _parse_and_validate(self, content: str, schema: Optional[Type[BaseModel]]) -> Dict[str, Any]:
        # 容错: 去掉可能的 ```json 包裹
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
                content = content.strip()
        # 容错: 提取首个 { ... } 块(容忍模型输出前后缀文字)
        if not content.startswith("{"):
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                content = m.group(0)
        # 容错: 修复尾逗号(对象与数组)
        content = re.sub(r",\s*([}\]])", r"\1", content)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # 修复: 输出被 max_tokens 截断(Unterminated string / 括号未闭合)时
            # 先尝试自动修复再判失败, 显著降低"LLM 调用失败"节点数
            repaired = self._repair_json(content)
            data = json.loads(repaired)
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
            client = self._client_for()
            resp = await client.embeddings.create(model=self.embedding_model, input=texts)
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
