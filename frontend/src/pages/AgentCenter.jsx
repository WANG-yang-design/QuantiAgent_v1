import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Zap, ChevronDown, ChevronRight } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, Empty, Spin } from "../components/Common";
import DecisionTimeline from "../components/DecisionTimeline";

/** Agent 决策中心: 最近工作流列表 + 决策链路可视化 */
export default function AgentCenter() {
  const qc = useQueryClient();
  const [traceId, setTraceId] = useState(null);
  const [code, setCode] = useState("");

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

  const scan = useMutation({
    mutationFn: async () => {
      setCode("");
      const { task_id } = await api.post(`/api/scan/${code.trim().toUpperCase()}`);
      setScanTask(task_id);
      return task_id;
    },
  });

  // 异步分析任务轮询(完整分析需1-3分钟)
  const [scanTask, setScanTask] = useState(null);
  const { data: scanStatus } = useQuery({
    queryKey: ["scantask", scanTask],
    queryFn: () => api.get(`/api/scan/status/${scanTask}`),
    enabled: !!scanTask,
    refetchInterval: 3000,
  });
  useEffect(() => {
    if (scanStatus?.status === "DONE") {
      qc.invalidateQueries({ queryKey: ["traces"] });
      const t = scanStatus.trace_id;
      if (t) setTraceId(t);   // 分析完成自动选中该决策链路
      setTimeout(() => setScanTask(null), 2000);
    }
    if (scanStatus?.status === "FAILED") setTimeout(() => setScanTask(null), 3000);
  }, [scanStatus]);

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">Agent 决策中心</h1>
        <SystemBar />
      </div>

      {/* 触发分析 */}
      <div className="card flex items-center gap-3">
        <span className="text-sm text-gray-600">手动触发一次完整投研分析:</span>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); scan.mutate(); }}>
          <input className="input w-32" placeholder="6位代码" value={code} onChange={(e) => setCode(e.target.value)} />
          <button type="submit" className="btn-primary" disabled={scan.isPending || !!scanTask}>
            <Zap size={14} className="inline mr-1" />
            {scanTask ? "分析中..." : "开始分析"}
          </button>
        </form>
        {scanStatus && scanStatus.status === "RUNNING" && (
          <span className="badge bg-amber-50 text-amber-700 animate-pulse">正在分析 {scanStatus.symbol} ...(约1-3分钟)</span>
        )}
        {scanStatus && scanStatus.status === "FAILED" && (
          <span className="badge bg-red-50 text-red-600">分析失败: {scanStatus.error}</span>
        )}
        <span className="ml-auto text-xs text-gray-400">分析会依次调用: 数据闸门→7分析师→多空辩论→首席→交易员→风控→合规→执行</span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* 左: 工作流列表 */}
        <div className="card">
          <div className="card-title">最近工作流 ({(traces || []).length})</div>
          <div className="space-y-1.5 max-h-[600px] overflow-y-auto">
            {traces?.length ? traces.map((t) => {
              const sel = t.trace_id === traceId;
              return (
                <button key={t.trace_id} className={`w-full text-left px-3 py-2 rounded-lg border ${sel ? "border-brand-600 bg-brand-50" : "border-gray-100 hover:bg-gray-50"}`}
                  onClick={() => setTraceId(t.trace_id)}>
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-sm">{t.symbol || "全市场"}</span>
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
                  {sel ? <ChevronDown size={14} className="absolute" /> : <ChevronRight size={14} className="absolute" />}
                </button>
              );
            }) : <Empty text="暂无工作流记录(先执行 scan)" />}
          </div>
        </div>

        {/* 右: 决策链路 */}
        <div className="card lg:col-span-2">
          <div className="card-title">决策链路 {traceId ? `· ${traceId.slice(0, 20)}...` : ""}</div>
          {traceId ? (
            trace ? (
              <div className="max-h-[600px] overflow-y-auto pr-1">
                <DecisionTimeline trace={trace} />
              </div>
            ) : <Spin />
          ) : <Empty text="选择左侧工作流查看完整 Agent 决策链路" />}
        </div>
      </div>
    </div>
  );
}
