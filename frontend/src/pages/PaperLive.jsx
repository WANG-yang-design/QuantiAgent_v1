import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  Pause, Play, XCircle, Check, X, Info, ShieldAlert, Activity, Wallet,
  ChevronDown, History, RotateCcw, Archive, Download, Trash2, FileText,
} from "lucide-react";
import { api } from "../api/client";
import { fmt, fmtWan, Empty, Spin, ErrorBox, errMsg, pnlColor } from "../components/Common";
import { toastOk, toastErr } from "../components/Toast";

/** 模拟盘/实盘: 运行模式状态 + 控制 + 持仓明细 + 限额 + 人工确认 + 重置/运行记录 */
export default function PaperLive() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const { data: mode, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["sysmode"],
    queryFn: () => api.get("/api/system/mode"),
    refetchInterval: 5000,
  });
  const { data: equity } = useQuery({ queryKey: ["equity"], queryFn: () => api.get("/api/equity?limit=500") });
  // 持仓明细(修复: 原页面只有汇总数字, 看不到持仓详细情况)
  const { data: positions, isError: posErr, error: posError } = useQuery({
    queryKey: ["positions"],
    queryFn: () => api.get("/api/positions"),
    refetchInterval: 3000,
  });
  const { data: trades } = useQuery({
    queryKey: ["trades"],
    queryFn: () => api.get("/api/trades?limit=30"),
    refetchInterval: 15000,
  });
  const { data: confirmHistory } = useQuery({
    queryKey: ["confirmation-history"],
    queryFn: () => api.get("/api/confirmations/history?limit=100"),
    refetchInterval: 10000,
  });
  const { data: confirmSettings } = useQuery({
    queryKey: ["confirmation-settings"],
    queryFn: () => api.get("/api/confirmations/settings"),
  });
  // 历史运行记录(每次重置自动归档)
  const { data: archives } = useQuery({
    queryKey: ["paper-archives"],
    queryFn: () => api.get("/api/paper/archives?limit=50"),
    refetchInterval: 60000,
  });

  const pause = useMutation({
    mutationFn: () => api.post("/api/emergency/pause?reason=paper-live"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["sysmode"] }); toastOk("已暂停全部交易"); },
    onError: (e) => toastErr("暂停失败: " + errMsg(e)),
  });
  const resume = useMutation({
    mutationFn: () => api.post("/api/emergency/resume"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["sysmode"] }); toastOk("已恢复交易"); },
    onError: (e) => toastErr("恢复失败: " + errMsg(e)),
  });
  const cancelAll = useMutation({
    mutationFn: () => api.post("/api/emergency/cancel_all"),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["sysmode"] });
      qc.invalidateQueries({ queryKey: ["orders"] });
      toastOk(`已撤销 ${(r?.cancelled || []).length} 笔未成交委托`);
    },
    onError: (e) => toastErr("撤单失败: " + errMsg(e)),
  });
  const decide = useMutation({
    mutationFn: ({ id, ok }) => api.post(`/api/confirmations/${id}/decide`, { approved: ok, note: "web" }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["sysmode"] });
      qc.invalidateQueries({ queryKey: ["confirmation-history"] });
      if (r?.status && !["ORDERED", "REJECTED"].includes(r.status)) {
        toastErr(`确认处理结果: ${r.status} ${r.reason || ""}`);
      } else {
        toastOk(r?.status === "REJECTED" ? "已拒绝该交易计划" : "已批准并提交订单");
      }
    },
    onError: (e) => toastErr("确认处理失败: " + errMsg(e)),
  });

  // 持仓风控巡检
  const { data: pm } = useQuery({
    queryKey: ["position-monitor"],
    queryFn: () => api.get("/api/risk/position-monitor"),
    refetchInterval: 30000,
  });
  const [pmResult, setPmResult] = useState(null);
  const runPm = useMutation({
    mutationFn: () => api.post("/api/risk/position-monitor/run"),
    onSuccess: (r) => {
      setPmResult(r);
      const n = (r?.executed || []).length;
      if (n > 0) toastErr(`巡检触发 ${n} 笔自动止损/止盈`);
      else toastOk(`巡检完成: 检查 ${r?.checked ?? 0} 只持仓, 无触发`);
    },
    onError: (e) => toastErr("巡检失败: " + errMsg(e)),
  });

  // 市场状态自适应策略
  const { data: regime } = useQuery({
    queryKey: ["strategy-regime"],
    queryFn: () => api.get("/api/strategy/regime"),
    refetchInterval: 60000,
  });
  const [showRegimeCfg, setShowRegimeCfg] = useState(false);
  const [showRegimeHistory, setShowRegimeHistory] = useState(false);
  const [showUniverse, setShowUniverse] = useState(false);
  const [regimeForm, setRegimeForm] = useState(null);
  useEffect(() => {
    if (regime?.config) {
      setRegimeForm({
        enabled: regime.config.enabled !== false,
        risk_on_mom: ((regime.config.risk_on_mom ?? 0.02) * 100).toFixed(1),
        risk_off_mom: ((regime.config.risk_off_mom ?? -0.03) * 100).toFixed(1),
        confirm_days: regime.config.confirm_days ?? 5,
        min_switch_days: regime.config.min_switch_days ?? 20,
        presets: {
          risk_on: regime.config.presets?.risk_on || "",
          neutral: regime.config.presets?.neutral || "",
          risk_off: regime.config.presets?.risk_off || "",
        },
        manual_preset: regime.config.manual_preset || "",
      });
    }
  }, [regime?.config]);
  const saveRegime = useMutation({
    mutationFn: () => api.put("/api/strategy/regime/config", {
      enabled: regimeForm.enabled,
      risk_on_mom: Number(regimeForm.risk_on_mom) / 100,
      risk_off_mom: Number(regimeForm.risk_off_mom) / 100,
      confirm_days: Number(regimeForm.confirm_days),
      min_switch_days: Number(regimeForm.min_switch_days),
      presets: regimeForm.presets,
      manual_preset: regimeForm.manual_preset,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategy-regime"] });
      toastOk("自动切换配置已保存(立即生效)");
    },
    onError: (e) => toastErr("保存失败: " + errMsg(e)),
  });
  const resetRegime = useMutation({
    mutationFn: () => api.post("/api/strategy/regime/config/reset"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategy-regime"] });
      toastOk("已恢复 config.yaml 默认配置");
    },
    onError: (e) => toastErr("重置失败: " + errMsg(e)),
  });
  const { data: universe } = useQuery({
    queryKey: ["strategy-universe"],
    queryFn: () => api.get("/api/strategy/universe"),
    enabled: showUniverse,
    refetchInterval: showUniverse ? 60000 : false,
  });

  // ---- 模拟盘重置(先归档上一轮运行记录) ----
  const [resetOpen, setResetOpen] = useState(false);
  const [resetForm, setResetForm] = useState({ initial_cash: "100000", note: "", run_name: "", ack: false });
  const [archiveDetail, setArchiveDetail] = useState(null);
  const { data: archiveDetailData } = useQuery({
    queryKey: ["paper-archive", archiveDetail],
    queryFn: () => api.get(`/api/paper/archives/${archiveDetail}`),
    enabled: !!archiveDetail,
  });
  const resetPaper = useMutation({
    mutationFn: () => api.post("/api/paper/reset", {
      confirm: "RESET",
      initial_cash: Number(resetForm.initial_cash),
      note: resetForm.note || "Web重置",
      run_name: resetForm.run_name,
    }),
    onSuccess: (r) => {
      const s = r?.summary || {};
      toastOk(`模拟盘已重置: 新初始资金 ¥${fmt(r?.initial_cash, 2)}\n` +
        `上一轮已归档 ${r?.archive_id}: 盈亏 ${s.total_pnl >= 0 ? "+" : ""}${fmt(s.total_pnl, 2)} ` +
        `(${((s.total_return || 0) * 100).toFixed(2)}%)`);
      setResetOpen(false);
      setResetForm({ initial_cash: "100000", note: "", run_name: "", ack: false });
      ["sysmode", "positions", "trades", "equity", "orders", "paper-archives",
       "confirmation-history", "account"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
    },
    onError: (e) => toastErr("重置失败: " + errMsg(e)),
  });
  const deleteArchive = useMutation({
    mutationFn: (id) => api.delete(`/api/paper/archives/${id}`),
    onSuccess: () => {
      toastOk("已删除该运行记录(当前账户不受影响)");
      qc.invalidateQueries({ queryKey: ["paper-archives"] });
    },
    onError: (e) => toastErr("删除失败: " + errMsg(e)),
  });
  // 最近成交展开/收起(修复: 原实现一次性全铺, 无折叠)
  const [showAllTrades, setShowAllTrades] = useState(false);
  // 页签: 缩短页面长度(手机端友好)
  const [tab, setTab] = useState("account");
  useEffect(() => {
    if ((mode?.confirmations || []).length > 0) setTab("confirm");
  }, [mode?.confirmations?.length]);

  if (isLoading) return <div className="p-5"><Spin /></div>;
  if (isError) return <div className="p-5"><ErrorBox error={error} text="模拟盘状态加载失败" onRetry={refetch} /></div>;
  const acc = mode?.account || {};
  const today = mode?.today || {};
  const orderCount = Number(today.order_count || 0);
  const orderAmount = Number(today.order_amount || 0);
  const maxOrderCount = Number(today.max_order_count || 0);
  const maxOrderAmount = Number(today.max_order_amount || 0);
  const orderPct = maxOrderCount > 0 ? Math.min(100, (orderCount / maxOrderCount) * 100) : 0;
  const amountPct = maxOrderAmount > 0 ? Math.min(100, (orderAmount / maxOrderAmount) * 100) : 0;

  return (
    <div className="p-3 md:p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">模拟盘 / 实盘</h1>
      </div>

      {/* 市场状态自适应策略(regime switch) */}
      {regime && (
        <div className="card space-y-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-sm">
            <span className="font-semibold text-brand-600">市场状态自适应</span>
            <span className="flex items-center gap-1.5">
              状态:
              <b className={
                regime.regime?.state === "risk_on" ? "text-up"
                  : regime.regime?.state === "risk_off" ? "text-down" : "text-amber-600"
              }>
                {regime.regime?.state === "risk_on" ? "进攻(risk_on)"
                  : regime.regime?.state === "risk_off" ? "防守(risk_off)" : "中性(neutral)"}
              </b>
              {regime.regime?.raw_state && regime.regime.raw_state !== regime.regime.state && (
                <span className="text-[10px] text-gray-400">(原始 {regime.regime.raw_state}, 待确认)</span>
              )}
            </span>
            <span>
              当前策略: <b className="text-brand-600">{regime.selected_preset || "-"}</b>
              {regime.manual && <span className="badge bg-purple-50 text-purple-600 ml-1">手动</span>}
              {!regime.mapped && <span className="text-[10px] text-gray-400 ml-1">(回退到已选策略)</span>}
            </span>
            <span className="text-[11px] text-gray-400">
              数据截止 {regime.regime?.asof || "-"}（按收盘口径，盘中不变）
            </span>
            <div className="ml-auto flex gap-1.5">
              <button className="btn-ghost text-xs" onClick={() => setShowUniverse((v) => !v)}>
                {showUniverse ? "收起候选池" : "候选池"}
              </button>
              <button className="btn-ghost text-xs" onClick={() => setShowRegimeHistory((v) => !v)}>
                {showRegimeHistory ? "收起历史" : `切换历史 (${(regime.history || []).length})`}
              </button>
              <button className="btn-primary text-xs" onClick={() => setShowRegimeCfg((v) => !v)}>
                {showRegimeCfg ? "收起配置" : "配置自动切换"}
              </button>
            </div>
          </div>
          <div className="text-[11px] text-gray-500">{regime.reason}</div>

          {/* 配置: 阈值 + 三种状态映射 + 手动指定 */}
          {showRegimeCfg && regimeForm && (
            <div className="border border-gray-100 rounded-lg p-3 space-y-3 bg-gray-50/60">
              <div className="flex flex-wrap items-end gap-3">
                <label className="flex items-center gap-2 text-xs text-gray-600">
                  <input type="checkbox" checked={regimeForm.enabled}
                    onChange={(e) => setRegimeForm((f) => ({ ...f, enabled: e.target.checked }))} />
                  启用自动切换
                </label>
                <label className="text-xs text-gray-500">进攻阈值(20日动量≥%)
                  <input type="number" step="0.5" className="input w-20 block mt-0.5"
                    value={regimeForm.risk_on_mom}
                    onChange={(e) => setRegimeForm((f) => ({ ...f, risk_on_mom: e.target.value }))} />
                </label>
                <label className="text-xs text-gray-500">防守阈值(≤%)
                  <input type="number" step="0.5" className="input w-20 block mt-0.5"
                    value={regimeForm.risk_off_mom}
                    onChange={(e) => setRegimeForm((f) => ({ ...f, risk_off_mom: e.target.value }))} />
                </label>
                <label className="text-xs text-gray-500">确认天数
                  <input type="number" min="1" className="input w-16 block mt-0.5"
                    value={regimeForm.confirm_days}
                    onChange={(e) => setRegimeForm((f) => ({ ...f, confirm_days: e.target.value }))} />
                </label>
                <label className="text-xs text-gray-500">最小切换间隔(交易日)
                  <input type="number" min="0" className="input w-20 block mt-0.5"
                    value={regimeForm.min_switch_days}
                    onChange={(e) => setRegimeForm((f) => ({ ...f, min_switch_days: e.target.value }))} />
                </label>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                {[["risk_on", "进攻状态 →"], ["neutral", "中性状态 →"], ["risk_off", "防守状态 →"]].map(([k, label]) => (
                  <label key={k} className="text-xs text-gray-500">
                    {label}
                    <select className="input w-full mt-0.5" value={regimeForm.presets[k]}
                      onChange={(e) => setRegimeForm((f) => ({
                        ...f, presets: { ...f.presets, [k]: e.target.value } }))}>
                      <option value="">（不指定）</option>
                      {(regime.presets || []).map((n) => <option key={n} value={n}>{n}</option>)}
                    </select>
                  </label>
                ))}
              </div>
              <div className="flex flex-wrap items-end gap-3">
                <label className="text-xs text-gray-500 flex-1 min-w-[220px]">手动指定策略(覆盖自动, 空=自动)
                  <select className="input w-full mt-0.5" value={regimeForm.manual_preset}
                    onChange={(e) => setRegimeForm((f) => ({ ...f, manual_preset: e.target.value }))}>
                    <option value="">自动(按市场状态)</option>
                    {(regime.presets || []).map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                <button className="btn-ghost text-xs" disabled={resetRegime.isPending}
                  onClick={() => resetRegime.mutate()}>恢复默认(config.yaml)</button>
                <button className="btn-primary text-xs" disabled={saveRegime.isPending}
                  onClick={() => saveRegime.mutate()}>保存配置</button>
              </div>
              <div className="text-[10px] text-gray-400">
                规则: 沪深300 MA20/MA60 + 20日动量；状态需连续确认天数成立才切换(sticky)，
                且两次切换间隔≥冷却交易日。保存后立即生效，无需重启；下个交易日14:40轮动时应用。
              </div>
            </div>
          )}

          {/* 切换历史: 回看"当时用的什么策略" */}
          {showRegimeHistory && (
            <div className="overflow-x-auto max-h-72 overflow-y-auto border border-gray-100 rounded-lg">
              <table className="w-full min-w-[720px]">
                <thead><tr>
                  <th className="th">日期</th><th className="th">状态</th>
                  <th className="th">数据截止</th><th className="th">策略(前→后)</th>
                  <th className="th">触发</th><th className="th">原因</th>
                </tr></thead>
                <tbody>
                  {[...(regime.history || [])].reverse().map((h, i) => (
                    <tr key={i}>
                      <td className="td text-xs">{h.time || h.date}</td>
                      <td className="td">
                        <span className={`badge ${h.state === "risk_on" ? "bg-red-50 text-up"
                          : h.state === "risk_off" ? "bg-green-50 text-down" : "bg-amber-50 text-amber-700"}`}>
                          {h.state || "-"}
                        </span>
                        {h.raw_state && h.raw_state !== h.state && (
                          <span className="text-[10px] text-gray-400 ml-1">raw {h.raw_state}</span>
                        )}
                      </td>
                      <td className="td text-xs text-gray-500">{h.data_asof || "-"}</td>
                      <td className="td text-xs">
                        <span className="text-gray-400">{h.previous_preset || "-"}</span>
                        <span className="mx-1">→</span>
                        <b>{h.preset}</b>
                      </td>
                      <td className="td text-xs">{h.manual ? "手动" : "自动"}</td>
                      <td className="td text-xs text-gray-500">{h.reason}</td>
                    </tr>
                  ))}
                  {!(regime.history || []).length && (
                    <tr><td className="td text-gray-400" colSpan="6">
                      暂无切换记录（14:40轮动执行后自动记录状态/策略变化）
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          {/* 候选池透明化: 母池→本期候选→实际持仓 */}
          {showUniverse && (
            <div className="border border-blue-100 bg-blue-50/50 rounded-lg p-3 text-xs text-blue-900 space-y-1.5">
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <span>母池(全市场有行情ETF): <b>{universe?.mother_count ?? "-"}</b> 只</span>
                <span>本期候选池: <b>{universe?.snapshot?.candidate_count ?? "-"}</b> 只
                  (asof {(universe?.snapshot?.asof_date || "").slice(0, 10)})</span>
                <span>策略实际持仓: <b>{universe?.holdings_count ?? 0}</b> 只</span>
                <span className="text-[10px] text-blue-700/70">筛选: 上市≥{universe?.config?.min_listing_days ?? 60}日 · 流动性≥{fmtWan(universe?.config?.min_avg_amount)} · 波动率 {((universe?.config?.min_annualized_volatility ?? 0.05) * 100).toFixed(0)}%~{((universe?.config?.max_annualized_volatility ?? 0.8) * 100).toFixed(0)}% · 排除债券/货币ETF</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {(universe?.snapshot?.members || []).map((m) => (
                  <span key={m.symbol}
                    className={`badge ${m.holding ? "bg-red-50 text-up" : "bg-white text-blue-700"}`}
                    title={`#${m.rank} ${m.name} · 20日均额 ${fmtWan(m.avg_amount)} · 年化波动 ${((m.annualized_volatility || 0) * 100).toFixed(0)}% · 覆盖率 ${((m.recent_coverage || 0) * 100).toFixed(0)}%`}>
                    {m.holding ? "★" : ""}{m.symbol} {m.name}
                  </span>
                ))}
                {!universe && <span className="text-blue-500">加载中...</span>}
              </div>
              <div className="text-[10px] text-blue-700/70">{universe?.note}</div>
            </div>
          )}
        </div>
      )}

      {/* 运行模式状态 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="card">
          <div className="text-xs text-gray-500">交易模式</div>
          <div className="text-lg font-bold text-brand-600">{mode?.trade_mode?.toUpperCase()}</div>
          <div className="text-xs text-gray-400 mt-1">模拟盘(架构已按实盘标准设计)</div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">券商适配器</div>
          <div className="text-lg font-bold">{mode?.broker_adapter?.toUpperCase()}</div>
          <div className={`text-xs mt-1 ${mode?.live_connected ? "text-green-600" : "text-gray-400"}`}>
            {mode?.live_connected ? "已连接" : "实盘未接入(预留QMT/PTrade)"}
          </div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">账户状态</div>
          <div className="text-lg font-bold">{mode?.account_status === "normal" ? "正常" : mode?.account_status}</div>
          <div className="text-xs text-gray-400 mt-1">账户 {mode?.account_id || "-"}</div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">熔断状态</div>
          <div className={`text-lg font-bold ${mode?.circuit?.paused ? "text-red-600" : "text-green-600"}`}>
            {mode?.circuit?.paused ? "已熔断" : "正常"}
          </div>
          {mode?.circuit?.paused && <div className="text-xs text-red-500 mt-1">{mode.circuit.reason}</div>}
        </div>
      </div>

      {/* 控制区 */}
      <div className="card">
        <div className="card-title">控制</div>
        <div className="flex flex-wrap gap-2">
        <button className="btn-danger" disabled={pause.isPending} onClick={() => {
          if (window.confirm("确认暂停全部交易?")) pause.mutate();
        }}>
          <Pause size={14} className="inline mr-1" />一键暂停交易
        </button>
        <button className="btn-green" disabled={resume.isPending} onClick={() => resume.mutate()}>
          <Play size={14} className="inline mr-1" />恢复交易
        </button>
        <button className="btn-danger" disabled={cancelAll.isPending} onClick={() => {
          if (window.confirm("确认撤销全部未成交委托? 该操作不可恢复。")) cancelAll.mutate();
        }}>
          <XCircle size={14} className="inline mr-1" />撤销全部未成交委托
        </button>
          {/* 持仓风控巡检按钮(修复: 原在页面最底部, 用户找不到, 移到控制区) */}
          <button className="btn-primary" disabled={runPm.isPending} onClick={() => runPm.mutate()}>
            <Activity size={14} className="inline mr-1" />{runPm.isPending ? "巡检中..." : "持仓风控巡检"}
          </button>
          <button className="btn-ghost opacity-50 cursor-not-allowed" title="QMT/PTrade 未接入, 接入后开放">
            切换实盘(未接入)
          </button>
          <button className="btn-ghost opacity-50 cursor-not-allowed" title="实盘接入后开放">
            只读模式(预留)
          </button>
          <button className="btn-danger ml-auto" onClick={() => {
            // 默认沿用当前账户的初始资金(避免重置后仓位/单笔限额口径变化)
            setResetForm((f) => ({ ...f, initial_cash: String(mode?.account?.init_cash || 100000) }));
            setResetOpen(true);
          }}
            title="清空当前模拟盘交易数据并重新开始(上一轮自动归档)">
            <RotateCcw size={14} className="inline mr-1" />重置模拟盘
          </button>
        </div>
        <div className="mt-3 flex items-start gap-2 text-xs text-gray-500 bg-gray-50 rounded-lg p-3">
          <Info size={14} className="shrink-0 mt-0.5 text-brand-600" />
          <span>
            实盘接入路径(文档23, 不可跳级): ① 只读账户同步与撮合校准 → ② 撮合模型对齐 →
            ③ 半自动(人工确认后下单) → ④ 小额度自动。接入 QMT/PTrade 后此处开关自动启用,
            且实盘阈值将比模拟盘更保守。
          </span>
        </div>
      </div>

      {/* 持仓风控巡检(移到上部, 常驻可见) */}
      <div className="card">
        <div className="card-title"><ShieldAlert size={14} />持仓风控巡检(硬性止损, 不依赖Agent及时性)</div>
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <span className="text-xs text-gray-500">
            自动巡检: {pm?.config?.check_interval_seconds ? `${pm.config.check_interval_seconds / 60}分钟/次` : "-"} ·
            硬止损 {((pm?.config?.stop_loss_pct ?? 0.08) * 100).toFixed(0)}% · 移动止盈 {((pm?.config?.trailing_stop_pct ?? 0.08) * 100).toFixed(0)}% ·
            <b>仅个股止损/止盈</b>(已移除市场降仓, 大盘状态只作Agent参考) ·
            {pm?.config?.auto_execute ? " 自动执行" : " 仅告警"}
            <span className="ml-1 text-gray-400">(阈值修改见 config/risk_limits.yaml position_monitor)</span>
          </span>
          {pm?.config?.auto_execute && (
            <span className="w-full text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1">
              自动执行已开启: 仅当个股触发硬止损(浮亏超{(pm?.config?.stop_loss_pct ?? 0.08) * 100}%)或移动止盈(从最高回撤{(pm?.config?.trailing_stop_pct ?? 0.08) * 100}%)时自动卖出,
              同一标的同一天只执行一次; 市场涨跌不会自动卖股。
              (仪表盘"最近订单"来源显示为"风控巡检"; 如需仅告警不下单, 将
              config/risk_limits.yaml 的 position_monitor.auto_execute 改为 false)
            </span>
          )}
        </div>
        {pmResult && (
          <div className="text-sm">
            <div className="text-xs text-gray-500 mb-1">巡检结果: 检查 {pmResult.checked ?? 0} 只持仓 · 触发 {(pmResult.triggered || []).length} · 执行 {(pmResult.executed || []).length} · 跳过 {(pmResult.skipped || []).length}</div>
            {(pmResult.executed || []).map((e, i) => (
              <div key={i} className="flex items-center gap-2 border border-red-200 bg-red-50/50 rounded-lg px-3 py-1.5 mb-1">
                <span className="badge bg-red-50 text-red-600">{e.type}</span>
                <span className="font-medium">{e.symbol}</span>
                <span>卖出 {e.qty}份 @ {fmt(e.price)}</span>
                <span className="text-xs text-gray-500 truncate flex-1">{e.reason}</span>
                <span className="text-[10px] text-gray-400">{e.order_id}</span>
              </div>
            ))}
            {(pmResult.triggered || []).filter((t) => !(pmResult.executed || []).some((e) => e.symbol === t.symbol)).map((t, i) => (
              <div key={`t${i}`} className="flex items-center gap-2 border border-amber-200 bg-amber-50/50 rounded-lg px-3 py-1.5 mb-1">
                <span className="badge bg-amber-50 text-amber-700">{t.type}触发(未执行)</span>
                <span className="font-medium">{t.symbol}</span>
                <span className="text-xs text-gray-500 truncate">{t.reason}</span>
              </div>
            ))}
            {(pmResult.skipped || []).map((s, i) => (
              <div key={`s${i}`} className="text-xs text-gray-400">跳过: {s}</div>
            ))}
            {!(pmResult.triggered || []).length && <div className="text-xs text-green-600">持仓健康, 无触发</div>}
          </div>
        )}
      </div>

      {/* 页签: 账户/确认/记录 (缩短页面, 手机端一屏切换) */}
      <div className="flex gap-1.5 overflow-x-auto pb-0.5">
        {[
          ["account", `账户与持仓 (${(positions || []).length})`],
          ["confirm", `人工确认 (${(mode?.confirmations || []).length})`],
          ["records", "成交与运行记录"],
        ].map(([v, label]) => (
          <button key={v}
            className={`btn text-xs whitespace-nowrap ${tab === v ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
            onClick={() => setTab(v)}>
            {label}
          </button>
        ))}
      </div>

      {tab === "account" && (<>
      {/* 账户 + 限额 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card">
          <div className="card-title">账户快照</div>
          <div className="space-y-1.5">
            {[["总资产", acc.total_asset, ""], ["可用资金", acc.cash, ""], ["持仓市值", acc.market_value, ""],
              ["当日盈亏", acc.day_pnl, pnlColor(acc.day_pnl)],
              ["累计盈亏", acc.total_pnl, pnlColor(acc.total_pnl)]].map(([k, v, c]) => (
              <div key={k} className="flex justify-between py-1 border-b border-gray-50 text-sm">
                <span className="text-gray-500">{k}</span>
                <span className={`font-semibold ${c}`}>¥{fmt(v, 2)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-title">今日限额用量</div>
          <div className="space-y-4">
            <div>
              <div className="flex justify-between text-xs text-gray-500 mb-1">
                <span>交易次数</span><span>{today.order_count} / {today.max_order_count}</span>
              </div>
              <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                <div className={`h-full rounded-full ${orderPct > 80 ? "bg-red-500" : "bg-brand-600"}`} style={{ width: orderPct + "%" }} />
              </div>
            </div>
            <div>
              <div className="flex justify-between text-xs text-gray-500 mb-1">
                <span>交易金额</span><span>{fmtWan(today.order_amount)} / {fmtWan(today.max_order_amount)}</span>
              </div>
              <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                <div className={`h-full rounded-full ${amountPct > 80 ? "bg-red-500" : "bg-brand-600"}`} style={{ width: amountPct + "%" }} />
              </div>
            </div>
            <div className="text-xs text-gray-400">风控限额来自 config/risk_limits.yaml, 超过限额合规审计将拒绝下单。</div>
          </div>
        </div>
      </div>

      {/* 持仓明细(用户核心诉求: 必须能看到持仓详细情况) */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span><Wallet size={14} className="inline mr-1" />持仓明细 ({(positions || []).length})</span>
          <span className="text-xs text-gray-400 font-normal">点击持仓跳转标的详情 · 3秒自动刷新</span>
        </div>
        {posErr ? <ErrorBox error={posError} text="持仓加载失败" /> : (positions || []).length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[700px]">
              <thead>
                <tr>
                  <th className="th">代码</th><th className="th">名称</th>
                  <th className="th">总数 / 可用(T+1)</th>
                  <th className="th">成本 / 现价</th>
                  <th className="th">市值</th><th className="th">当日盈亏</th>
                  <th className="th">浮盈亏 / 盈亏率</th>
                </tr>
              </thead>
              <tbody>
                {(positions || []).map((p) => {
                  const pnl = Number(p.pnl || 0);
                  const dayPnl = Number(p.day_pnl || 0);
                  const pnlPct = Number(p.pnl_pct || 0);
                  return (
                    <tr key={p.symbol} className="cursor-pointer hover:bg-gray-50"
                      onClick={() => nav(`/symbol/${p.symbol}`)}>
                      <td className="td font-medium">{p.symbol}</td>
                      <td className="td text-gray-500">{p.name || "-"}</td>
                      <td className="td">
                        <span className="font-medium">{p.total_qty}</span>
                        <span className="text-gray-400 mx-1">/</span>
                        <span>{p.available_qty}</span>
                        {p.today_buy_qty > 0 && (
                          <span className="badge bg-amber-50 text-amber-700 ml-1" title="今日买入T+1锁定">锁 {p.today_buy_qty}</span>
                        )}
                      </td>
                      <td className="td">
                        <span>{fmt(p.cost_price)}</span>
                        <span className="text-gray-400 mx-1">/</span>
                        <span className="font-semibold">{fmt(p.latest_price)}</span>
                      </td>
                      <td className="td">{fmt(p.market_value, 2)}</td>
                      <td className={`td font-semibold ${pnlColor(dayPnl)}`}>
                        {dayPnl > 0 ? "+" : ""}{fmt(dayPnl, 2)}
                      </td>
                      <td className={`td font-semibold ${pnlColor(pnl)}`}>
                        {pnl > 0 ? "+" : ""}{fmt(pnl, 2)}
                        <span className="text-gray-400 mx-1">/</span>
                        {pnlPct > 0 ? "+" : ""}{(pnlPct * 100).toFixed(2)}%
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : <Empty text="暂无持仓(有交易后自动出现)" />}
      </div>
      </>)}

      {tab === "confirm" && (<>
      {/* 人工确认队列(修复: 移到最近成交上方, 待确认事项优先可见) */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span>人工确认队列 ({(mode?.confirmations || []).length})</span>
          <span className="text-[11px] text-gray-500 font-normal">
            {confirmSettings?.timeout_seconds ? `${Math.round(confirmSettings.timeout_seconds / 60)}分钟超时` : "-"} ·
            {confirmSettings?.timeout_action === "execute" ? " 超时自动执行（重新取价+复检）" : " 超时自动撤销"}
          </span>
        </div>
        {(mode?.confirmations || []).length ? (
          <div className="space-y-2">
            {(mode?.confirmations || []).map((c) => (
              <div key={c.confirm_id} className="flex items-start gap-3 border border-amber-200 bg-amber-50/50 rounded-lg px-3 py-2">
                <span className={`badge shrink-0 mt-0.5 ${c.risk_level === "HIGH" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-700"}`}>{c.risk_level}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium">{c.symbol} {c.name && <span className="text-gray-500">{c.name}</span>}</span>
                    <span className="text-sm font-semibold">{c.action}</span>
                    <span className="text-sm text-gray-600">¥{fmt(c.amount, 0)}</span>
                    <span className="text-[10px] text-gray-400 ml-auto">创建 {c.created_at}</span>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-1.5 mt-2 text-[11px]">
                    <span>实时价 <b>{fmt(c.context?.data_snapshot?.latest_price ?? c.context?.latest_price)}</b></span>
                    <span>行情 {c.context?.data_snapshot?.quote_time || c.context?.data_snapshot?.captured_at || "-"}</span>
                    <span>来源 {c.context?.data_snapshot?.source || "-"}</span>
                    <span>Agent {c.context?.chief_decision || c.context?.agent_decision || "-"}</span>
                    <span>到期 {c.expires_at || "-"}</span>
                  </div>
                  {/* 分析结果/原因(修复: 原只显示一行截断的 reason, 看不到分析依据) */}
                  <pre className="mt-1.5 text-[11px] text-gray-600 bg-white/60 rounded px-2 py-1.5 whitespace-pre-wrap max-h-40 overflow-y-auto">{c.reason}</pre>
                  <div className="flex gap-2 mt-2">
                    <button className="btn-green" onClick={() => {
                      if (window.confirm("确认批准该交易计划并提交模拟盘订单?")) decide.mutate({ id: c.confirm_id, ok: true });
                    }}>
                      <Check size={14} className="inline mr-1" />批准
                    </button>
                    <button className="btn-danger" onClick={() => {
                      if (window.confirm("确认拒绝该交易计划?")) decide.mutate({ id: c.confirm_id, ok: false });
                    }}>
                      <X size={14} className="inline mr-1" />拒绝
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : <Empty text="无待确认交易(交易员标注需人工确认或中高风险时出现, 附完整分析原因)" />}
      </div>

      {/* 确认操作历史：确认/拒绝/超时及后续订单成交统一串联 */}
      <div className="card">
        <div className="card-title"><History size={14} />确认操作历史 ({confirmHistory?.total ?? 0})</div>
        {(confirmHistory?.items || []).length ? (
          <div className="overflow-x-auto max-h-80 overflow-y-auto">
            <table className="w-full min-w-[1050px]">
              <thead><tr><th className="th">创建/决定</th><th className="th">标的</th><th className="th">动作</th>
                <th className="th">决策时价格</th><th className="th">处理结果</th><th className="th">处理人</th>
                <th className="th">后续订单</th><th className="th">成交</th><th className="th">链路</th></tr></thead>
              <tbody>{(confirmHistory?.items || []).map((c) => {
                const snap = c.context?.data_snapshot || c.context || {};
                return <tr key={c.confirm_id}>
                  <td className="td text-xs"><div>{(c.created_at || "").slice(0, 19)}</div><div className="text-gray-400">{(c.decided_at || "").slice(0, 19) || "待处理"}</div></td>
                  <td className="td">{c.symbol} <span className="text-gray-500">{c.name}</span></td>
                  <td className="td font-semibold">{c.action}</td>
                  <td className="td">{fmt(snap.latest_price ?? snap.plan_price)}<div className="text-[10px] text-gray-400">{snap.quote_time || snap.captured_at || "-"}</div></td>
                  <td className="td"><span className="badge bg-gray-100 text-gray-700">{c.status}</span><div className="text-[10px] text-gray-400">{c.decision_note}</div></td>
                  <td className="td text-xs">{c.decided_by || "-"}</td>
                  <td className="td text-xs">{c.order ? `${c.order.status} · ${c.order.qty}份 @${fmt(c.order.price)}` : "未下单"}</td>
                  <td className="td text-xs">{c.trade ? `${c.trade.qty}份 @${fmt(c.trade.price)}` : "未成交"}</td>
                  <td className="td">{c.trace_id ? <button className="text-brand-600 hover:underline text-xs" onClick={() => nav(`/agents?trace=${c.trace_id}`)}>查看决策</button> : "-"}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
        ) : <Empty text="暂无确认记录" />}
      </div>
      </>)}

      {tab === "records" && (<>
      {/* 今日成交 */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span>最近成交 ({(trades || []).length})</span>
          {/* 修复: 成交列表没有展开收起功能, 一页铺满几十条 */}
          {(trades || []).length > 10 && (
            <button className="btn-ghost text-xs" onClick={() => setShowAllTrades(!showAllTrades)}>
              {showAllTrades ? "收起" : `展开全部(${(trades || []).length}条)`}
              <ChevronDown size={13} className={`inline ml-1 transition-transform ${showAllTrades ? "rotate-180" : ""}`} />
            </button>
          )}
        </div>
        {(trades || []).length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px]">
              <thead>
                <tr>
                  <th className="th">时间</th><th className="th">代码</th><th className="th">名称</th>
                  <th className="th">方向</th><th className="th">价格</th><th className="th">数量</th>
                  <th className="th">手续费</th><th className="th">盈亏</th>
                </tr>
              </thead>
              <tbody>
                {(showAllTrades ? trades : trades.slice(0, 10)).map((t) => {
                  // 清仓标注(修复): 该标的当前无持仓的卖出成交标记为清仓
                  const closed = t.side === "SELL" && !(positions || []).some((p) => p.symbol === t.symbol);
                  const pnl = t.pnl != null ? Number(t.pnl) : null;
                  return (
                    <tr key={t.trade_id} className="cursor-pointer hover:bg-gray-50"
                      onClick={() => nav(`/symbol/${t.symbol}`)}>
                      <td className="td text-gray-500">{(t.trade_time || "").slice(0, 19)}</td>
                      <td className="td font-medium">{t.symbol}</td>
                      <td className="td text-gray-500">{t.name || "-"}</td>
                      <td className="td">
                        <span className={`badge ${t.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>
                          {t.side === "BUY" ? "买入" : "卖出"}
                        </span>
                        {closed && <span className="badge bg-purple-50 text-purple-600 ml-1" title="该标的已全部卖出">清仓</span>}
                      </td>
                      <td className="td">{fmt(t.price)}</td>
                      <td className="td">{t.qty}</td>
                      <td className="td text-gray-500">{fmt(t.fee, 2)}</td>
                      <td className={`td font-semibold ${pnl == null ? "text-gray-400" : pnl >= 0 ? "text-up" : "text-down"}`}>
                        {pnl == null ? "-" : `${pnl >= 0 ? "+" : ""}${fmt(pnl, 2)}`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : <Empty text="暂无成交" />}
      </div>

      {/* 历史运行记录(每次重置自动归档, 永久保留) */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span><Archive size={14} className="inline mr-1" />历史运行记录 ({(archives?.items || []).length})</span>
          <span className="text-[11px] text-gray-400 font-normal">
            重置模拟盘时自动归档 · DB + reports/paper_archive/*.json 双备份
          </span>
        </div>
        {(archives?.items || []).length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px]">
              <thead>
                <tr>
                  <th className="th">运行名称</th><th className="th">时间范围</th>
                  <th className="th">初始资金</th><th className="th">期末资产</th>
                  <th className="th">盈亏 / 收益率</th><th className="th">最大回撤</th>
                  <th className="th">交易</th><th className="th">操作</th>
                </tr>
              </thead>
              <tbody>
                {(archives?.items || []).map((a) => {
                  const pnl = Number(a.total_pnl || 0);
                  const ret = Number(a.total_return || 0);
                  return (
                    <tr key={a.archive_id} className="hover:bg-gray-50">
                      <td className="td">
                        <div className="font-medium">{a.run_name || a.archive_id}</div>
                        <div className="text-[10px] text-gray-400">{a.note || "-"}</div>
                      </td>
                      <td className="td text-xs text-gray-500">
                        {(a.started_at || "").slice(0, 16) || "-"}<br />→ {(a.ended_at || "").slice(0, 16)}
                      </td>
                      <td className="td">{fmt(a.initial_cash, 2)}</td>
                      <td className="td font-semibold">{fmt(a.final_asset, 2)}</td>
                      <td className={`td font-semibold ${pnlColor(pnl)}`}>
                        {pnl > 0 ? "+" : ""}{fmt(pnl, 2)}
                        <span className="text-gray-400 mx-1">/</span>
                        {ret > 0 ? "+" : ""}{(ret * 100).toFixed(2)}%
                      </td>
                      <td className="td text-amber-700">{((Number(a.max_drawdown) || 0) * 100).toFixed(2)}%</td>
                      <td className="td text-xs text-gray-500">
                        {a.trade_count} 笔 / {a.order_count} 单
                        {a.position_count > 0 && <span className="text-gray-400"> · 期末持仓{a.position_count}</span>}
                      </td>
                      <td className="td">
                        <div className="flex items-center gap-1">
                          <button className="btn-ghost text-xs" title="查看明细"
                            onClick={() => setArchiveDetail(a.archive_id)}>
                            <FileText size={12} />详情
                          </button>
                          <a className="btn-ghost text-xs" title="下载JSON备份"
                            href={`/api/paper/archives/${a.archive_id}/export`}
                            onClick={(e) => { e.preventDefault(); downloadArchive(a.archive_id); }}>
                            <Download size={12} />
                          </a>
                          <button className="btn-ghost text-xs text-red-500" title="删除该记录"
                            onClick={() => {
                              if (window.confirm(`确认删除运行记录 ${a.archive_id}? 仅删除归档, 不影响当前账户。`))
                                deleteArchive.mutate(a.archive_id);
                            }}>
                            <Trash2 size={12} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty text="暂无历史运行记录(点击“重置模拟盘”时会自动归档当前一轮)" />
        )}
      </div>
      </>)}

      {/* 重置模拟盘弹窗 */}
      {resetOpen && (
        <div className="modal-mask" onClick={() => !resetPaper.isPending && setResetOpen(false)}>
          <div className="modal-panel max-w-md p-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 text-red-600 font-bold mb-2">
              <RotateCcw size={16} />重置模拟盘
            </div>
            <div className="text-xs text-gray-600 bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4 space-y-1">
              <div>· 当前账户的持仓 / 订单 / 成交 / 净值曲线将被清空;</div>
              <div>· 重置前会自动归档为一条“历史运行记录”(数据库 + JSON 文件双备份);</div>
              <div>· 审计日志与历史运行记录永久保留, 不会丢失;</div>
              <div>· 策略预设、监控池、Agent 开关等配置不受影响;</div>
              <div>· 若系统处于熔断/暂停状态, 重置后仍需在控制区手动“恢复交易”。</div>
            </div>
            <div className="space-y-3">
              <label className="block">
                <span className="text-xs text-gray-500">新初始资金(元)</span>
                <input type="number" min="1" step="1000" className="input w-full mt-1"
                  value={resetForm.initial_cash}
                  onChange={(e) => setResetForm((f) => ({ ...f, initial_cash: e.target.value }))} />
              </label>
              <label className="block">
                <span className="text-xs text-gray-500">本轮名称(可选, 用于历史记录标识)</span>
                <input className="input w-full mt-1" placeholder="如: 策略V2 实盘验证"
                  value={resetForm.run_name}
                  onChange={(e) => setResetForm((f) => ({ ...f, run_name: e.target.value }))} />
              </label>
              <label className="block">
                <span className="text-xs text-gray-500">重置备注(可选)</span>
                <input className="input w-full mt-1" placeholder="如: 更换轮动参数重新开始"
                  value={resetForm.note}
                  onChange={(e) => setResetForm((f) => ({ ...f, note: e.target.value }))} />
              </label>
              <label className="flex items-start gap-2 text-xs text-gray-700 bg-gray-50 rounded-lg p-2.5 cursor-pointer">
                <input type="checkbox" className="mt-0.5" checked={resetForm.ack}
                  onChange={(e) => setResetForm((f) => ({ ...f, ack: e.target.checked }))} />
                <span>我已知晓: 重置会清空当前模拟盘交易数据(归档后无法恢复到当前状态, 只能从历史记录查看)</span>
              </label>
            </div>
            <div className="flex gap-2 justify-end mt-4">
              <button className="btn-ghost" disabled={resetPaper.isPending}
                onClick={() => setResetOpen(false)}>取消</button>
              <button className="btn-danger" disabled={!resetForm.ack || resetPaper.isPending ||
                !(Number(resetForm.initial_cash) > 0)}
                onClick={() => resetPaper.mutate()}>
                {resetPaper.isPending ? "归档并重置中..." : "确认归档并重置"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 运行记录详情弹窗 */}
      {archiveDetail && (
        <div className="modal-mask" onClick={() => setArchiveDetail(null)}>
          <div className="modal-panel max-w-3xl p-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 font-bold text-brand-600">
                <Archive size={15} />运行记录详情
              </div>
              <button className="text-gray-400 hover:text-gray-600" onClick={() => setArchiveDetail(null)}>
                <X size={18} />
              </button>
            </div>
            {!archiveDetailData ? <Spin /> : (
              <div className="space-y-4">
                <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
                  {[
                    ["运行名称", archiveDetailData.run_name || "-"],
                    ["结束时间", (archiveDetailData.ended_at || "").slice(0, 19)],
                    ["初始资金", `¥${fmt(archiveDetailData.initial_cash, 2)}`],
                    ["期末资产", `¥${fmt(archiveDetailData.final_asset, 2)}`],
                    ["总盈亏", `${Number(archiveDetailData.total_pnl) >= 0 ? "+" : ""}${fmt(archiveDetailData.total_pnl, 2)}`],
                    ["收益率", `${((Number(archiveDetailData.total_return) || 0) * 100).toFixed(2)}%`],
                    ["最大回撤", `${((Number(archiveDetailData.max_drawdown) || 0) * 100).toFixed(2)}%`],
                    ["累计手续费", `¥${fmt(archiveDetailData.total_fee, 2)}`],
                  ].map(([k, v]) => (
                    <div key={k} className="bg-gray-50 rounded-lg px-3 py-2">
                      <div className="text-[11px] text-gray-400">{k}</div>
                      <div className="font-semibold">{v}</div>
                    </div>
                  ))}
                </div>
                <div className="text-xs text-gray-500">{archiveDetailData.note}</div>
                <div>
                  <div className="text-xs font-semibold text-gray-600 mb-1.5">
                    期末持仓 ({(archiveDetailData.summary?.positions || []).length})
                  </div>
                  {(archiveDetailData.summary?.positions || []).length ? (
                    <div className="overflow-x-auto max-h-48 overflow-y-auto border border-gray-100 rounded-lg">
                      <table className="w-full min-w-[560px]">
                        <thead><tr><th className="th">代码</th><th className="th">名称</th>
                          <th className="th">数量</th><th className="th">成本</th>
                          <th className="th">最新价</th><th className="th">盈亏</th></tr></thead>
                        <tbody>{(archiveDetailData.summary.positions || []).map((p) => (
                          <tr key={p.symbol}>
                            <td className="td">{p.symbol}</td>
                            <td className="td text-gray-500">{p.name || "-"}</td>
                            <td className="td">{p.total_qty}</td>
                            <td className="td">{fmt(p.cost_price)}</td>
                            <td className="td">{fmt(p.latest_price)}</td>
                            <td className={`td ${Number(p.pnl) >= 0 ? "text-up" : "text-down"}`}>
                              {Number(p.pnl) >= 0 ? "+" : ""}{fmt(p.pnl, 2)}
                            </td>
                          </tr>))}
                        </tbody>
                      </table>
                    </div>
                  ) : <div className="text-xs text-gray-400">无</div>}
                </div>
                <div>
                  <div className="text-xs font-semibold text-gray-600 mb-1.5">
                    成交明细 ({(archiveDetailData.summary?.trades || []).length}, 最多显示100条)
                  </div>
                  {(archiveDetailData.summary?.trades || []).length ? (
                    <div className="overflow-x-auto max-h-56 overflow-y-auto border border-gray-100 rounded-lg">
                      <table className="w-full min-w-[620px]">
                        <thead><tr><th className="th">时间</th><th className="th">代码</th>
                          <th className="th">方向</th><th className="th">价格</th>
                          <th className="th">数量</th><th className="th">盈亏</th></tr></thead>
                        <tbody>{(archiveDetailData.summary.trades || []).slice(-100).reverse().map((t) => (
                          <tr key={t.trade_id}>
                            <td className="td text-xs text-gray-500">{(t.trade_time || "").slice(0, 19)}</td>
                            <td className="td">{t.symbol}</td>
                            <td className="td">
                              <span className={`badge ${t.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>
                                {t.side === "BUY" ? "买入" : "卖出"}
                              </span>
                            </td>
                            <td className="td">{fmt(t.price)}</td>
                            <td className="td">{t.qty}</td>
                            <td className={`td ${t.pnl == null ? "text-gray-400" : Number(t.pnl) >= 0 ? "text-up" : "text-down"}`}>
                              {t.pnl == null ? "-" : `${Number(t.pnl) >= 0 ? "+" : ""}${fmt(t.pnl, 2)}`}
                            </td>
                          </tr>))}
                        </tbody>
                      </table>
                    </div>
                  ) : <div className="text-xs text-gray-400">无</div>}
                </div>
                <div className="flex justify-end gap-2">
                  <button className="btn-ghost" onClick={() => downloadArchive(archiveDetailData.archive_id)}>
                    <Download size={13} className="inline mr-1" />下载完整JSON备份
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 下载归档 JSON(带鉴权头; a 标签直链无法带 Authorization)。 */
async function downloadArchive(archiveId) {
  try {
    const { downloadFile } = await import("../api/client");
    await downloadFile(`/api/paper/archives/${archiveId}/export`, `${archiveId}.json`);
  } catch (e) {
    toastErr("下载失败: " + errMsg(e));
  }
}

