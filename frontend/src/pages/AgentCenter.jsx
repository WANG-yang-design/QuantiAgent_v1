import { useState, useEffect, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Zap, ChevronDown, ChevronRight, Target, Download } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, Empty, Spin } from "../components/Common";
import DecisionTimeline from "../components/DecisionTimeline";
import { useScanStore } from "../store/scanStore";

/** 首席研究员结论 → 中文+配色(修复: 原只显示英文 BUY_CANDIDATE, 不直观) */
export const DEC_META = {
  BUY_CANDIDATE: { label: "买入候选", cls: "bg-red-50 text-red-600 border-red-200", dot: "#e03131" },
  SELL_CANDIDATE: { label: "卖出候选", cls: "bg-green-50 text-green-600 border-green-200", dot: "#2f9e44" },
  HOLD: { label: "持有观望", cls: "bg-gray-100 text-gray-600 border-gray-200", dot: "#868e96" },
  EXCLUDE: { label: "排除", cls: "bg-gray-100 text-gray-400 border-gray-200", dot: "#adb5bd" },
};
export function decisionMeta(d) {
  return DEC_META[d] || { label: d || "无结论", cls: "bg-gray-100 text-gray-500 border-gray-200", dot: "#868e96" };
}

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
/** 秒数 → 友好耗时(1分23秒) */
function fmtDuration(sec) {
  if (sec == null) return "-";
  if (sec < 60) return `${sec.toFixed(0)}秒`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}分${s}秒`;
}

/** 止损止盈执行方案卡(修复: 买入结论只给方向, 不给怎么止损止盈)。
 * 依据 config/risk_limits.yaml 的风控参数计算:
 *   止损: 成本×(1-stop_loss_pct); 移动止盈: 从持仓最高价回撤 trailing_stop_pct;
 *   仓位: target_weight / max_total_position。 */
function RiskPlanCard({ trace, limits }) {
  if (!trace?.nodes?.length) return null;
  const chief = trace.nodes.find((n) => n.agent === "chief_researcher")?.output;
  const trader = trace.nodes.find((n) => n.agent === "trader")?.output;
  const decision = chief?.research_decision;
  const action = trader?.action;
  if (!chief && !trader) return null;
  if (decision !== "BUY_CANDIDATE" && decision !== "SELL_CANDIDATE" && action !== "BUY" && action !== "SELL") return null;

  const acct = limits?.account_level || {};
  const pm = limits?.position_monitor || {};
  const stopPct = pm.stop_loss_pct ?? 0.08;
  const trailPct = pm.trailing_stop_pct ?? 0.08;
  const maxPos = acct.max_total_position ?? 0.9;
  const targetW = trader?.target_weight ?? acct.max_single_position ?? 0.2;
  const entryPrice = trader?.limit_price ?? null;
  const qty = trader?.estimated_quantity ?? null;
  const isBuy = decision === "BUY_CANDIDATE" || action === "BUY";

  const fmt3 = (v) => (v == null ? "-" : Number(v).toFixed(3));
  const stopLine = entryPrice ? (entryPrice * (1 - stopPct)).toFixed(3) : null;
  return (
    <div className={`card mt-3 ${isBuy ? "border-red-200" : "border-green-200"}`}>
      <div className="card-title">{isBuy ? "买入后的风控执行方案" : "卖出/减仓提示"}</div>
      {isBuy ? (
        <div className="text-xs text-gray-600 space-y-1">
          <div className="flex flex-wrap gap-4">
            <span>建仓参考价: <b>{fmt3(entryPrice)}</b>{qty ? ` × ${qty}份` : ""}</span>
            <span>单标的目标仓位: <b>{((targetW ?? 0.2) * 100).toFixed(0)}%</b></span>
            <span>总仓位上限: <b>{((maxPos ?? 0.9) * 100).toFixed(0)}%</b></span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2 mt-2">
            <div className="bg-red-50 rounded-lg px-3 py-2">
              <div className="text-red-600 font-semibold">硬止损线 ({(stopPct * 100).toFixed(0)}%)</div>
              <div className="text-sm font-bold text-red-700">{stopLine || "建仓后生效"}</div>
              <div className="text-[10px] text-gray-500">浮亏超过成本{(stopPct * 100).toFixed(0)}% 自动全部卖出</div>
            </div>
            <div className="bg-green-50 rounded-lg px-3 py-2">
              <div className="text-green-600 font-semibold">移动止盈 ({(trailPct * 100).toFixed(0)}%)</div>
              <div className="text-sm font-bold text-green-700">从最高价回撤{(trailPct * 100).toFixed(0)}%</div>
              <div className="text-[10px] text-gray-500">持仓期间最高价回撤超过阈值自动锁定利润</div>
            </div>
            <div className="bg-blue-50 rounded-lg px-3 py-2">
              <div className="text-blue-600 font-semibold">持仓周期</div>
              <div className="text-sm font-bold text-blue-700">{chief?.expected_holding_period || "-"}</div>
              <div className="text-[10px] text-gray-500">首席给出的预期持有周期, 超期需重新评估</div>
            </div>
          </div>
          {chief?.key_monitoring_points?.length > 0 && (
            <div className="mt-2">
              <div className="font-semibold text-gray-700">关键监控点:</div>
              <ul className="list-disc list-inside">{chief.key_monitoring_points.slice(0, 5).map((p, i) => <li key={i}>{p}</li>)}</ul>
            </div>
          )}
          <div className="text-[10px] text-gray-400 mt-1">方案参数来自 config/risk_limits.yaml(position_monitor 止损/止盈阈值 + account_level 仓位上限), 由持仓风控巡检自动执行, 不依赖 Agent 及时性。</div>
        </div>
      ) : (
        <div className="text-xs text-gray-600 space-y-1">
          {chief?.downside_risk && <div>看空依据: {chief.downside_risk}</div>}
          {trader?.reasons?.length > 0 && <div>交易理由: {trader.reasons.slice(0, 3).join("；")}</div>}
          <div className="text-[10px] text-gray-400">卖出建议仅对已持仓标的有效(首席已按持仓一致性约束生成结论); 未持仓标的的看空结论不代表卖出。</div>
        </div>
      )}
    </div>
  );
}

/** Agent 决策中心: 最近工作流列表 + 决策链路可视化 + 准确率归因 */
export default function AgentCenter() {
  const qc = useQueryClient();
  const [sp] = useSearchParams();
  // 深链接: 监控标的页点击"最近结论"跳转到 /agents?trace=xxx
  const [traceId, setTraceId] = useState(sp.get("trace") || null);
  const [code, setCode] = useState("");
  const [tab, setTab] = useState("traces");   // traces / accuracy
  const [traceLimit, setTraceLimit] = useState(500);   // 当天全部展示(修复: 原只显示前20条)
  // 日期筛选: today / yesterday / 前天 / all(修复: 最近工作流展示太少, 前几天的可选查看)
  const [selDate, setSelDate] = useState("today");
  const dateParam = useMemo(() => {
    if (selDate === "all") return "";
    const off = selDate === "today" ? 0 : selDate === "yesterday" ? 1 : 2;
    const d = new Date();
    d.setDate(d.getDate() - off);
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${mm}-${dd}`;
  }, [selDate]);
  // 结论状态筛选 + 持仓筛选(修复: 按买入/卖出/持有/排除/失败筛选, 持仓特别标记)
  const [decFilter, setDecFilter] = useState("");
  const [holdFilter, setHoldFilter] = useState(0);   // 0=全部 1=有持仓 2=未持仓
  const { taskId, symbol: scanSymbol, status: scanStatus, error: scanError, setTask, update } = useScanStore();

  // 从其他页面跳转带 trace 参数时, 自动选中对应链路
  useEffect(() => {
    const t = sp.get("trace");
    if (t) setTraceId(t);
  }, [sp]);

  const { data: traces } = useQuery({
    queryKey: ["traces", traceLimit, dateParam, decFilter, holdFilter],
    queryFn: () => api.get("/api/workflow/traces", {
      limit: traceLimit, date: dateParam || undefined,
      decision: decFilter || undefined, holding: holdFilter || undefined,
    }),
    refetchInterval: 10000,
  });
  const traceList = traces?.traces || [];
  const traceTotal = traces?.total || 0;
  const { data: trace } = useQuery({
    queryKey: ["trace", traceId],
    queryFn: () => api.get(`/api/workflow/trace/${traceId}`),
    enabled: !!traceId,
    // 终态链路不再轮询。
    // 注意: 不能用 trace?.xxx 判断(引用自身 const 会触发 TDZ 白屏),
    // 必须用 react-query 提供的 query 参数读取当前数据。
    refetchInterval: (query) => {
      const cur = query.state.data;
      return cur?.latest_status === "RUNNING" ? 5000 : false;
    },
  });
  const { data: accuracy } = useQuery({
    queryKey: ["accuracy"],
    queryFn: () => api.get("/api/agents/accuracy?days=90&horizon_days=5"),
    enabled: tab === "accuracy",
    refetchInterval: 60000,
  });
  // 风控参数(止损止盈方案卡用)
  const { data: limits } = useQuery({
    queryKey: ["risk-limits"],
    queryFn: () => api.get("/api/risk/limits"),
    staleTime: 60000,
  });

  // 全局扫描任务状态轮询(跨页面恢复); 终态后停止
  const { data: taskStatus } = useQuery({
    queryKey: ["scantask", taskId],
    queryFn: () => api.get(`/api/scan/status/${taskId}`),
    enabled: !!taskId && scanStatus !== "DONE" && scanStatus !== "FAILED",
    refetchInterval: scanStatus === "RUNNING" ? 3000 : false,
    // 修复: 轮询接口异常(服务重启/网络)时置为终态, 不再无限卡死
    onError: () => {
      update({ status: "FAILED", error: "任务状态查询失败(服务可能已重启), 请重新分析" });
      setLastTaskState("FAILED");
    },
  });
  // 只在状态迁移时刷新工作流列表(修复: 原实现依赖每3秒变化的对象, DONE后持续刷新)
  const [lastTaskState, setLastTaskState] = useState(null);
  useEffect(() => {
    if (!taskStatus) return;
    if (taskStatus.status === "DONE") {
      if (lastTaskState !== "DONE") qc.invalidateQueries({ queryKey: ["traces"] });
      setLastTaskState("DONE");
      update({ status: "DONE" });
      if (taskStatus.trace_id) setTraceId(taskStatus.trace_id);
    } else if (taskStatus.status === "FAILED") {
      if (lastTaskState !== "FAILED") qc.invalidateQueries({ queryKey: ["traces"] });
      setLastTaskState("FAILED");
      update({ status: "FAILED", error: taskStatus.error });
    } else {
      update({ status: "RUNNING" });
    }
  }, [taskStatus]);

  const scan = useMutation({
    mutationFn: async () => {
      const sym = code.trim().toUpperCase();
      // 6位代码校验(修复: 原实现任意输入直接提交, 静默失败)
      if (!/^\d{6}$/.test(sym)) throw new Error("请输入6位代码");
      setCode("");
      const { task_id } = await api.post(`/api/scan/${sym}`);
      setTask(task_id, sym);
      return task_id;
    },
    onError: (e) => {
      const msg = e.response?.data?.detail || e.message || "分析任务提交失败";
      update({ status: "FAILED", error: msg });
    },
  });

  const scanning = taskId && scanStatus === "RUNNING";

  return (
    <div className="p-3 md:p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">Agent 决策中心</h1>
        <SystemBar />
      </div>

      {/* 触发分析(全局状态, 切页不丢) */}
      <div className="card flex items-center gap-3 flex-wrap">
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
            正在分析 {scanSymbol} ...(切换页面不会中断)
          </span>
        )}
        {taskId && scanStatus === "DONE" && (
          <span className="badge bg-green-50 text-green-600">分析完成, 已展示最新决策链路</span>
        )}
        {taskId && scanStatus === "FAILED" && (
          <span className="badge bg-red-50 text-red-600">分析失败: {scanError}</span>
        )}
        <span className="ml-auto text-xs text-gray-400">分析会依次调用: 数据闸门→7分析师→多空辩论→首席→交易员→风控→合规→执行</span>

        {/* 节点级实时进度(修复: 原实现只有"分析中"三个字, 看不到任何进度) */}
        {scanning && taskStatus?.progress_pct != null && (
          <div className="w-full">
            <div className="flex items-center gap-2 text-xs text-gray-500 mb-1">
              <span className="shrink-0">进度 {taskStatus.progress_pct}%</span>
              <span className="truncate">{taskStatus.current_node || "准备中..."}</span>
            </div>
            <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
              <div className="h-full bg-brand-600 rounded-full transition-all duration-500"
                style={{ width: `${taskStatus.progress_pct || 0}%` }} />
            </div>
            {/* 已完成的节点时间线 */}
            {(taskStatus.node_log || []).length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {taskStatus.node_log.map((n, i) => (
                  <span key={i}
                    className={`badge text-[10px] ${
                      n.status === "done" ? "bg-green-50 text-green-600"
                      : n.status === "failed" ? "bg-red-50 text-red-600"
                      : "bg-amber-50 text-amber-700 animate-pulse"}`}
                    title={n.error || ""}>
                    {n.label} {n.status === "done" ? `(${n.cost}s)` : n.status === "failed" ? "失败" : "..."}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
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
            <div className="card-title flex items-center justify-between">
              <span>最近工作流 ({(traceList || []).length}/{traceTotal})</span>
              <span className="flex gap-1">
                {[
                  ["today", "今天"],
                  ["yesterday", "昨天"],
                  ["two_days_ago", "前天"],
                  ["all", "全部"],
                ].map(([v, label]) => (
                  <button key={v}
                    className={`badge ${selDate === v ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
                    onClick={() => setSelDate(v)}>
                    {label}
                  </button>
                ))}
              </span>
            </div>
            {/* 日期筛选说明(修复: 当天全部展示, 前几天的按日选择查看) */}
            <div className="text-[11px] text-gray-400 mb-2">
              {selDate === "today" ? "展示今天的全部工作流(自动刷新)" :
               selDate === "yesterday" ? "展示昨天的工作流" :
               selDate === "two_days_ago" ? "展示前天的工作流" : "展示全部历史工作流"}
            </div>
            {/* 结论状态筛选 + 持仓筛选(修复) */}
            <div className="flex flex-wrap items-center gap-1 mb-2">
              {[
                ["", "全部"],
                ["BUY_CANDIDATE", "买入"],
                ["SELL_CANDIDATE", "卖出"],
                ["HOLD", "持有"],
                ["EXCLUDE", "排除"],
                ["FAILED", "失败"],
              ].map(([v, label]) => (
                <button key={v}
                  className={`badge ${decFilter === v ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
                  onClick={() => setDecFilter(v)}>
                  {label}
                </button>
              ))}
              <span className="mx-1 w-px h-4 bg-gray-200" />
              {[[0, "全部持仓"], [1, "持仓中"], [2, "未持仓"]].map(([v, label]) => (
                <button key={v}
                  className={`badge ${holdFilter === v ? "bg-amber-500 text-white" : "bg-gray-100 text-gray-600"}`}
                  onClick={() => setHoldFilter(v)}>
                  {label}
                </button>
              ))}
            </div>
            <div className="space-y-1.5 max-h-[600px] overflow-y-auto">
              {traceList?.length ? traceList.map((t) => {
                const sel = t.trace_id === traceId;
                const dm = decisionMeta(t.chief?.decision);
                const conf = t.chief?.confidence;
                return (
                  <button key={t.trace_id} className={`w-full text-left px-3 py-2 rounded-lg border ${sel ? "border-brand-600 bg-brand-50" : "border-gray-100 hover:bg-gray-50"}`}
                    onClick={() => setTraceId(t.trace_id)}>
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-sm">
                      {t.symbol || "全市场"}
                      {t.name && <span className="text-gray-400 font-normal"> · {t.name}</span>}
                      {t.has_position && <span className="badge bg-amber-50 text-amber-700 ml-1" title="当前账户持有该标的">持仓中</span>}
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
                      {t.start} · {(t.runs || []).length}个节点
                    </div>
                    {/* 首席决策结论: 中文+配色+置信度(修复: 原只有英文缩写, 不明显) */}
                    {t.chief && (
                      <div className="flex items-center gap-1.5 mt-1.5">
                        <span className={`badge border ${dm.cls}`}>
                          <span className="w-1.5 h-1.5 rounded-full inline-block mr-1" style={{ background: dm.dot }} />
                          {dm.label}
                        </span>
                        {conf != null && (
                          <span className="text-[11px] text-gray-500">置信 {Math.round(conf * 100)}%</span>
                        )}
                        {t.chief.score != null && (
                          <span className="text-[11px] text-gray-400">评分 {t.chief.score.toFixed(0)}</span>
                        )}
                      </div>
                    )}
                    {/* 失败原因直接展示(修复: 原实现列表看不到失败原因) */}
                    {t.last_error && (
                      <div className="text-[11px] text-red-500 mt-0.5 line-clamp-2 bg-red-50 rounded px-1.5 py-0.5">
                        失败: {t.last_error}
                      </div>
                    )}
                    {sel ? <ChevronDown size={14} className="ml-auto shrink-0 text-brand-600" /> : <ChevronRight size={14} className="ml-auto shrink-0 text-gray-300" />}
                  </button>
                );
              }) : <Empty text="该日期暂无工作流记录" />}
              {/* 全部模式才显示加载更多(修复: 原来固定前20条, 无法查看更多历史) */}
              {selDate === "all" && (traceList || []).length < (traceTotal || 0) && (
                <button className="w-full text-center text-xs text-brand-600 py-2 hover:bg-brand-50 rounded-lg"
                  onClick={() => setTraceLimit((n) => n + 100)}>
                  加载更多 ({traceList.length}/{traceTotal})
                </button>
              )}
            </div>
          </div>

        <div className="card lg:col-span-2">
          <div className="card-title flex items-center justify-between">
            <span>
              决策链路 {traceId ? `· ${traceId.slice(0, 20)}...` : ""}
              {trace?.duration != null && (
                <span className="text-xs text-gray-400 font-normal ml-2">
                  链路总耗时 {fmtDuration(trace.duration)}
                </span>
              )}
            </span>
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
                  <RiskPlanCard trace={trace} limits={limits} />
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
            <div className="overflow-x-auto">
            <table className="w-full min-w-[640px]">
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
            </div>
          ) : <Empty text="暂无足够样本(需要结论后5个交易日的数据)" />}
          <div className="text-xs text-gray-400 mt-2">
            归因口径: 首席BUY_CANDIDATE后5日上涨/SELL_CANDIDATE后5日下跌为命中; 分析师bullish后5日上涨/bearish后5日下跌为命中。样本不足时准确率参考意义有限。
          </div>
        </div>
      )}
    </div>
  );
}

