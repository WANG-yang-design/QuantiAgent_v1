import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend, AreaChart, Area,
} from "recharts";
import { FileText, Download } from "lucide-react";
import { api, getToken } from "../api/client";
import { SystemBar, fmt, Empty, Spin } from "../components/Common";

const PERIODS = [
  ["day", "今日"],
  ["week", "本周"],
  ["month", "本月"],
  ["year", "今年"],
];

/** 账户分析: 日报/周报/月报/年报数据 + 单标的统计 + 沪深300基准对比 */
export default function AccountAnalysis() {
  const qc = useQueryClient();
  const [period, setPeriod] = useState("month");
  const { data, isFetching } = useQuery({
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["reports"] }); qc.invalidateQueries({ queryKey: ["analysis"] }); },
    onError: (e) => window.alert("报告生成失败: " + (e.response?.data?.detail || e.message)),
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
      color: (st.period_return ?? 0) >= 0 ? "text-up" : "text-down" },
    { label: "沪深300", value: st.benchmark_return != null ? `${(st.benchmark_return * 100).toFixed(2)}%` : "-",
      color: (st.benchmark_return ?? 0) >= 0 ? "text-up" : "text-down" },
    { label: "超额收益", value: st.excess_return != null ? `${(st.excess_return * 100).toFixed(2)}%` : "-",
      color: (st.excess_return ?? 0) >= 0 ? "text-up" : "text-down" },
    { label: "最大回撤", value: st.max_drawdown != null ? `${(st.max_drawdown * 100).toFixed(2)}%` : "-", color: "text-down" },
    { label: "已实现盈亏", value: st.realized_pnl != null ? `${st.realized_pnl >= 0 ? "+" : ""}${fmt(st.realized_pnl, 2)}` : "-",
      color: (st.realized_pnl ?? 0) >= 0 ? "text-up" : "text-down" },
    { label: "成交笔数", value: st.trade_count ?? "-", color: "" },
    { label: "胜率", value: st.win_rate != null ? `${(st.win_rate * 100).toFixed(0)}%` : "-", color: "" },
    { label: "手续费", value: st.fee_total != null ? `¥${fmt(st.fee_total, 2)}` : "-", color: "" },
  ];

  return (
    <div className="p-3 md:p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">账户分析</h1>
        <div className="flex items-center gap-2">
          <div className="flex gap-1">
            {PERIODS.map(([v, label]) => (
              <button key={v} className={`badge ${period === v ? "bg-brand-600 text-white" : "bg-gray-100 text-gray-600"}`}
                onClick={() => setPeriod(v)}>
                {label}
              </button>
            ))}
          </div>
          <SystemBar />
        </div>
      </div>

      {isFetching && !data ? <div className="p-5"><Spin /></div> : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-2">
            {cards.map(({ label, value, color }) => (
              <div key={label} className="card text-center">
                <div className={`text-base font-bold ${color}`}>{value}</div>
                <div className="text-xs text-gray-500 mt-0.5">{label}</div>
              </div>
            ))}
          </div>

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
            ) : <Empty text="区间快照不足(定时快照每30分钟记录一次, 明天起曲线完整)" />}
          </div>

          <div className="card">
            <div className="card-title">单标的统计 ({(data?.symbol_stats || []).length})</div>
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
                        <td className={`td font-semibold ${s.realized_pnl >= 0 ? "text-up" : "text-down"}`}>
                          {s.realized_pnl >= 0 ? "+" : ""}{fmt(s.realized_pnl, 2)}
                        </td>
                        <td className="td text-gray-500">{fmt(s.fee, 2)}</td>
                        <td className="td">
                          {s.position
                            ? <span>{s.position.total_qty}份 <span className={`text-xs ${s.position.pnl >= 0 ? "text-up" : "text-down"}`}>{s.position.pnl >= 0 ? "+" : ""}{fmt(s.position.pnl, 2)}</span></span>
                            : <span className="badge bg-purple-50 text-purple-600">已清仓</span>}
                        </td>
                        <td className={`td ${(s.price_return ?? 0) >= 0 ? "text-up" : "text-down"}`}>
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

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="card">
              <div className="card-title">区间成交 ({(data?.trades || []).length})</div>
              {(data?.trades || []).length ? (
                <div className="max-h-72 overflow-y-auto">
                  <table className="w-full">
                    <thead><tr>
                      <th className="th">时间</th><th className="th">代码</th><th className="th">名称</th>
                      <th className="th">方向</th><th className="th">价格</th><th className="th">数量</th>
                      <th className="th">盈亏</th>
                    </tr></thead>
                    <tbody>
                      {(data?.trades || []).map((t, i) => (
                        <tr key={i}>
                          <td className="td text-gray-500">{(t.trade_time || "").slice(5, 16)}</td>
                          <td className="td font-medium">{t.symbol}</td>
                          <td className="td text-gray-500">{t.name || "-"}</td>
                          <td className="td"><span className={`badge ${t.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>{t.side === "BUY" ? "买入" : "卖出"}</span></td>
                          <td className="td">{fmt(t.price)}</td>
                          <td className="td">{t.qty}</td>
                          <td className={`td font-semibold ${t.pnl == null ? "text-gray-400" : t.pnl >= 0 ? "text-up" : "text-down"}`}>
                            {t.pnl == null ? "-" : `${t.pnl >= 0 ? "+" : ""}${fmt(t.pnl, 2)}`}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <Empty text="区间内无成交" />}
            </div>

            <div className="card">
              <div className="card-title flex items-center justify-between">
                <span><FileText size={14} className="inline mr-1" />报告中心</span>
                <span className="flex gap-1">
                  {[["weekly", "生成周报"], ["monthly", "生成月报"], ["annual", "生成年报"], ["daily", "生成日报"]].map(([t, label]) => (
                    <button key={t} className="btn-ghost text-xs" disabled={gen.isPending}
                      onClick={() => gen.mutate(t)}>
                      {label}
                    </button>
                  ))}
                </span>
              </div>
              <div className="space-y-1.5 max-h-72 overflow-y-auto">
                {reports?.length ? reports.map((r) => (
                  <div key={r.report_id} className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-gray-100 hover:bg-gray-50">
                    <span className={`badge ${r.type === "daily" ? "bg-blue-50 text-blue-700" : r.type === "weekly" ? "bg-green-50 text-green-700" : r.type === "monthly" ? "bg-amber-50 text-amber-700" : r.type === "year" || r.type === "annual" ? "bg-purple-50 text-purple-700" : "bg-gray-100 text-gray-600"}`}>
                      {r.type === "daily" ? "日报" : r.type === "weekly" ? "周报" : r.type === "monthly" ? "月报" : (r.type === "year" || r.type === "annual") ? "年报" : r.type}
                    </span>
                    <span className="text-sm text-gray-600 truncate flex-1">{r.title}</span>
                    <span className="text-[10px] text-gray-400 shrink-0">{r.created_at}</span>
                    <a className="btn-ghost text-xs shrink-0" href={`/api/reports/${r.file_name}?token=${getToken()}`} target="_blank" rel="noreferrer">
                      <Download size={12} className="inline mr-0.5" />下载
                    </a>
                  </div>
                )) : <Empty text="暂无报告(日报17:00自动生成, 或点击上方按钮生成)" />}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
