import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Zap, ChevronDown, ChevronRight, Target, Download } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, Empty, Spin } from "../components/Common";
import DecisionTimeline from "../components/DecisionTimeline";
import { useScanStore } from "../store/scanStore";

/** 下载 JSON 文件(导出用) */
function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}
function downloadCsv(filename, rows) {
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const csv = rows.map((r) => r.map(esc).join(",")).join("\n");
  const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** Agent 决策中心: 最近工作流列表 + 决策链路可视化 + 准确率归因 */
export default function AgentCenter() {
  const qc = useQueryClient();
  const [traceId, setTraceId] = useState(null);
  const [code, setCode] = useState("");
  const [tab, setTab] = useState("traces");   // traces / accuracy
  const { taskId, symbol: scanSymbol, status: scanStatus, error: scanError, setTask, update } = useScanStore();

  const { data: traces } = useQuery({
    queryKey: ["traces"],
    queryFn: () => api.get("/api/workflow/traces?limit=20"),
    refetchInterval: 10000,
  });
  const { data: trace } = useQuery({
    queryKey: ["trace", traceId],
    queryFn: () => api.get(`/api/workflow/trace/${traceId}`),
    enabled: !!traceId,
  });
  const { data: accuracy } = useQuery({
    queryKey: ["accuracy"],
    queryFn: () => api.get("/api/agents/accuracy?days=90&horizon_days=5"),
    enabled: tab === "accuracy",
    refetchInterval: 60000,
  });

  // 全局扫描任务状态轮询(跨页面恢复, 状态存 zustand+sessionStorage)
  const { data: taskStatus } = useQuery({
    queryKey: ["scantask", taskId],
    queryFn: () => api.get(`/api/scan/status/${taskId}`),
    enabled: !!taskId,
    refetchInterval: 3000,
  });
  useEffect(() => {
    if (!taskStatus) return;
    if (taskStatus.status === "DONE") {
      update({ status: "DONE" });
      qc.invalidateQueries({ queryKey: ["traces"] });
      if (taskStatus.trace_id) setTraceId(taskStatus.trace_id);
    } else if (taskStatus.status === "FAILED") {
      update({ status: "FAILED", error: taskStatus.error });
    } else {
      update({ status: "RUNNING" });
    }
  }, [taskStatus]);

  const scan = useMutation({
    mutationFn: async () => {
      const sym = code.trim().toUpperCase();
      setCode("");
      const { task_id } = await api.post(`/api/scan/${sym}`);
      setTask(task_id, sym);
      return task_id;
    },
  });

  const scanning = taskId && scanStatus === "RUNNING";

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">Agent 决策中心</h1>
        <SystemBar />
      </div>

      {/* 触发分析(全局状态, 切页不丢) */}
      <div className="card flex items-center gap-3">
        <span className="text-sm text-gray-600">手动触发一次完整投研分析:</span>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); scan.mutate(); }}>
          <input className="input w-32" placeholder="6位代码" value={code} onChange={(e) => setCode(e.target.value)} />
          <button type="submit" className="btn-primary" disabled={scan.isPending || scanning}>
            <Zap size={14} className="inline mr-1" />
            {scanning ? "分析中..." : "开始分析"}
          </button>
        </form>
        {scanning && (
          <span className="badge bg-amber-50 text-amber-700 animate-pulse">
            正在分析 {scanSymbol} ...(约1-3分钟, 切换页面不会中断)
          </span>
        )}
        {taskId && scanStatus === "DONE" && (
          <span className="badge bg-green-50 text-green-600">分析完成, 已展示最新决策链路</span>
        )}
        {taskId && scanStatus === "FAILED" && (
          <span className="badge bg-red-50 text-red-600">分析失败: {scanError}</span>
        )}
        <span className="ml-auto text-xs text-gray-400">分析会依次调用: 数据闸门→7分析师→多空辩论→首席→交易员→风控→合规→执行</span>
      </div>

      {/* tab 切换 */}
      <div className="flex gap-2">
        <button className={`btn ${tab === "traces" ? "btn-primary" : "btn-ghost"}`} onClick={() => setTab("traces")}>决策链路</button>
        <button className={`btn ${tab === "accuracy" ? "btn-primary" : "btn-ghost"}`} onClick={() => setTab("accuracy")}>
          <Target size={13} className="inline mr-1" />Agent准确率归因
        </button>
      </div>

      {tab === "traces" ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="card">
            <div className="card-title">最近工作流 ({(traces || []).length})</div>
            <div className="space-y-1.5 max-h-[600px] overflow-y-auto">
              {traces?.length ? traces.map((t) => {
                const sel = t.trace_id === traceId;
                return (
                  <button key={t.trace_id} className={`w-full text-left px-3 py-2 rounded-lg border ${sel ? "border-brand-600 bg-brand-50" : "border-gray-100 hover:bg-gray-50"}`}
                    onClick={() => setTraceId(t.trace_id)}>
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-sm">
                      {t.symbol || "全市场"}
                      {t.name && <span className="text-gray-400 font-normal"> · {t.name}</span>}
                    </span>
                      {t.latest_status === "RUNNING" ? (
                        <span className="badge bg-amber-50 text-amber-700 animate-pulse">运行中</span>
                      ) : t.failed ? (
                        <span className="badge bg-red-50 text-red-600">有失败</span>
                      ) : (
                        <span className="badge bg-green-50 text-green-600">{t.latest_status}</span>
                      )}
                    </div>
                    <div className="text-xs text-gray-500 mt-1">
                      {t.start} · {t.runs.length}个节点
                      {t.chief && <> · <b className={t.chief.decision === "BUY_CANDIDATE" ? "text-up" : "text-gray-700"}>{t.chief.decision}</b></>}
                    </div>
                    {sel ? <ChevronDown size={14} className="ml-auto shrink-0 text-brand-600" /> : <ChevronRight size={14} className="ml-auto shrink-0 text-gray-300" />}
                  </button>
                );
              }) : <Empty text="暂无工作流记录(先执行 scan)" />}
            </div>
          </div>

        <div className="card lg:col-span-2">
          <div className="card-title flex items-center justify-between">
            <span>决策链路 {traceId ? `· ${traceId.slice(0, 20)}...` : ""}</span>
            {trace && (
              <button className="btn-ghost text-xs" onClick={() => downloadJson(
                `agent_trace_${trace.trace_id.slice(0, 16)}.json`, trace)}>
                <Download size={13} className="inline mr-1" />导出链路JSON
              </button>
            )}
          </div>
            {traceId ? (
              trace ? (
                <div className="max-h-[600px] overflow-y-auto pr-1">
                  <DecisionTimeline trace={trace} />
                </div>
              ) : <Spin />
            ) : <Empty text="选择左侧工作流查看完整 Agent 决策链路" />}
          </div>
        </div>
      ) : (
        <div className="card">
          <div className="card-title">Agent 准确率归因(结论后 {accuracy?.horizon_days} 个交易日收益校验 · 近 {accuracy?.window_days} 天)</div>
          {accuracy?.agents?.length ? (
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Agent</th><th className="th">有效结论数</th><th className="th">命中</th>
                  <th className="th">准确率</th><th className="th">平均置信度</th><th className="th">中性/无数据</th>
                </tr>
              </thead>
              <tbody>
                {accuracy.agents.map((a) => (
                  <tr key={a.agent}>
                    <td className="td font-medium">{a.agent}</td>
                    <td className="td">{a.count}</td>
                    <td className="td">{a.hit}</td>
                    <td className={`td font-semibold ${a.accuracy >= 0.6 ? "text-up" : a.accuracy < 0.4 ? "text-down" : ""}`}>
                      {(a.accuracy * 100).toFixed(0)}%
                    </td>
                    <td className="td">{(a.avg_confidence * 100).toFixed(0)}%</td>
                    <td className="td text-gray-500">{a.neutral}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty text="暂无足够样本(需要结论后5个交易日的数据)" />}
          <div className="text-xs text-gray-400 mt-2">
            归因口径: 首席BUY_CANDIDATE后5日上涨/SELL_CANDIDATE后5日下跌为命中; 分析师bullish后5日上涨/bearish后5日下跌为命中。样本不足时准确率参考意义有限。
          </div>
        </div>
      )}
    </div>
  );
}

