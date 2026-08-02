import { useState, useMemo } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  AreaChart, Area, BarChart, Bar, Legend,
} from "recharts";
import { Play, History, Download } from "lucide-react";
import { api, poll } from "../api/client";
import BacktestKline from "../components/BacktestKline";
import { SystemBar, fmt, Empty } from "../components/Common";

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

const HOT_ETFS = ["510300", "159915", "588000", "512100", "159949", "513100", "512690", "515880", "512170", "588050"];
function MetricCard({ label, value, color = "" }) {
  return (
    <div className="card text-center">
      <div className={`text-lg font-bold ${color}`}>{value}</div>
      <div className="text-xs text-gray-500 mt-0.5">{label}</div>
    </div>
  );
}

/** 买卖点K线图卡片: 按回测交易过的标的分tab切换, 显示买卖点+成本/止损包络 */
function TradeKlineCard({ runId, trades }) {
  const symbols = [...new Set((trades || []).map((t) => t.symbol))];
  const [cur, setCur] = useState(symbols[0] || "");
  const { data } = useQuery({
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
          const name = (trades || []).find((t) => t.symbol === s)?.name || "";
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
        <BacktestKline candles={data.candles} marks={data.marks || []} roundTrips={data.round_trips || []} />
      ) : (
        <Empty text="加载K线中(先执行 fetch-daily)" />
      )}
    </div>
  );
}

/** 回测中心: 表单 → 异步任务轮询 → 指标/图表/明细/历史 */
export default function BacktestCenter() {
  const [form, setForm] = useState({
    symbols: [], start: "2026-04-01", end: "2026-07-31",
    initial_cash: 20000, mode: "daily", use_agents: false, name: "",
    params: { top_n: 3, mom_window: 20, min_amount: 30000000, max_vol: 0.5,
              target_weight: 0.2, rebalance_threshold: 0.15, max_total_position: 0.9,
              stop_loss_pct: 0.08, market_filter: true, min_hold_days: 3,
              max_buy_momentum: 0.3 },
  });
  const [customCode, setCustomCode] = useState("");
  const [running, setRunning] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);
  const setParam = (k, v) => setForm({ ...form, params: { ...form.params, [k]: v } });
  const clearSymbols = () => setForm({ ...form, symbols: [] });

  // 标的池: 来自监控列表(代码+中文名) + 常用ETF + 自定义添加
  const { data: watch } = useQuery({ queryKey: ["watchlist"], queryFn: () => api.get("/api/watchlist"), refetchInterval: 60000 });
  const pool = useMemo(() => {
    const seen = new Set();
    const list = [];
    for (const w of watch?.items || []) {
      if (w.enabled && !seen.has(w.symbol)) { seen.add(w.symbol); list.push({ symbol: w.symbol, name: w.name }); }
    }
    for (const s of HOT_ETFS) {
      if (!seen.has(s)) { seen.add(s); list.push({ symbol: s, name: "" }); }
    }
    return list;
  }, [watch]);

  const addCustom = () => {
    const code = customCode.trim().toUpperCase();
    if (!/^\d{6}$/.test(code)) return;
    if (!form.symbols.includes(code)) setForm({ ...form, symbols: [...form.symbols, code] });
    setCustomCode("");
  };

  const { data: history } = useQuery({ queryKey: ["btlist"], queryFn: () => api.get("/api/backtest/list?limit=10"), refetchInterval: running ? 5000 : false });

  const submit = useMutation({
    mutationFn: async () => {
      setError(null); setMetrics(null); setRunning({ status: "PENDING", progress: "排队中..." });
      const { run_id } = await api.post("/api/backtest/submit", form);
      setRunning({ run_id, status: "PENDING", progress: "排队中..." });
      await poll(`/api/backtest/${run_id}`, (d) => setRunning({ run_id, status: d.status, progress: d.progress }), { interval: 2000 });
      const final = await api.get(`/api/backtest/${run_id}`);
      setMetrics(final.metrics);
      setRunning(null);
      return final;
    },
    onError: (e) => { setRunning(null); setError(e.response?.data?.detail || e.message); },
  });

  const eqData = (metrics?.equity_curve || []).map((v, i) => ({
    d: metrics?.dates?.[i] || i, 净值: v, 基准: metrics?.benchmark?.benchmark_return != null ? (metrics.benchmark_curve?.[i] || v) : null,
  }));
  const ddData = (metrics?.drawdown_curve || []).map((v, i) => ({ d: metrics?.dates?.[i] || i, 回撤: (v * 100).toFixed(2) }));
  const posData = (metrics?.position_curve || []).map((v, i) => ({
    d: metrics?.dates?.[i] || i,
    p: metrics?.params?.initial_cash ? Math.min(1, v / metrics.params.initial_cash) : 0,
  }));
  const mData = Object.entries(metrics?.monthly_returns || {}).map(([k, v]) => ({ month: k, ret: (v * 100).toFixed(2) }));

  const toggleSym = (s) => {
    const cur = form.symbols;
    setForm({ ...form, symbols: cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s] });
  };

  const loadHistory = async (runId) => {
    setMetrics(null); setError(null);
    try {
      const r = await api.get(`/api/backtest/${runId}`);
      if (r.status === "DONE" && r.metrics) setMetrics(r.metrics);
      else setError(`该回测无结果(${r.status || "数据缺失"}), 可能是历史残留记录`);
    } catch (e) {
      setError("该回测记录不存在或已被清理: " + (e.response?.data?.detail || e.message));
    }
  };

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">回测中心</h1>
        <SystemBar />
      </div>

      {/* 表单 */}
      <div className="card grid grid-cols-1 md:grid-cols-3 gap-4">
        <div>
          <div className="text-xs text-gray-500 mb-1.5">标的池(来自监控列表+常用, 代码/名称)</div>
          <div className="flex flex-wrap gap-1.5 max-h-28 overflow-y-auto mb-2">
            {pool.map(({ symbol, name }) => (
              <button key={symbol} onClick={() => toggleSym(symbol)}
                className={`badge ${form.symbols.includes(symbol) ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
                title={name || symbol}>
                {symbol} {name && <span className="opacity-70">·{name}</span>}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <input className="input w-28" placeholder="自定义代码" value={customCode}
              onChange={(e) => setCustomCode(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addCustom()} />
            <button type="button" className="btn-ghost" onClick={addCustom}>添加</button>
            <button type="button" className="btn-ghost" onClick={clearSymbols} disabled={!form.symbols.length}>清空</button>
            <span className="text-[11px] text-gray-400 self-center">已选 {form.symbols.length} 只</span>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
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
              <option value="minute">分钟回测(5m)</option>
            </select>
          </label>
        </div>
        <div className="flex flex-col justify-between gap-2">
          {/* 策略参数 */}
          <div>
            <div className="text-xs text-gray-500 mb-1.5">轮动策略参数(修改后立即生效)</div>
            <div className="grid grid-cols-3 gap-2">
              {[
                ["top_n", "持有数量", "number"],
                ["mom_window", "动量窗口", "number"],
                ["target_weight", "单标的目标仓位", "number"],
                ["max_total_position", "总仓位上限", "number"],
                ["rebalance_threshold", "再平衡阈值", "number"],
                ["max_vol", "波动率上限", "number"],
                ["min_amount", "成交额下限(万)", "number"],
                ["stop_loss_pct", "止损线(%)", "number"],
                ["min_hold_days", "最小持仓天数", "number"],
              ].map(([k, label, type]) => (
                <label key={k} className="text-[11px] text-gray-500">
                  {label}
                  <input type={type} step={k.includes("weight") || k.includes("vol") || k.includes("threshold") || k.includes("position") ? 0.01 : 1}
                    className="input w-full mt-0.5 text-xs"
                    value={k === "stop_loss_pct" ? ((form.params[k] ?? 0.1) * 100) : (form.params[k] === 30000000 ? form.params[k] / 10000 : form.params[k])}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      if (k === "stop_loss_pct") setParam(k, isNaN(v) ? 0 : v / 100);
                      else setParam(k, k === "min_amount" ? (isNaN(v) ? 0 : v * 10000) : (isNaN(v) ? 0 : v));
                    }} />
                </label>
              ))}
              <label className="text-[11px] text-gray-500 flex items-center gap-1.5 pt-4">
                <input type="checkbox" checked={form.params.market_filter}
                  onChange={(e) => setParam("market_filter", e.target.checked)} />
                市场风险过滤(熊市空仓)
              </label>
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-600">
            <input type="checkbox" checked={form.use_agents}
              onChange={(e) => setForm({ ...form, use_agents: e.target.checked })} />
            关键节点调用 Agent(慢, 消耗token)
          </label>
          <input className="input w-full" placeholder="回测名称(可选)" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <button className="btn-primary w-full" disabled={submit.isPending || !form.symbols.length} onClick={() => submit.mutate()}>
            <Play size={14} className="inline mr-1" />{submit.isPending ? "提交中..." : "开始回测"}
          </button>
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
          {/* 参数摘要 + 提示 */}
          <div className="card">
            <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
              <span className="font-semibold text-gray-600">参数:</span>
              <span className="badge bg-gray-100">标的 {form.symbols.length}只</span>
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
            <MetricCard label="交易次数" value={metrics.trade_count} />
            <MetricCard label="超额收益" value={(metrics.benchmark?.excess_return * 100)?.toFixed(2) + "%"} color="text-brand-600" />
          </div>

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
                  {eqData[0]?.基准 && <Line type="monotone" dataKey="基准" stroke="#f59f00" dot={false} strokeWidth={1.2} />}
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

          {/* 买卖点K线图(多标的切换) */}
          <TradeKlineCard runId={metrics.run_id} trades={metrics.trade_details || []} />

          <div className="card">
            <div className="card-title">交易明细 ({(metrics.trade_details || []).length}) {metrics.report_path && <a className="text-xs text-brand-600 underline ml-2" href={`/reports/${metrics.report_path.split("/").pop()}`} target="_blank">查看报告</a>}
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
            <div className="max-h-72 overflow-y-auto">
              <table className="w-full">
                <thead><tr><th className="th">日期</th><th className="th">方向</th><th className="th">标的</th><th className="th">名称</th>
                  <th className="th">数量</th><th className="th">价格</th><th className="th">手续费</th><th className="th">滑点</th><th className="th">盈亏</th></tr></thead>
                <tbody>
                  {(metrics.trade_details || []).map((t, i) => (
                    <tr key={i}>
                      <td className="td text-gray-500">{String(t.date).slice(0, 10)}</td>
                      <td className="td"><span className={`badge ${t.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>{t.side}</span></td>
                      <td className="td font-medium">{t.symbol}</td>
                      <td className="td text-gray-500">{t.name || "-"}</td>
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

      {/* 历史 */}
      <div className="card">
        <div className="card-title"><History size={14} />历史回测</div>
        <div className="space-y-1.5 max-h-64 overflow-y-auto">
          {history?.length ? history.map((h) => (
            <button key={h.run_id} className="w-full flex items-center gap-3 px-3 py-2 rounded-lg border border-gray-100 hover:bg-gray-50 text-left"
              onClick={() => loadHistory(h.run_id)}>
              <span className="text-sm font-medium">{h.name || h.run_id}</span>
              <span className="text-xs text-gray-500">{h.start} ~ {h.end} · {h.mode}</span>
              <span className={`badge ml-auto ${h.status === "DONE" ? "bg-green-50 text-green-600" : h.status === "FAILED" ? "bg-red-50 text-red-600" : "bg-gray-100 text-gray-500"}`}>{h.status}</span>
              <span className="text-[10px] text-gray-400">{h.created_at}</span>
            </button>
          )) : <Empty text="暂无历史回测" />}
        </div>
      </div>
    </div>
  );
}



