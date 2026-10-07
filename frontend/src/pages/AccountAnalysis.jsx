import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from "recharts";
import { FileText, Download } from "lucide-react";
import { api, downloadReport } from "../api/client";
import { fmt, fmtPct, Empty, Spin, ErrorBox, errMsg, pnlColor } from "../components/Common";
import { toastOk, toastErr } from "../components/Toast";

const PERIODS = [
  ["day", "今日"],
  ["week", "本周"],
  ["month", "本月"],
  ["year", "今年"],
];

const TABS = [
  ["overview", "净值概览"],
  ["daily", "每日盈亏"],
  ["symbols", "标的盈亏"],
  ["trades", "成交/报告"],
];

/** 账户分析: 区间统计 + 每日盈亏 + 历史标的盈亏(含已清仓) + 基准对比 */
export default function AccountAnalysis() {
  const qc = useQueryClient();
  const [period, setPeriod] = useState("month");
  const [tab, setTab] = useState("overview");
  const { data, isFetching, isError, error, refetch } = useQuery({
    queryKey: ["analysis", period],
    queryFn: () => api.get("/api/account/analysis", { period }),
    refetchInterval: 30000,
  });
  const { data: reports } = useQuery({
    queryKey: ["reports"],
    queryFn: () => api.get("/api/reports/list", { limit: 30 }),
    refetchInterval: 60000,
  });
  const gen = useMutation({
    mutationFn: (report_type) => api.post(`/api/reports/generate?report_type=${report_type}`),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["reports"] });
      qc.invalidateQueries({ queryKey: ["analysis"] });
      toastOk(`报告已生成: ${r?.file_name || ""}`);
    },
    onError: (e) => toastErr("报告生成失败: " + errMsg(e)),
  });

  const st = data?.stats || {};
  const eq = (data?.equity_curve || []).map((p) => p.total_asset);
  const eq0 = eq.length ? eq[0] : 1;
  const chartData = (data?.equity_curve || []).map((p, i) => ({
    t: p.time,
    净值: Math.round((p.total_asset / eq0) * 10000) / 10000,
    基准: data?.benchmark_curve?.[i]?.value ?? null,
  }));

  const cards = [
    { label: "区间收益", value: st.period_return != null ? `${(st.period_return * 100).toFixed(2)}%` : "-",
      color: pnlColor(st.period_return) },
    { label: "沪深300", value: st.benchmark_return != null ? `${(st.benchmark_return * 100).toFixed(2)}%` : "-",
      color: pnlColor(st.benchmark_return) },
    { label: "超额收益", value: st.excess_return != null ? `${(st.excess_return * 100).toFixed(2)}%` : "-",
      color: pnlColor(st.excess_return) },
    { label: "最大回撤", value: st.max_drawdown != null ? `${(st.max_drawdown * 100).toFixed(2)}%` : "-", color: "text-down" },
    { label: "已实现盈亏", value: st.realized_pnl != null ? `${st.realized_pnl >= 0 ? "+" : ""}${fmt(st.realized_pnl, 2)}` : "-",
      color: pnlColor(st.realized_pnl) },
    { label: "成交笔数", value: st.trade_count ?? "-", color: "" },
    { label: "胜率", value: st.win_rate != null ? `${(st.win_rate * 100).toFixed(0)}%` : "-", color: "" },
    { label: "手续费", value: st.fee_total != null ? `¥${fmt(st.fee_total, 2)}` : "-", color: "" },
  ];

  const daily = data?.daily_pnl || [];
  const historySymbols = data?.history_symbols || [];

  return (
    <div className="p-3 md:p-5 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">账户分析</h1>
        <div className="flex gap-1">
          {PERIODS.map(([v, label]) => (
            <button key={v} className={`badge ${period === v ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
              onClick={() => setPeriod(v)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {isError ? <ErrorBox error={error} text="账户分析加载失败" onRetry={refetch} />
        : isFetching && !data ? <Spin /> : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-2">
            {cards.map(({ label, value, color }) => (
              <div key={label} className="card text-center py-2.5">
                <div className={`text-base font-bold ${color}`}>{value}</div>
                <div className="text-xs text-gray-500 mt-0.5">{label}</div>
              </div>
            ))}
          </div>

          {/* 页签: 缩短页面长度, 手机端一屏可切换 */}
          <div className="flex gap-1.5 overflow-x-auto pb-0.5">
            {TABS.map(([v, label]) => (
              <button key={v}
                className={`btn text-xs whitespace-nowrap ${tab === v ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
                onClick={() => setTab(v)}>
                {label}
                {v === "daily" && daily.length ? ` (${daily.length})` : ""}
                {v === "symbols" && historySymbols.length ? ` (${historySymbols.length})` : ""}
              </button>
            ))}
          </div>

          {tab === "overview" && (
            <div className="card">
              <div className="card-title">净值 vs 沪深300 (区间起点归一为1)</div>
              {chartData.length >= 2 ? (
                <ResponsiveContainer width="100%" height={260}>
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                    <XAxis dataKey="t" fontSize={9} tickFormatter={(v) => String(v).slice(5, 16)} minTickGap={40} />
                    <YAxis fontSize={10} domain={["auto", "auto"]} tickFormatter={(v) => (v * 100).toFixed(0) + "%"} />
                    <Tooltip formatter={(v) => [(v * 100).toFixed(2) + "%", ""]} />
                    <Legend />
                    <Line type="monotone" dataKey="净值" stroke="#1c3a5e" dot={false} strokeWidth={1.6} />
                    <Line type="monotone" dataKey="基准" stroke="#f59f00" dot={false} strokeWidth={1.2} />
                  </LineChart>
                </ResponsiveContainer>
              ) : <Empty text="区间快照不足(定时快照每30分钟记录一次)" />}
            </div>
          )}

          {tab === "daily" && (
            <div className="card">
              <div className="card-title flex items-center justify-between">
                <span>每日盈亏 ({daily.length} 天)</span>
                <span className="text-[11px] text-gray-400 font-normal">
                  当日盈亏 = 当日总资产 - 上一交易日总资产(账户无出入金)
                </span>
              </div>
              {daily.length ? (
                <div className="overflow-x-auto max-h-[60vh] overflow-y-auto">
                  <table className="w-full min-w-[560px]">
                    <thead><tr>
                      <th className="th">日期</th><th className="th">总资产</th>
                      <th className="th">当日盈亏</th><th className="th">涨跌%</th>
                      <th className="th">成交</th><th className="th">已实现盈亏</th>
                      <th className="th">手续费</th>
                    </tr></thead>
                    <tbody>
                      {[...daily].reverse().map((d) => (
                        <tr key={d.date}>
                          <td className="td font-medium">{d.date}</td>
                          <td className="td">¥{fmt(d.total_asset, 2)}</td>
                          <td className={`td font-semibold ${pnlColor(d.day_pnl)}`}>
                            {d.day_pnl == null ? "-" : `${d.day_pnl > 0 ? "+" : ""}${fmt(d.day_pnl, 2)}`}
                          </td>
                          <td className={`td ${pnlColor(d.day_pnl_pct)}`}>
                            {d.day_pnl_pct == null ? "-" : `${d.day_pnl_pct > 0 ? "+" : ""}${fmtPct(d.day_pnl_pct * 100)}`}
                          </td>
                          <td className="td text-gray-500">{d.trade_count || 0} 笔</td>
                          <td className={`td ${pnlColor(d.realized_pnl)}`}>
                            {d.realized_pnl ? `${d.realized_pnl > 0 ? "+" : ""}${fmt(d.realized_pnl, 2)}` : "-"}
                          </td>
                          <td className="td text-gray-500">{fmt(d.fee, 2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <Empty text="暂无每日数据(快照每30分钟记录一次)" />}
            </div>
          )}

          {tab === "symbols" && (
            <div className="space-y-3">
              <div className="card">
                <div className="card-title flex items-center justify-between">
                  <span>历史标的盈亏 (全部交易, 含已清仓) ({historySymbols.length})</span>
                  <span className="text-[11px] text-gray-400 font-normal">
                    累计口径: 从账户开始至今的已实现盈亏
                  </span>
                </div>
                {historySymbols.length ? (
                  <div className="overflow-x-auto max-h-[60vh] overflow-y-auto">
                    <table className="w-full min-w-[720px]">
                      <thead><tr>
                        <th className="th">标的</th><th className="th">名称</th>
                        <th className="th">买/卖(次)</th><th className="th">已实现盈亏</th>
                        <th className="th">胜/负</th><th className="th">胜率</th>
                        <th className="th">手续费</th><th className="th">状态</th>
                        <th className="th">最近交易</th>
                      </tr></thead>
                      <tbody>
                        {historySymbols.map((s) => (
                          <tr key={s.symbol}>
                            <td className="td font-medium">{s.symbol}</td>
                            <td className="td text-gray-500">{s.name || "-"}</td>
                            <td className="td">{s.buy_count}/{s.sell_count}</td>
                            <td className={`td font-semibold ${pnlColor(s.realized_pnl)}`}>
                              {s.realized_pnl > 0 ? "+" : ""}{fmt(s.realized_pnl, 2)}
                            </td>
                            <td className="td text-gray-500">{s.wins}/{s.losses}</td>
                            <td className="td">{s.win_rate != null ? `${(s.win_rate * 100).toFixed(0)}%` : "-"}</td>
                            <td className="td text-gray-500">{fmt(s.fee, 2)}</td>
                            <td className="td">
                              {s.position ? (
                                <span className="badge bg-blue-50 text-blue-700">
                                  持有 {s.position.total_qty}份
                                  <span className={`ml-1 ${pnlColor(s.position.pnl)}`}>
                                    {s.position.pnl > 0 ? "+" : ""}{fmt(s.position.pnl, 2)}
                                  </span>
                                </span>
                              ) : s.closed ? (
                                <span className="badge bg-purple-50 text-purple-600">已清仓</span>
                              ) : <span className="text-gray-400 text-xs">-</span>}
                            </td>
                            <td className="td text-xs text-gray-400">{(s.last_trade || "").slice(0, 16)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : <Empty text="暂无历史成交" />}
              </div>

              <div className="card">
                <div className="card-title">区间标的统计 ({(data?.symbol_stats || []).length})</div>
                {(data?.symbol_stats || []).length ? (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[760px]">
                      <thead><tr>
                        <th className="th">标的</th><th className="th">名称</th>
                        <th className="th">买卖(次)</th><th className="th">已实现盈亏</th>
                        <th className="th">手续费</th><th className="th">当前持仓</th>
                        <th className="th">区间涨跌幅</th><th className="th">权重</th>
                      </tr></thead>
                      <tbody>
                        {(data?.symbol_stats || []).map((s) => (
                          <tr key={s.symbol}>
                            <td className="td font-medium">{s.symbol}</td>
                            <td className="td text-gray-500">{s.name || "-"}</td>
                            <td className="td">{s.buy_count}/{s.sell_count}</td>
                            <td className={`td font-semibold ${pnlColor(s.realized_pnl)}`}>
                              {s.realized_pnl > 0 ? "+" : ""}{fmt(s.realized_pnl, 2)}
                            </td>
                            <td className="td text-gray-500">{fmt(s.fee, 2)}</td>
                            <td className="td">
                              {s.position
                                ? <span>{s.position.total_qty}份 <span className={`text-xs ${pnlColor(s.position.pnl)}`}>{s.position.pnl > 0 ? "+" : ""}{fmt(s.position.pnl, 2)}</span></span>
                                : <span className="badge bg-purple-50 text-purple-600">已清仓</span>}
                            </td>
                            <td className={`td ${pnlColor(s.price_return)}`}>
                              {s.price_return != null ? `${(s.price_return * 100).toFixed(2)}%` : "-"}
                            </td>
                            <td className="td text-gray-500">{s.weight != null ? `${(s.weight * 100).toFixed(1)}%` : "-"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : <Empty text="区间内无成交" />}
              </div>
            </div>
          )}

          {tab === "trades" && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="card">
                <div className="card-title">区间成交 ({(data?.trades || []).length})</div>
                {(data?.trades || []).length ? (
                  <div className="max-h-80 overflow-y-auto">
                    <table className="w-full min-w-[560px]">
                      <thead><tr>
                        <th className="th">时间</th><th className="th">代码</th><th className="th">名称</th>
                        <th className="th">方向</th><th className="th">价格</th><th className="th">数量</th>
                        <th className="th">盈亏</th>
                      </tr></thead>
                      <tbody>
                        {[...(data?.trades || [])].reverse().map((t, i) => (
                          <tr key={i}>
                            <td className="td text-gray-500">{(t.trade_time || "").slice(5, 16)}</td>
                            <td className="td font-medium">{t.symbol}</td>
                            <td className="td text-gray-500">{t.name || "-"}</td>
                            <td className="td"><span className={`badge ${t.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>{t.side === "BUY" ? "买入" : "卖出"}</span></td>
                            <td className="td">{fmt(t.price)}</td>
                            <td className="td">{t.qty}</td>
                            <td className={`td font-semibold ${t.pnl == null ? "text-gray-400" : pnlColor(t.pnl)}`}>
                              {t.pnl == null ? "-" : `${t.pnl > 0 ? "+" : ""}${fmt(t.pnl, 2)}`}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : <Empty text="区间内无成交" />}
              </div>

              <div className="card">
                <div className="card-title flex items-center justify-between flex-wrap gap-1">
                  <span><FileText size={14} className="inline mr-1" />报告中心</span>
                  <span className="flex gap-1 flex-wrap">
                    {[["weekly", "周报"], ["monthly", "月报"], ["annual", "年报"], ["daily", "日报"]].map(([t, label]) => (
                      <button key={t} className="btn-ghost text-xs" disabled={gen.isPending}
                        onClick={() => gen.mutate(t)}>
                        生成{label}
                      </button>
                    ))}
                  </span>
                </div>
                <div className="space-y-1.5 max-h-80 overflow-y-auto">
                  {reports?.length ? reports.map((r) => (
                    <div key={r.report_id} className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-gray-100 hover:bg-gray-50">
                      <span className={`badge ${r.type === "daily" ? "bg-blue-50 text-blue-700" : r.type === "weekly" ? "bg-green-50 text-green-700" : r.type === "monthly" ? "bg-amber-50 text-amber-700" : r.type === "year" || r.type === "annual" ? "bg-purple-50 text-purple-700" : "bg-gray-100 text-gray-600"}`}>
                        {r.type === "daily" ? "日报" : r.type === "weekly" ? "周报" : r.type === "monthly" ? "月报" : (r.type === "year" || r.type === "annual") ? "年报" : r.type}
                      </span>
                      <span className="text-sm text-gray-600 truncate flex-1">{r.title}</span>
                      <span className="text-[10px] text-gray-400 shrink-0">{r.created_at}</span>
                      <button className="btn-ghost text-xs shrink-0" onClick={() => downloadReport(r.file_name)}>
                        <Download size={12} className="inline mr-0.5" />下载
                      </button>
                    </div>
                  )) : <Empty text="暂无报告(日报17:00自动生成, 或点击上方按钮生成)" />}
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
