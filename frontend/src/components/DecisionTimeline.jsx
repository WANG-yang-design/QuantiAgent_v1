import { useState } from "react";
import { api } from "../api/client";

/** 首席研究员结论 → 中文+配色(修复: 决策结论不明显, 只有英文缩写) */
const DEC_META = {
  BUY_CANDIDATE: { label: "买入候选", cls: "bg-red-50 text-red-600 border-red-200" },
  SELL_CANDIDATE: { label: "卖出候选", cls: "bg-green-50 text-green-600 border-green-200" },
  HOLD: { label: "持有观望", cls: "bg-gray-100 text-gray-600 border-gray-200" },
  EXCLUDE: { label: "排除", cls: "bg-gray-100 text-gray-400 border-gray-200" },
};

/**
 * 决策链路时间线: 展示一次工作流 trace 的完整 Agent 节点链
 * nodes: [{agent, status, cost, model, output, error}]
 */
const AGENT_META = {
  data_admin: { label: "数据管理员", color: "bg-gray-100 text-gray-700" },
  technical_analyst: { label: "技术分析师", color: "bg-blue-50 text-blue-700" },
  etf_analyst: { label: "ETF分析师", color: "bg-cyan-50 text-cyan-700" },
  fundamental_analyst: { label: "基本面分析师", color: "bg-indigo-50 text-indigo-700" },
  news_analyst: { label: "新闻公告分析师", color: "bg-amber-50 text-amber-700" },
  sentiment_analyst: { label: "情绪分析师", color: "bg-pink-50 text-pink-700" },
  money_flow_analyst: { label: "资金流分析师", color: "bg-teal-50 text-teal-700" },
  macro_analyst: { label: "宏观分析师", color: "bg-violet-50 text-violet-700" },
  bull_researcher: { label: "看多研究员", color: "bg-red-50 text-red-700" },
  bear_researcher: { label: "看空研究员", color: "bg-green-50 text-green-700" },
  chief_researcher: { label: "首席研究员", color: "bg-purple-50 text-purple-700 font-semibold" },
  trader: { label: "交易员", color: "bg-orange-50 text-orange-700 font-semibold" },
  risk_manager: { label: "风险管理", color: "bg-red-50 text-red-700 font-semibold" },
  compliance: { label: "合规审计", color: "bg-yellow-50 text-yellow-700" },
  execution_supervisor: { label: "执行监督", color: "bg-emerald-50 text-emerald-700" },
  review: { label: "复盘总结", color: "bg-slate-50 text-slate-700" },
};

function summarize(node) {
  const o = node.output || {};
  // 标的中文名(修复: 决策链路只显示代码, 股票名常为空)
  const nm = node.symbol_name || o?.name || "";
  const sym = node.symbol || o?.symbol || "";
  const label = nm ? `${sym} ${nm}` : sym;
  if (node.agent === "chief_researcher")
    return `结论: ${o.research_decision} · 置信 ${(o.confidence * 100)?.toFixed(0)}% · 评分 ${o.score?.toFixed(0)}`;
  if (node.agent === "risk_manager")
    return `风控: ${o.risk_decision} · 等级 ${o.risk_level}${o.blocked_reason ? " · " + o.blocked_reason : ""}`;
  if (node.agent === "trader")
    return `${o.action} ${label} ${o.estimated_quantity}份 @ ${o.limit_price ?? "市价"} · 金额 ${o.order_amount?.toFixed(0)}`;
  if (node.agent === "compliance")
    return `合规: ${o.compliance_status}${o.reason ? " · " + o.reason : ""}`;
  if (node.agent === "data_admin")
    return `数据: ${o.data_status}${o.blocked_reason ? " · " + o.blocked_reason : ""}`;
  if (o.agent && o.view)
    return `${o.agent} · ${o.view} · 评分 ${o.score?.toFixed(0)} · 置信 ${(o.confidence * 100)?.toFixed(0)}%`;
  const s = JSON.stringify(o).slice(0, 80);
  return s || "-";
}

function statusBadge(status) {
  if (status === "FAILED") return <span className="badge bg-red-50 text-red-600">失败</span>;
  if (status === "RUNNING") return <span className="badge bg-amber-50 text-amber-700 animate-pulse">运行中</span>;
  return <span className="badge bg-green-50 text-green-600">成功</span>;
}

export default function DecisionTimeline({ trace }) {
  const [expanded, setExpanded] = useState(null);
  if (!trace?.nodes?.length) return <div className="text-gray-400 text-sm py-6">无节点数据</div>;

  return (
    <div className="space-y-2">
      {trace.nodes.map((n, i) => {
        const meta = AGENT_META[n.agent] || { label: n.agent, color: "bg-gray-100" };
        const isRisk = n.agent === "risk_manager" && n.output?.risk_decision === "REJECT";
        const exp = expanded === i;
        return (
          <div
            key={i}
            className={`border rounded-lg overflow-hidden ${isRisk ? "border-red-200 bg-red-50/40" : "border-gray-100 bg-white"}`}
          >
            <button
              className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-gray-50"
              onClick={() => setExpanded(exp ? null : i)}
            >
              <span className={`badge ${meta.color}`}>{meta.label}</span>
              {n.agent === "chief_researcher" && n.output?.research_decision && (
                <span className={`badge border shrink-0 ${(DEC_META[n.output.research_decision] || { cls: "bg-gray-100 text-gray-600 border-gray-200" }).cls}`}>
                  {(DEC_META[n.output.research_decision] || { label: n.output.research_decision }).label}
                </span>
              )}
              <span className="flex-1 text-xs text-gray-600 truncate">{summarize(n)}</span>
              {statusBadge(n.status)}
              <span className="text-[10px] text-gray-400">
                {n.cost != null ? `${n.cost}s` : ""}
              </span>
            </button>
            {exp && (
              <pre className="px-3 pb-3 text-[11px] text-gray-600 bg-gray-50 overflow-x-auto whitespace-pre-wrap">
                {JSON.stringify(n.output, null, 2)}
              </pre>
            )}
          </div>
        );
      })}
      {trace.events?.length > 0 && (
        <div className="border border-gray-100 rounded-lg p-3 bg-gray-50">
          <div className="text-xs font-semibold text-gray-500 mb-2">审计事件流</div>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {trace.events.map((e, i) => (
              <div key={i} className="flex gap-2 text-[11px]">
                <span className="text-gray-400 shrink-0">{e.time}</span>
                <span className="badge bg-white border border-gray-200 text-gray-600">{e.event_type}</span>
                <span className="text-gray-500 truncate">{e.actor}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
