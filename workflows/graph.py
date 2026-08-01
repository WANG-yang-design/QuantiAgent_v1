# -*- coding: utf-8 -*-
"""
工作流状态与 DAG 图运行器
=========================
实现 LangGraph 同思路的固定 DAG 编排:
- 节点: 有向无环图, 每个节点是一个 async 函数(state) → dict(增量状态)
- 边: 顺序执行 + 条件分支(如: 数据闸门 BLOCKED → 中止)
- 禁止自由群聊式决策: 交易系统采用固定 DAG
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.ids import gen_trace_id
from core.logging import set_trace_id, get_logger

logger = logging.getLogger("workflow.graph")


class WorkflowState:
    """工作流共享状态(类字典, 自动追踪来源)。"""

    def __init__(self, symbol: str = "", trace_id: Optional[str] = None):
        self.symbol = symbol
        self.trace_id = trace_id or gen_trace_id()
        self.data: Dict[str, Any] = {}
        self.node_logs: List[Dict[str, Any]] = []
        self.interrupted: Optional[str] = None     # 中断原因(如数据BLOCKED)

    def set(self, key: str, value: Any):
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def record(self, node: str, summary: str, payload: Dict[str, Any] | None = None):
        self.node_logs.append({"node": node, "summary": summary,
                               "payload": payload or {}})

    def interrupt(self, reason: str):
        self.interrupted = reason

    def is_interrupted(self) -> bool:
        return self.interrupted is not None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trace_id": self.trace_id,
            "interrupted": self.interrupted,
            "data_keys": list(self.data.keys()),
            "node_logs": self.node_logs,
        }


NodeFn = Callable[[WorkflowState], Awaitable[Dict[str, Any]]]


class WorkflowGraph:
    """固定 DAG: nodes[name]=async函数, edges=[(from, to)] 顺序执行。"""

    def __init__(self, name: str):
        self.name = name
        self.nodes: Dict[str, NodeFn] = {}
        self.edges: List[tuple] = []
        self.conditions: Dict[str, Callable[[WorkflowState], bool]] = {}

    def add_node(self, name: str, fn: NodeFn):
        self.nodes[name] = fn

    def add_edge(self, src: str, dst: str):
        self.edges.append((src, dst))

    def add_condition(self, node: str, cond: Callable[[WorkflowState], bool]):
        """条件: cond(state)==True 才执行该节点。"""
        self.conditions[node] = cond

    # ------------------------------------------------------------------
    async def run(self, state: WorkflowState, stop_at: Optional[str] = None) -> WorkflowState:
        set_trace_id(state.trace_id)
        logger.info("[%s] 工作流 %s 启动: symbol=%s", state.trace_id, self.name, state.symbol)
        # 拓扑排序执行(简单 Kahn)
        order = self._topo_sort()
        for node in order:
            if state.is_interrupted():
                logger.info("[%s] 工作流被中断: %s", state.trace_id, state.interrupted)
                break
            cond = self.conditions.get(node)
            if cond is not None and not cond(state):
                continue
            if stop_at and node == stop_at:
                logger.info("[%s] 工作流停在 %s", state.trace_id, node)
                break
            fn = self.nodes.get(node)
            if fn is None:
                continue
            try:
                t0 = asyncio.get_event_loop().time()
                result = await fn(state)
                cost = asyncio.get_event_loop().time() - t0
                if isinstance(result, dict):
                    for k, v in result.items():
                        state.set(k, v)
                logger.info("[%s] 节点 %s 完成 (%.2fs)", state.trace_id, node, cost)
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] 节点 %s 异常: %s", state.trace_id, node, exc,
                             exc_info=True)
                state.interrupt(f"节点 {node} 异常: {exc}")
                break
        logger.info("[%s] 工作流 %s 结束", state.trace_id, self.name)
        return state

    def _topo_sort(self) -> List[str]:
        from collections import defaultdict, deque
        indeg = {n: 0 for n in self.nodes}
        adj = defaultdict(list)
        for src, dst in self.edges:
            if src in indeg and dst in indeg:
                adj[src].append(dst)
                indeg[dst] += 1
        q = deque([n for n, d in indeg.items() if d == 0])
        order = []
        while q:
            n = q.popleft()
            order.append(n)
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    q.append(m)
        if len(order) != len(self.nodes):
            logger.warning("工作流 %s 存在环, 按注册顺序执行", self.name)
            order = list(self.nodes.keys())
        return order

    # ------------------------------------------------------------------
    def render(self) -> str:
        """输出 DAG 结构(用于文档/审计)。"""
        lines = [f"工作流: {self.name}"]
        for n in self.nodes:
            lines.append(f"  - {n}")
        lines.append("  边:")
        for s, d in self.edges:
            lines.append(f"    {s} → {d}")
        return "\n".join(lines)
