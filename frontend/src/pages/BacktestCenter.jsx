import { useState, useMemo, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  AreaChart, Area, BarChart, Bar, Legend,
} from "recharts";
import { Play, History, Download, ListPlus, ChevronDown } from "lucide-react";
import { api, poll, downloadReport } from "../api/client";
import BacktestKline from "../components/BacktestKline";
import ErrorBoundary from "../components/ErrorBoundary";
import { fmt, Empty } from "../components/Common";

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

const INDEX_CODES = new Set(["000001", "000300", "000905", "399006"]);
const PERCENT_PARAM_KEYS = new Set([
  "stop_loss_pct", "hard_stop_pct", "trailing_stop_pct",
  "trailing_stop_activation", "max_buy_momentum", "min_momentum",
  "max_distance_from_ma20", "low_rebound_bonus", "low_rebound_from_low_pct",
  "max_position_in_recent_range",
]);
function MetricCard({ label, value, color = "" }) {
  return (
    <div className="card text-center">
      <div className={`text-lg font-bold ${color}`}>{value}</div>
      <div className="text-xs text-gray-500 mt-0.5">{label}</div>
    </div>
  );
}

function formatDuration(seconds) {
  if (seconds == null) return "-";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}秒`;
  const min = Math.floor(seconds / 60);
  const sec = Math.round(seconds % 60);
  return `${min}分${sec}秒`;
}

function BacktestHistory({ history, onLoad, selectedId }) {
  return (
    <div className="card">
      <div className="card-title"><History size={14} />历史回测 <span className="text-gray-400 font-normal">（点击查看完整结果）</span></div>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2 max-h-64 overflow-y-auto pr-1">
        {history?.length ? history.map((h) => (
          <button key={h.run_id}
            className={`flex items-start gap-2 px-3 py-2.5 rounded-lg border hover:border-brand-200 hover:bg-brand-50/30 text-left ${selectedId === h.run_id ? "border-brand-400 bg-brand-50/50" : "border-gray-100"}`}
            onClick={() => onLoad(h.run_id)}>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium truncate">{h.name || h.run_id}</span>
              <span className="block text-[11px] text-gray-500 mt-0.5">回测区间：{h.start} ～ {h.end}</span>
              <span className="block text-[10px] text-gray-400 mt-0.5">
                提交 {h.created_at || "-"}{h.finished_at ? ` · 完成 ${h.finished_at}` : ""}
              </span>
              <span className="block text-[10px] text-gray-400 mt-0.5">
                {h.mode === "minute" ? "分钟" : "日线"} · {h.universe_mode === "dynamic_etf" ? "历史动态ETF池" : "固定池"} · 耗时 {formatDuration(h.duration_seconds)}
              </span>
              {h.total_return != null && (
                <span className="flex gap-2 mt-1 text-[11px]">
                  <span className={h.total_return >= 0 ? "text-up" : "text-down"}>收益 {(h.total_return * 100).toFixed(2)}%</span>
                  <span className="text-gray-500">回撤 {(h.max_drawdown * 100).toFixed(2)}%</span>
                  <span className="text-gray-500">{h.trade_count ?? 0}笔</span>
                </span>
              )}
            </span>
            <span className={`badge shrink-0 ${h.status === "DONE" ? "bg-green-50 text-green-600" : h.status === "FAILED" ? "bg-red-50 text-red-600" : "bg-gray-100 text-gray-500"}`}>{h.status}</span>
          </button>
        )) : <Empty text="暂无历史回测" />}
      </div>
    </div>
  );
}

/** 买卖点K线图卡片: 按回测交易过的标的分tab切换, 显示买卖点+成本/止损包络 */
function TradeKlineCard({ runId, trades, nameMap = {} }) {
  const symbols = [...new Set((trades || []).map((t) => t.symbol))];
  const [cur, setCur] = useState(symbols[0] || "");
  // 修复: 加载不同 runId 的历史回测后重置 tab(原实现 cur 停留在旧标的)
  useEffect(() => {
    setCur(symbols[0] || "");
  }, [runId]);
  const { data, error: klineError } = useQuery({
    queryKey: ["btkline", runId, cur],
    queryFn: () => api.get(`/api/backtest/${runId}/kline/${cur}`),
    enabled: !!cur && !!runId,
  });
  if (!symbols.length) return null;
  return (
    <div className="card">
      <div className="card-title">买卖点K线图(红B=买入 绿S=卖出 · 蓝虚=成本 橙虚=止损)</div>
      <div className="flex flex-wrap gap-1.5 mb-2">
        {symbols.map((s) => {
          const name = (trades || []).find((t) => t.symbol === s)?.name || nameMap[s] || "";
          const cnt = (trades || []).filter((t) => t.symbol === s).length;
          return (
            <button key={s} className={`badge ${cur === s ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
              onClick={() => setCur(s)}>
              {s}{name ? ` · ${name}` : ""} · {cnt}笔
            </button>
          );
        })}
      </div>
      {data?.candles?.length ? (
        <>
          <div className="text-[11px] text-gray-400 mb-1">
            冻结快照 {data.snapshot_hash?.slice?.(0, 12) || "-"} · 行情覆盖 {data.coverage?.actual_days || data.candles.length}/{data.coverage?.expected_days || data.candles.length} 个交易日
          </div>
          {(data.marks || []).some((m) => !(data.candles || []).some((c) => c.date === m.date)) && (
            <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded px-3 py-2 mb-2">
              行情快照缺少部分买卖点日期，请重新运行回测，当前图表不可作为依据。
            </div>
          )}
          <ErrorBoundary fallback={(err, retry) => (
            <div className="border border-amber-200 bg-amber-50/50 rounded-lg px-3 py-2 text-sm">
              <span className="text-amber-700">K线图渲染失败: {err?.message || "未知错误"}</span>
              <button className="btn-ghost text-xs ml-2" onClick={retry}>重试</button>
            </div>
          )}>
            <BacktestKline candles={data.candles} marks={data.marks || []} roundTrips={data.round_trips || []} />
          </ErrorBoundary>
        </>
      ) : (
        <Empty text={klineError?.message || "加载冻结行情快照中"} />
      )}
    </div>
  );
}

/** 回测中心: 表单 → 异步任务轮询 → 指标/图表/明细/历史 */
export default function BacktestCenter() {
  const [form, setForm] = useState(() => {
    // 默认区间: 最近 4 个月(修复: 原硬编码 2026-04-01~2026-07-31, 时间一过就过期)
    const end = new Date();
    const start = new Date(end);
    start.setMonth(start.getMonth() - 4);
    const iso = (d) => d.toISOString().slice(0, 10);
    return {
      symbols: [], start: iso(start), end: iso(end),
      initial_cash: 20000, mode: "daily", use_agents: false, name: "",
      universe_mode: "dynamic_etf",
      params: { top_n: 4, mom_window: 20, min_amount: 30000000, max_vol: 0.55,
                target_weight: 0.21, rebalance_threshold: 0.15, max_total_position: 0.85,
                hard_stop_pct: 0.08, trailing_stop_pct: 0.06,
                trailing_stop_activation: 0.04, min_hold_days: 3, hold_buffer: 2,
                rebalance_interval_days: 3, min_momentum: 0.01, trend_ma_window: 20,
                max_buy_momentum: 0.25, fresh_stop_mult: 1.5,
                initial_ratio: 0.5, bottom_ratio: 0.0,
                require_above_ma20: true, max_distance_from_ma20: 0.15,
                max_position_in_recent_range: 1.0,
                low_rebound_bonus: 0.015, low_rebound_from_low_pct: 0.10 },
    };
  });
  const [customCode, setCustomCode] = useState("");
  const [running, setRunning] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const notify = (message, type = "success") => {
    setNotice({ message, type, id: Date.now() });
  };
  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(null), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);
  const setParam = (k, v) => setForm({ ...form, params: { ...form.params, [k]: v } });
  const clearSymbols = () => setForm({ ...form, symbols: [] });

  // 固定标的池只来自用户监控列表；系统候选由动态ETF池统一维护。
  const { data: watch } = useQuery({ queryKey: ["watchlist"], queryFn: () => api.get("/api/watchlist"), refetchInterval: 60000 });
  const pool = useMemo(() => {
    const seen = new Set();
    const list = [];
    for (const w of watch?.items || []) {
      // 指数仅作行情/基准展示，不能作为可下单资产进入策略回测池。
      if (w.enabled && w.asset_type !== "index" && !seen.has(w.symbol)) {
        seen.add(w.symbol); list.push({ symbol: w.symbol, name: w.name });
      }
    }
    return list;
  }, [watch]);

  const addCustom = () => {
    const code = customCode.trim().toUpperCase();
    if (!/^\d{6}$/.test(code)) {
      notify("请输入 6 位股票或 ETF 代码", "warning");
      return;
    }
    if (INDEX_CODES.has(code)) {
      notify("指数不能直接交易，请选择对应的股票或 ETF 进行回测", "warning");
      return;
    }
    if (!form.symbols.includes(code)) setForm({ ...form, symbols: [...form.symbols, code] });
    setCustomCode("");
  };

  const { data: history } = useQuery({ queryKey: ["btlist"], queryFn: () => api.get("/api/backtest/list?limit=30"), refetchInterval: running ? 5000 : false });

  // 一键加入监控标的/自选池(修复: 回测结果不能直接转成实盘监控池)
  const qc = useQueryClient();
  const addWatch = useMutation({
    mutationFn: async (symbols) => {
      const list = [...new Set((symbols || []).filter(Boolean))];
      for (const s of list) {
        await api.post("/api/watchlist", { symbol: s, categories: ["watched"] });
      }
      return list.length;
    },
    onSuccess: (n) => {
      qc.invalidateQueries({ queryKey: ["watchlist"] });
      notify(`已添加 ${n} 只标的到监控列表，可在“监控标的”页查看`);
    },
    onError: (e) => notify("添加失败: " + (e.response?.data?.detail || e.message), "error"),
  });

  // 命名策略: 保存当前参数 → 应用到回测表单 / 一键应用到模拟盘
  const [presetName, setPresetName] = useState("");
  const { data: presets } = useQuery({
    queryKey: ["strategy-presets"],
    queryFn: () => api.get("/api/strategies/presets"),
  });
  const savePreset = useMutation({
    mutationFn: () => api.post("/api/strategies/presets", {
      name: presetName, params: form.params, universe_mode: form.universe_mode,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategy-presets"] });
      setPresetName("");
      notify("策略已保存，可在下方列表加载或应用到模拟盘");
    },
    onError: (e) => notify("保存失败: " + (e.response?.data?.detail || e.message), "error"),
  });
  const applyPaper = useMutation({
    mutationFn: (name) => api.post(`/api/strategies/presets/${name}/apply_paper`),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["strategy-presets"] });
      notify(r.active_paper ? `模拟盘轮动已使用「${r.active_paper}」的完整参数` : "已恢复模拟盘默认参数");
    },
    onError: (e) => notify("应用失败: " + (e.response?.data?.detail || e.message), "error"),
  });
  const deletePreset = useMutation({
    mutationFn: (name) => api.delete(`/api/strategies/presets/${name}`),
    onSuccess: (_, name) => {
      qc.invalidateQueries({ queryKey: ["strategy-presets"] });
      notify(`已删除策略「${name}」`);
    },
    onError: (e) => notify("删除失败: " + (e.response?.data?.detail || e.message), "error"),
  });

  const submit = useMutation({
    mutationFn: async () => {
      // 表单校验(修复: 原实现无任何校验, 非法参数直接提交等后端报错)
      if (form.universe_mode === "manual" && !form.symbols.length) throw new Error("请至少选择 1 只标的");
      if (!form.start || !form.end) throw new Error("请选择回测日期区间");
      if (form.start >= form.end) throw new Error("开始日期必须早于结束日期");
      if (!form.initial_cash || form.initial_cash <= 0) throw new Error("初始资金必须大于 0");
      if (form.universe_mode === "dynamic_etf" && form.mode !== "daily") throw new Error("动态ETF池当前仅支持日线回测");
      const p = form.params || {};
      if (p.max_total_position <= 0 || p.max_total_position > 1) throw new Error("总仓位上限应在 0~1 之间");
      if (p.target_weight <= 0 || p.target_weight > 1) throw new Error("单标的目标仓位应在 0~1 之间");
      if (p.top_n < 1 || p.top_n > 10) throw new Error("持有数量应在 1~10 之间");
      setError(null); setMetrics(null); setRunning({ status: "PENDING", progress: "排队中..." });
      const { run_id } = await api.post("/api/backtest/submit", form);
      setRunning({ run_id, status: "PENDING", progress: "排队中..." });
      const final = await poll(`/api/backtest/${run_id}`,
        (d) => setRunning({ run_id, status: d.status, progress: d.progress }),
        { interval: 2000 });
      // 修复: 回测 FAILED 时原实现静默回到初始状态(用户以为没点着),
      // 现在展示失败原因并保留进度条信息
      if (final.status === "FAILED") {
        const msg = final.progress || "回测失败";
        setRunning(null);
        throw new Error(msg);
      }
      if (final.status === "DONE" && final.metrics) {
        setMetrics(final.metrics);
        setSelectedRunId(final.run_id);
        setRunning(null);
        return final;
      }
      setRunning(null);
      throw new Error("回测结果异常(状态: " + (final.status || "未知") + ")");
    },
    onError: (e) => { setRunning(null); setError(e.response?.data?.detail || e.message); },
  });

  // 基准曲线: 只有后端真正返回 benchmark_curve 才渲染基准线
  // (修复: benchmark_return 存在但 benchmark_curve 缺失时, 原实现把基准画成与净值重合, 误导判断)
  const hasBench = Array.isArray(metrics?.benchmark_curve) && metrics.benchmark_curve.length > 0;
  const eqData = (metrics?.equity_curve || []).map((v, i) => ({
    d: metrics?.dates?.[i] || i, 净值: v, 基准: hasBench ? metrics.benchmark_curve[i] : null,
  }));
  const ddData = (metrics?.drawdown_curve || []).map((v, i) => ({ d: metrics?.dates?.[i] || i, 回撤: (v * 100).toFixed(2) }));
  const posData = (metrics?.position_curve || []).map((v, i) => ({
    d: metrics?.dates?.[i] || i,
    p: metrics?.params?.initial_cash ? Math.min(1, v / metrics.params.initial_cash) : 0,
  }));
  const mData = Object.entries(metrics?.monthly_returns || {}).map(([k, v]) => ({ month: k, ret: (v * 100).toFixed(2) }));
  const displayName = (symbol, direct = "") => direct || metrics?.symbol_names?.[symbol]
    || (metrics?.universe_snapshots || []).flatMap((x) => x.members || []).find((x) => x.symbol === symbol)?.name
    || (watch?.items || []).find((x) => x.symbol === symbol)?.name || "";
  const exitReasonStats = useMemo(() => {
    if (metrics?.exit_reason_stats) {
      const s = metrics.exit_reason_stats;
      return { total: s.total_sells || 0, cost_stop: s.cost_stop || 0,
        trailing_stop: s.trailing_stop || 0, rotation: s.rotation || 0,
        market: s.market || 0, final: s.final || 0, other: s.other || 0 };
    }
    const sells = (metrics?.trade_details || []).filter((t) => t.side === "SELL");
    const groups = { cost_stop: 0, trailing_stop: 0, rotation: 0, market: 0, final: 0, other: 0 };
    for (const t of sells) {
      const reason = String(t.reason || "");
      if (reason.includes("成本止损")) groups.cost_stop += 1;
      else if (reason.includes("移动止损")) groups.trailing_stop += 1;
      else if (reason.includes("排名") || reason.includes("轮动")) groups.rotation += 1;
      else if (reason.includes("risk_off") || reason.includes("市场")) groups.market += 1;
      else if (reason.includes("期末平仓")) groups.final += 1;
      else groups.other += 1;
    }
    return { total: sells.length, ...groups };
  }, [metrics]);

  const toggleSym = (s) => {
    const cur = form.symbols;
    setForm({ ...form, symbols: cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s] });
  };

  const loadHistory = async (runId) => {
    setMetrics(null); setError(null);
    setSelectedRunId(runId);
    try {
      const r = await api.get(`/api/backtest/${runId}`);
      if (r.status === "DONE" && r.metrics) setMetrics(r.metrics);
      else setError(`该回测无结果(${r.status || "数据缺失"}), 可能是历史残留记录`);
    } catch (e) {
      setError("该回测记录不存在或已被清理: " + (e.response?.data?.detail || e.message));
    }
  };

  return (
    <div className="p-3 md:p-5 space-y-4">
      {notice && (
        <div
          key={notice.id}
          role="status"
          aria-live="polite"
          className={`fixed right-4 top-4 z-[80] max-w-sm rounded-lg border px-4 py-3 text-sm shadow-lg transition-all ${
            notice.type === "error"
              ? "border-red-200 bg-red-50 text-red-700"
              : notice.type === "warning"
                ? "border-amber-200 bg-amber-50 text-amber-700"
                : "border-green-200 bg-green-50 text-green-700"
          }`}
        >
          {notice.message}
        </div>
      )}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">回测中心</h1>
      </div>

      {/* 历史记录放在操作区上方，便于随时切换结果 */}
      <BacktestHistory history={history} onLoad={loadHistory} selectedId={selectedRunId} />

      {/* 表单 */}
      <div className="card grid grid-cols-1 xl:grid-cols-3 gap-3 items-start">
        <div className="xl:col-span-2">
          <div className="flex items-center gap-3 mb-1.5">
            <span className="text-xs text-gray-500">一级候选池</span>
            <label className="text-xs flex items-center gap-1">
              <input type="radio" checked={form.universe_mode === "dynamic_etf"}
                onChange={() => setForm({ ...form, universe_mode: "dynamic_etf", mode: "daily" })} />
              历史时点动态ETF池（推荐）
            </label>
            <label className="text-xs flex items-center gap-1">
              <input type="radio" checked={form.universe_mode === "manual"}
                onChange={() => setForm({ ...form, universe_mode: "manual" })} />
              手工固定池
            </label>
          </div>
          {form.universe_mode === "dynamic_etf" && (
            <div className="text-[11px] text-green-700 bg-green-50 border border-green-100 rounded px-2 py-1.5 mb-2">
              每月仅使用当时已上市且数据、流动性、波动率合格的ETF生成快照；下方手工选择在此模式不参与回测。
            </div>
          )}
          <div className="text-xs text-gray-500 mb-1.5">手工固定池(来自监控列表+常用, 代码/名称)</div>
          <div className="flex flex-wrap gap-1.5 max-h-28 overflow-y-auto mb-2">
            {pool.map(({ symbol, name }) => (
              <button key={symbol} onClick={() => toggleSym(symbol)}
                className={`badge ${form.symbols.includes(symbol) ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
                title={name || symbol}>
                {symbol} {name && <span className="opacity-70">·{name}</span>}
              </button>
            ))}
          </div>
          <div className={`flex gap-2 ${form.universe_mode === "dynamic_etf" ? "opacity-50" : ""}`}>
            <input className="input w-28" placeholder="自定义代码" value={customCode}
              onChange={(e) => setCustomCode(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addCustom()} />
            <button type="button" className="btn-ghost" onClick={addCustom}>添加</button>
            <button type="button" className="btn-ghost" onClick={clearSymbols} disabled={!form.symbols.length}>清空</button>
            {/* 修复: 一键导入监控列表 —— 把监控列表的全部标的加入回测选中(监控→回测) */}
            <button type="button" className="btn-primary text-xs"
                disabled={!(watch?.items || []).some((i) => i.enabled && i.asset_type !== "index")}
              title="把监控列表中所有启用的标的加入回测选中, 直接对这些股票回测"
              onClick={() => {
                const syms = (watch?.items || []).filter((i) => i.enabled && i.asset_type !== "index").map((i) => i.symbol);
                const merged = [...new Set([...form.symbols, ...syms])];
                setForm({ ...form, symbols: merged });
                notify(`已导入 ${syms.length} 只监控标的，当前共选中 ${merged.length} 只`);
              }}>
              <ListPlus size={13} className="inline mr-0.5" />
              导入监控列表({(watch?.items || []).filter((i) => i.enabled && i.asset_type !== "index").length})
            </button>
            <span className="text-[11px] text-gray-400 self-center">已选 {form.symbols.length} 只</span>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 self-start">
          <label className="text-xs text-gray-500">开始日期
            <input type="date" className="input w-full mt-1" value={form.start}
              onChange={(e) => setForm({ ...form, start: e.target.value })} />
          </label>
          <label className="text-xs text-gray-500">结束日期
            <input type="date" className="input w-full mt-1" value={form.end}
              onChange={(e) => setForm({ ...form, end: e.target.value })} />
          </label>
          <label className="text-xs text-gray-500">初始资金
            <input type="number" className="input w-full mt-1" value={form.initial_cash}
              onChange={(e) => setForm({ ...form, initial_cash: Number(e.target.value) })} />
          </label>
          <label className="text-xs text-gray-500">模式
            <select className="input w-full mt-1" value={form.mode}
              onChange={(e) => setForm({ ...form, mode: e.target.value })}>
              <option value="daily">日线回测(次日开盘成交)</option>
              <option value="minute" disabled={form.universe_mode === "dynamic_etf"}>分钟回测(5m)</option>
            </select>
          </label>
        </div>
        <div className="xl:col-span-3 flex flex-col gap-2 border-t border-gray-100 pt-3">
          {/* 策略参数(默认折叠: 页面更短, 需要时展开微调) */}
          <div>
            <button className="text-xs text-gray-500 mb-1.5 flex items-center gap-1 hover:text-brand-600"
              onClick={() => setShowAdvanced((v) => !v)}>
              <ChevronDown size={13} className={`transition-transform ${showAdvanced ? "" : "-rotate-90"}`} />
              轮动策略参数(修改后立即生效){showAdvanced ? "" : " · 点击展开"}
            </button>
            {showAdvanced && (
            <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-2">
              {[
                ["top_n", "持有数量", "number"],
                ["mom_window", "动量窗口", "number"],
                ["target_weight", "单标的目标仓位", "number"],
                ["max_total_position", "总仓位上限", "number"],
                ["rebalance_threshold", "再平衡阈值", "number"],
                ["max_vol", "波动率上限", "number"],
                ["min_amount", "成交额下限(万)", "number"],
                ["hard_stop_pct", "成本硬止损(%)", "number"],
                ["trailing_stop_pct", "移动止损(%)", "number"],
                ["trailing_stop_activation", "移动止损启用盈利(%)", "number"],
                ["min_hold_days", "最小持仓天数", "number"],
                ["rebalance_interval_days", "轮动间隔(交易日)", "number"],
                ["fresh_stop_mult", "新仓止损放宽倍", "number"],
                ["max_buy_momentum", "追高保护(%)", "number"],
                ["min_momentum", "最低动量(%)", "number"],
                ["max_distance_from_ma20", "距MA20过热(%)", "number"],
                ["max_position_in_recent_range", "20日区间追高线(%)", "number"],
                ["trend_ma_window", "趋势均线窗口", "number"],
                ["low_rebound_bonus", "低位启动加分(%)", "number"],
                ["low_rebound_from_low_pct", "低位回升阈值(%)", "number"],
              ].map(([k, label, type]) => (
                <label key={k} className="text-[11px] text-gray-500">
                  {label}
                  <input type={type} step={k.includes("weight") || k.includes("vol") || k.includes("threshold") || k.includes("position") || k.includes("momentum") || k.includes("distance") || k.includes("rebound") ? 0.01 : 1}
                    className="input w-full mt-0.5 text-xs"
                    value={PERCENT_PARAM_KEYS.has(k) ? ((form.params[k] ?? 0) * 100) : (k === "min_amount" ? (form.params[k] ?? 30000000) / 10000 : form.params[k])}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      if (PERCENT_PARAM_KEYS.has(k)) setParam(k, isNaN(v) ? 0 : v / 100);
                      else if (k === "min_amount") setParam(k, isNaN(v) ? 0 : v * 10000);
                      else setParam(k, isNaN(v) ? 0 : v);
                    }} />
                </label>
              ))}
              <label className="text-[11px] text-gray-500 flex items-center gap-1.5 pt-4">
                <input type="checkbox" checked={form.params.market_filter}
                  onChange={(e) => setParam("market_filter", e.target.checked)} />
                市场风险过滤(熊市空仓)
              </label>
              <label className="text-[11px] text-gray-500 flex items-center gap-1.5 pt-4" title="必须站上所选趋势均线才买入, 防止下跌趋势中接刀">
                <input type="checkbox" checked={form.params.require_above_ma20}
                  onChange={(e) => setParam("require_above_ma20", e.target.checked)} />
                买入需站上趋势均线
              </label>
            </div>
            )}
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-600">
            <input type="checkbox" checked={form.use_agents}
              onChange={(e) => setForm({ ...form, use_agents: e.target.checked })} />
            关键节点调用 Agent(慢, 消耗token)
          </label>
          <input className="input w-full" placeholder="回测名称(可选)" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <div className="flex gap-2">
            <button className="btn-primary flex-1" disabled={submit.isPending || (form.universe_mode === "manual" && !form.symbols.length)} onClick={() => submit.mutate()}>
              <Play size={14} className="inline mr-1" />{submit.isPending ? "提交中..." : "开始回测"}
            </button>
            <button className="btn-ghost" disabled={!form.symbols.length || addWatch.isPending}
              title="把已选标的加入监控列表(回测→监控, 反向操作)"
              onClick={() => addWatch.mutate(form.symbols)}>
              <ListPlus size={14} className="inline mr-1" />已选→监控
            </button>
          </div>

          {/* 命名策略：模拟盘执行仍受风控、交易规则和监控池约束 */}
          <div className="border-t border-gray-100 pt-2 mt-2">
            <div className="text-xs text-gray-500 mb-1.5">命名策略（保存当前参数，一键应用回测/模拟盘）</div>
            <div className="flex gap-1.5 mb-1.5">
              <input className="input text-xs flex-1" placeholder="策略名称, 如: 稳健轮动" value={presetName}
                onChange={(e) => setPresetName(e.target.value)} />
              <button className="btn-ghost text-xs" disabled={savePreset.isPending || !presetName.trim()}
                onClick={() => savePreset.mutate()}>保存</button>
            </div>
            <div className="space-y-1 max-h-32 overflow-y-auto">
              {(presets?.presets || []).map((p) => (
                <div key={p.name} className="flex items-center gap-1.5 px-2 py-1 rounded border border-gray-100 text-xs">
                  <span className="font-medium truncate">{p.name}</span>
                  {p.active_paper && <span className="badge bg-green-50 text-green-600 shrink-0">模拟盘生效中</span>}
                  {!p.compatible_dynamic && <span className="badge bg-amber-50 text-amber-700 shrink-0">旧固定池·仅复查</span>}
                  <span className="ml-auto flex gap-1 shrink-0">
                    <button className="btn-ghost text-[11px]" title="加载到回测表单"
                      onClick={() => { setForm({ ...form, params: { ...form.params, ...p.params } }); notify(`已加载「${p.name}」参数到回测表单`); }}>
                      应用回测
                    </button>
                    <button className="btn-ghost text-[11px] text-brand-600"
                      disabled={applyPaper.isPending || !p.compatible_dynamic}
                      title={p.compatible_dynamic ? "把动态ETF池回测参数应用到自动模拟盘轮动" : "旧固定池策略不可上动态ETF模拟盘，请重新寻优"}
                      onClick={() => applyPaper.mutate(p.name)}>
                      上模拟盘
                    </button>
                    <button className="text-gray-300 hover:text-red-500" title="删除"
                      disabled={deletePreset.isPending}
                      onClick={() => deletePreset.mutate(p.name)}>×</button>
                  </span>
                </div>
              ))}
              {!(presets?.presets || []).length && (
                <div className="text-[11px] text-gray-400">暂无保存的策略 —— 调好参数后点"保存"命名, 以后可直接复用</div>
              )}
              {presets?.active_paper && (
                <button className="text-[11px] text-amber-600 hover:underline"
                  disabled={applyPaper.isPending}
                  onClick={() => applyPaper.mutate("__default__")}>
                  恢复默认参数（清除模拟盘覆盖）
                </button>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* 进度 */}
      {running && (
        <div className="card">
          <div className="card-title">回测任务 {running.run_id}</div>
          <div className="flex items-center gap-3">
            <span className={`badge ${running.status === "FAILED" ? "bg-red-50 text-red-600" : "bg-brand-50 text-brand-600"}`}>{running.status}</span>
            <div className="flex-1 h-2 bg-gray-100 rounded-full overflow-hidden">
              <div className="h-full bg-brand-600 rounded-full transition-all"
                style={{ width: (() => { const m = /(\d+)%/.exec(running.progress || ""); return m ? m[1] + "%" : (running.status === "DONE" ? "100%" : "8%"); })() }} />
            </div>
            <span className="text-xs text-gray-500">{running.progress}</span>
          </div>
        </div>
      )}
      {error && <div className="card border-red-200 bg-red-50 text-red-600 text-sm">{error}</div>}

      {/* 结果 */}
      {metrics && (
        <>
          {metrics.data_integrity_error && (
            <div className="card border-red-300 bg-red-50 text-red-700 text-sm font-medium">
              数据完整性校验失败：{metrics.data_integrity_error}
            </div>
          )}
          {/* 参数摘要 + 提示 */}
          <div className="card">
            <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
              <span className="font-semibold text-gray-600">参数:</span>
              <span className="badge bg-gray-100">
                {metrics.params?.universe_mode === "dynamic_etf"
                  ? "历史动态ETF池"
                  : `固定标的 ${form.symbols.length}只`}
              </span>
              <span className="badge bg-gray-100">{metrics.params?.start} ~ {metrics.params?.end}</span>
              <span className="badge bg-gray-100">资金 {metrics.params?.initial_cash?.toLocaleString?.() || form.initial_cash}</span>
              <span className="badge bg-gray-100">{metrics.params?.mode === "minute" ? "分钟" : "日线"}</span>
              <span className="badge bg-gray-100">Top{metrics.params?.top_n} 窗口{metrics.params?.mom_window}日</span>
              <span className="badge bg-gray-100">单标的目标 {((metrics.params?.target_weight ?? 0.2) * 100).toFixed(0)}%</span>
              <span className="badge bg-gray-100">总仓位 ≤ {((metrics.params?.max_total_position ?? 0.9) * 100).toFixed(0)}%</span>
              <span className="badge bg-gray-100">波动率≤{(metrics.params?.max_vol ?? 0.5) * 100}%</span>
              {metrics.params?.use_agents && <span className="badge bg-amber-50 text-amber-700">Agent模式</span>}
            </div>
            {metrics.note && (
              <div className="mt-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                {metrics.note}
              </div>
            )}
            {Object.keys(metrics.data_coverage || {}).length > 0 && (() => {
              const covs = Object.values(metrics.data_coverage);
              const abnormal = covs.filter((r) =>
                Number(r.coverage || 0) < 0.98 || (r.missing_days || []).length > 0);
              const minCov = Math.min(...covs.map((r) => Number(r.coverage || 0)));
              const hasIssue = abnormal.length > 0;
              return (
                <details className={`mt-2 text-xs border rounded-lg px-3 py-2 ${
                  hasIssue ? "text-amber-700 bg-amber-50 border-amber-200"
                           : "text-emerald-700 bg-emerald-50 border-emerald-200"}`}>
                  <summary className="cursor-pointer font-medium">
                    行情完整性: {covs.length} 只 · 最低覆盖 {(minCov * 100).toFixed(1)}%
                    · 缺口/异常 {abnormal.length} 只 · 快照 {metrics.data_snapshot_hash?.slice?.(0, 12)}
                    <span className="font-normal opacity-70">（点击展开明细）</span>
                  </summary>
                  <div className="mt-1.5 space-y-0.5 max-h-56 overflow-y-auto">
                    {covs.map((r) => (
                      <div key={r.symbol}>
                        {r.symbol} {(Number(r.coverage || 0) * 100).toFixed(1)}%
                        （{r.actual_start || "无数据"}～{r.actual_end || "无数据"}
                        {r.pre_listing_days ? `，上市前 ${r.pre_listing_days} 日已排除` : ""}
                        {r.missing_days?.length ? `，上市后缺口 ${r.missing_days.length} 日` : ""}
                        {r.price_adjustments?.length ? `，复权口径修正 ${r.price_adjustments.length} 处` : ""}）
                      </div>
                    ))}
                  </div>
                </details>
              );
            })()}
            {(metrics.universe_snapshots || []).length > 0 && (
              <details className="mt-2 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-800">
                <summary className="cursor-pointer font-semibold">
                  一级候选池快照 {(metrics.universe_snapshots || []).length} 期
                  （动态池每月成员，可审计、无未来数据，点击展开）
                </summary>
                <div className="mt-2 space-y-2 max-h-64 overflow-y-auto">
                  {(metrics.universe_snapshots || []).map((s) => (
                    <div key={s.snapshot_id}>
                      <div className="font-medium">
                        {String(s.effective_date).slice(0, 10)} · {s.candidate_count ?? s.members?.length ?? 0} 只
                        <span className="font-normal opacity-60 ml-2">asof {String(s.asof_date || "").slice(0, 10)}</span>
                      </div>
                      <div className="flex flex-wrap gap-1 mt-1">
                        {(s.members || []).map((m) => (
                          <span key={m.symbol} className="badge bg-white text-blue-700"
                            title={`${m.symbol} ${m.name || ""} · 20日均额 ${((m.avg_amount || 0) / 1e4).toFixed(0)}万 · 波动 ${((m.annualized_volatility || 0) * 100).toFixed(0)}%`}>
                            {m.symbol} {m.name || ""}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </details>
            )}
            {metrics.skipped_buys > 0 && (
              <div className="mt-1 text-xs text-gray-400">
                因资金不足跳过 {metrics.skipped_buys} 次买入(总仓位上限或现金不足时正常现象)
              </div>
            )}
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-2">
            <MetricCard label="总收益" value={(metrics.total_return * 100).toFixed(2) + "%"} color={(metrics.total_return || 0) >= 0 ? "text-up" : "text-down"} />
            <MetricCard label="年化" value={(metrics.annual_return * 100).toFixed(2) + "%"} />
            <MetricCard label="最大回撤" value={(metrics.max_drawdown * 100).toFixed(2) + "%"} color="text-down" />
            <MetricCard label="夏普" value={metrics.sharpe?.toFixed(2)} />
            <MetricCard label="卡玛" value={metrics.calmar?.toFixed(2)} />
            <MetricCard label="胜率" value={metrics.win_rate == null ? "样本不足" : (metrics.win_rate * 100).toFixed(0) + "%"} />
            <MetricCard label="成交笔数" value={metrics.trade_count} />
            <MetricCard label="超额收益" value={(metrics.benchmark?.excess_return * 100)?.toFixed(2) + "%"} color="text-brand-600" />
          </div>

          {exitReasonStats.total > 0 && (
            <div className="card">
              <div className="card-title">卖出原因诊断</div>
              <div className="flex flex-wrap gap-2 text-xs">
                <span className="badge bg-red-50 text-red-700">成本止损 {exitReasonStats.cost_stop}笔</span>
                <span className="badge bg-amber-50 text-amber-700">移动止损 {exitReasonStats.trailing_stop}笔</span>
                <span className="badge bg-blue-50 text-blue-700">排名轮动 {exitReasonStats.rotation}笔</span>
                <span className="badge bg-purple-50 text-purple-700">市场风控 {exitReasonStats.market}笔</span>
                <span className="badge bg-gray-100 text-gray-600">期末平仓 {exitReasonStats.final}笔</span>
                {exitReasonStats.other > 0 && <span className="badge bg-gray-100 text-gray-600">其他 {exitReasonStats.other}笔</span>}
                <span className="ml-auto text-gray-500">
                  止损占卖出 {(((exitReasonStats.cost_stop + exitReasonStats.trailing_stop) / exitReasonStats.total) * 100).toFixed(1)}%
                </span>
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="card">
              <div className="card-title">净值 vs 沪深300</div>
              <ResponsiveContainer width="100%" height={240}>
                <LineChart data={eqData}>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                  <XAxis dataKey="d" fontSize={9} tickFormatter={(v) => String(v).slice(5)} minTickGap={40} />
                  <YAxis fontSize={10} domain={["auto", "auto"]} />
                  <Tooltip />
                  <Legend />
                  <Line type="monotone" dataKey="净值" stroke="#1c3a5e" dot={false} strokeWidth={1.6} />
                  {hasBench && <Line type="monotone" dataKey="基准" stroke="#f59f00" dot={false} strokeWidth={1.2} />}
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="card">
              <div className="card-title">回撤曲线</div>
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={ddData}>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                  <XAxis dataKey="d" fontSize={9} tickFormatter={(v) => String(v).slice(5)} minTickGap={40} />
                  <YAxis fontSize={10} />
                  <Tooltip />
                  <Area type="monotone" dataKey="回撤" stroke="#e03131" fill="#e03131" fillOpacity={0.3} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="card">
              <div className="card-title">持仓市值曲线(占总资产比例)</div>
              <ResponsiveContainer width="100%" height={160}>
                <AreaChart data={posData}>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                  <XAxis dataKey="d" fontSize={9} tickFormatter={(v) => String(v).slice(5)} minTickGap={50} />
                  <YAxis fontSize={10} tickFormatter={(v) => (v * 100).toFixed(0) + "%"} domain={[0, 1]} />
                  <Tooltip formatter={(v) => [(v * 100).toFixed(1) + "%", "持仓比例"]} />
                  <Area type="monotone" dataKey="p" stroke="#1971c2" fill="#1971c2" fillOpacity={0.25} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div className="card">
              <div className="card-title">月度收益(%)</div>
              <ResponsiveContainer width="100%" height={160}>
                <BarChart data={mData}>
                  <XAxis dataKey="month" fontSize={10} />
                  <YAxis fontSize={10} />
                  <Tooltip />
                  <Bar dataKey="ret" fill="#1c3a5e" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* 单标的收益统计(修复: 轮动换仓后看不到每只标的的贡献) */}
          {(metrics.symbol_stats || []).length > 0 && (
            <div className="card">
              <div className="card-title">单标的收益统计(按已实现盈亏排序)</div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px]">
                  <thead><tr>
                    <th className="th">标的</th><th className="th">名称</th>
                    <th className="th">买入次数</th><th className="th">买入金额</th>
                    <th className="th">卖出次数</th><th className="th">已实现盈亏</th>
                    <th className="th">胜/负</th><th className="th">胜率</th>
                  </tr></thead>
                  <tbody>
                    {(metrics.symbol_stats || []).map((st) => (
                      <tr key={st.symbol}>
                        <td className="td font-medium">{st.symbol}</td>
                        <td className="td text-gray-500">{displayName(st.symbol, st.name) || "-"}</td>
                        <td className="td">{st.buy_count}</td>
                        <td className="td">{fmt(st.buy_amount, 0)}</td>
                        <td className="td">{st.sell_count}</td>
                        <td className={`td font-semibold ${st.realized_pnl >= 0 ? "text-up" : "text-down"}`}>
                          {st.realized_pnl >= 0 ? "+" : ""}{fmt(st.realized_pnl, 2)}
                        </td>
                        <td className="td">{st.wins}/{st.losses}</td>
                        <td className={`td ${st.win_rate >= 0.5 ? "text-up" : "text-down"}`}>
                          {(st.win_rate * 100).toFixed(0)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="text-xs text-gray-400 mt-2">
                已实现盈亏 = 全部卖出(含轮动/止损/期末平仓)的税后净盈亏; 未平仓部分不计入。
              </div>
            </div>
          )}

          {/* 买卖点K线图(多标的切换) */}
          <TradeKlineCard runId={metrics.run_id} trades={metrics.trade_details || []}
            nameMap={Object.fromEntries(
              [...new Set((metrics.trade_details || []).map((t) => t.symbol))]
                .map((s) => [s, displayName(s)])
            )} />

          {/* 一键把回测交易过的标的加入监控/自选池(修复) */}
          {(() => {
            const syms = [...new Set((metrics.trade_details || []).map((t) => t.symbol))];
            if (!syms.length) return null;
            return (
              <div className="card flex flex-wrap items-center gap-3">
                <span className="text-sm text-gray-600">把回测交易过的标的一键加入监控:</span>
                <div className="flex flex-wrap gap-1.5">
                  {syms.map((s) => (
                    <span key={s} className="badge bg-gray-100 text-gray-600">{s}</span>
                  ))}
                </div>
                <button className="btn-primary ml-auto" disabled={addWatch.isPending}
                  title="把本次回测交易过的标的加入监控列表(回测→监控, 与'导入监控列表'方向相反)"
                  onClick={() => addWatch.mutate(syms)}>
                  <ListPlus size={14} className="inline mr-1" />
                  {addWatch.isPending ? "添加中..." : `回测标的→监控(${syms.length}只)`}
                </button>
              </div>
            );
          })()}

          <div className="card">
            <div className="card-title">交易明细 ({(metrics.trade_details || []).length}) {metrics.report_path && <button className="text-xs text-brand-600 underline ml-2" onClick={() => downloadReport(metrics.report_path.split("/").pop())}>查看报告</button>}
              <span className="ml-auto flex gap-2">
                <button className="btn-ghost text-xs" onClick={() => downloadJson(`backtest_${metrics.run_id}.json`, metrics)}>
                  <Download size={13} className="inline mr-1" />导出JSON
                </button>
                <button className="btn-ghost text-xs" onClick={() => downloadCsv(`backtest_${metrics.run_id}_trades.csv`, [
                  ["日期", "方向", "标的", "名称", "数量", "价格", "手续费", "滑点", "盈亏"],
                  ...(metrics.trade_details || []).map((t) => [String(t.date).slice(0, 10), t.side, t.symbol, t.name ?? "", t.qty, t.price, t.fee, t.slippage_cost ?? "", t.pnl ?? ""]),
                ])}>
                  <Download size={13} className="inline mr-1" />导出交易CSV
                </button>
              </span>
            </div>
            <div className="max-h-72 overflow-y-auto overflow-x-auto">
              <table className="w-full min-w-[700px]">
                <thead><tr><th className="th">日期</th><th className="th">方向</th><th className="th">标的</th><th className="th">名称</th>
                  <th className="th">数量</th><th className="th">价格</th><th className="th">手续费</th><th className="th">滑点</th><th className="th">盈亏</th></tr></thead>
                <tbody>
                  {(metrics.trade_details || []).map((t, i) => (
                    <tr key={i}>
                      <td className="td text-gray-500">{String(t.date).slice(0, 10)}</td>
                      <td className="td"><span className={`badge ${t.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>{t.side}</span></td>
                      <td className="td font-medium">{t.symbol}</td>
                      <td className="td text-gray-500">{displayName(t.symbol, t.name) || "-"}</td>
                      <td className="td">{t.qty}</td>
                      <td className="td">{fmt(t.price)}</td>
                      <td className="td">{fmt(t.fee, 2)}</td>
                      <td className="td text-gray-400">{t.slippage_cost ? fmt(t.slippage_cost, 2) : "-"}</td>
                      <td className={`td font-medium ${(t.pnl || 0) >= 0 ? "text-up" : "text-down"}`}>{t.pnl ? fmt(t.pnl, 2) : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

    </div>
  );
}



