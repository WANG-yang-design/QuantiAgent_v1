import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, AreaChart, Area,
} from "recharts";
import { Pause, Play, XCircle, Wallet, TrendingUp, TrendingDown, PiggyBank, Radar } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, fmt, fmtWan, Empty, Spin } from "../components/Common";

export default function Dashboard() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const { data: acc } = useQuery({ queryKey: ["account"], queryFn: () => api.get("/api/account"), refetchInterval: 15000 });
  const { data: equity } = useQuery({ queryKey: ["equity"], queryFn: () => api.get("/api/equity?limit=200") });
  const { data: orders } = useQuery({ queryKey: ["orders"], queryFn: () => api.get("/api/orders?limit=10"), refetchInterval: 15000 });
  const { data: mode } = useQuery({ queryKey: ["sysmode"], queryFn: () => api.get("/api/system/mode"), refetchInterval: 15000 });

  const pause = useMutation({ mutationFn: () => api.post("/api/emergency/pause?reason=dashboard"), onSuccess: () => qc.invalidateQueries() });
  const resume = useMutation({ mutationFn: () => api.post("/api/emergency/resume"), onSuccess: () => qc.invalidateQueries() });
  const cancelAll = useMutation({ mutationFn: () => api.post("/api/emergency/cancel_all"), onSuccess: () => qc.invalidateQueries() });

  const cards = [
    { label: "总资产", value: fmt(acc?.total_asset, 2), icon: Wallet, color: "text-brand-600" },
    { label: "可用资金", value: fmt(acc?.cash, 2), icon: PiggyBank, color: "text-cyan-600" },
    { label: "持仓市值", value: fmt(acc?.market_value, 2), icon: TrendingUp, color: "text-green-600" },
    { label: "总盈亏", value: fmt(acc?.total_pnl, 2), icon: TrendingDown, color: (acc?.total_pnl || 0) >= 0 ? "text-up" : "text-down" },
  ];
  const eqData = (equity || []).map((p) => ({ t: p.time.slice(5, 16), v: p.total_asset }));

  // 大盘指数 + 牛熊诊断
  const { data: idx } = useQuery({
    queryKey: ["indexes"], queryFn: () => api.get("/api/index/overview"), refetchInterval: 30000,
  });
  const { data: diag } = useQuery({
    queryKey: ["diagnosis"], queryFn: () => api.get("/api/market/diagnosis"), refetchInterval: 300000,
  });
  const diagColor = diag?.state === "risk_on" ? "text-up" : diag?.state === "risk_off" ? "text-down" : "text-amber-600";

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-brand-600">仪表盘</h1>
        <SystemBar />
      </div>

      {/* 大盘指数 + 牛熊诊断 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        {(idx?.indexes || []).map((ix) => (
          <div key={ix.code} className="card flex items-center justify-between">
            <div>
              <div className="text-xs text-gray-500">{ix.name}</div>
              <div className={`text-xl font-bold ${ix.color === "up" ? "text-up" : ix.color === "down" ? "text-down" : ""}`}>
                {ix.price}
              </div>
            </div>
            <div className={`text-sm font-semibold ${ix.color === "up" ? "text-up" : ix.color === "down" ? "text-down" : ""}`}>
              {ix.change_pct > 0 ? "+" : ""}{ix.change_pct}%
            </div>
          </div>
        ))}
        <div className={`card border-l-4 ${diag?.state === "risk_on" ? "border-red-500" : diag?.state === "risk_off" ? "border-green-500" : "border-amber-500"}`}>
          <div className="flex items-center gap-2 text-xs text-gray-500">
            <Radar size={14} className="text-brand-600" />
            市场诊断(Agent)
            <span className={`badge ${diag?.state === "risk_on" ? "bg-red-50 text-up" : diag?.state === "risk_off" ? "bg-green-50 text-down" : "bg-amber-50 text-amber-700"}`}>
              {diag?.label || "-"}
            </span>
          </div>
          <div className={`text-sm font-semibold mt-1 ${diagColor}`}>{diag?.advice || "计算中..."}</div>
          <div className="text-[10px] text-gray-400 mt-1">依据上证/沪深300/中证500的20日动量与均线 · {diag?.time}</div>
        </div>
      </div>

      {/* 指标卡片 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {cards.map(({ label, value, icon: Icon, color }) => (
          <div key={label} className="card">
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">{label}</span>
              <Icon size={16} className={color} />
            </div>
            <div className={`text-xl font-bold mt-1 ${label === "总盈亏" ? "" : ""}`}>¥{value}</div>
            {label === "总盈亏" && (
              <div className={`text-xs ${(acc?.total_pnl || 0) >= 0 ? "text-up" : "text-down"}`}>
                {(acc?.total_return || 0) >= 0 ? "+" : ""}{(acc?.total_return * 100)?.toFixed(2)}%
              </div>
            )}
          </div>
        ))}
      </div>

      {/* 紧急按钮 + 今日限额 */}
      <div className="card flex flex-wrap items-center gap-3">
        <span className="text-sm font-semibold text-gray-600">紧急控制:</span>
        <button className="btn-danger" disabled={pause.isPending} onClick={() => pause.mutate()}>
          <Pause size={14} className="inline mr-1" />一键暂停
        </button>
        <button className="btn-green" disabled={resume.isPending} onClick={() => resume.mutate()}>
          <Play size={14} className="inline mr-1" />恢复交易
        </button>
        <button className="btn-danger" disabled={cancelAll.isPending} onClick={() => cancelAll.mutate()}>
          <XCircle size={14} className="inline mr-1" />撤销全部委托
        </button>
        <span className="ml-auto text-xs text-gray-500">
          今日订单 {mode?.today?.order_count ?? "-"}/{mode?.today?.max_order_count ?? "-"} 笔 ·
          金额 {fmtWan(mode?.today?.order_amount)}/{fmtWan(mode?.today?.max_order_amount)}
        </span>
      </div>

      {/* 净值曲线 + 持仓 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card lg:col-span-2">
          <div className="card-title">账户净值曲线</div>
          {eqData.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={eqData}>
                <defs>
                  <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#1c3a5e" stopOpacity={0.25} />
                    <stop offset="100%" stopColor="#1c3a5e" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                <XAxis dataKey="t" fontSize={10} />
                <YAxis fontSize={10} domain={["auto", "auto"]} />
                <Tooltip />
                <Area type="monotone" dataKey="v" name="总资产" stroke="#1c3a5e" fill="url(#g)" />
              </AreaChart>
            </ResponsiveContainer>
          ) : <Empty text="尚无账户快照(收盘后生成)" />}
        </div>

        <div className="card">
          <div className="card-title">持仓 ({(acc?.positions || []).length})</div>
          {(acc?.positions || []).length ? (
            <div className="space-y-1.5">
              {(acc?.positions || []).map((p) => (
                <button key={p.symbol} className="w-full flex items-center justify-between px-3 py-2 rounded-lg hover:bg-gray-50 text-left"
                  onClick={() => nav(`/symbol/${p.symbol}`)}>
                  <span className="text-sm font-medium">{p.symbol} <span className="text-gray-400">{p.name}</span></span>
                  <span className={`text-sm font-semibold ${(p.pnl_pct || 0) >= 0 ? "text-up" : "text-down"}`}>
                    {fmt(p.total_qty, 0)}份 {(p.pnl_pct * 100)?.toFixed(2)}%
                  </span>
                </button>
              ))}
            </div>
          ) : <Empty text="暂无持仓" />}
        </div>
      </div>

      {/* 今日订单 */}
      <div className="card">
        <div className="card-title">最近订单</div>
        {orders?.length ? (
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">时间</th><th className="th">标的</th><th className="th">方向</th>
                <th className="th">价格</th><th className="th">数量</th><th className="th">状态</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.order_id}>
                  <td className="td text-gray-500">{(o.submit_time || "").slice(11, 19)}</td>
                  <td className="td font-medium">{o.symbol}</td>
                  <td className="td"><span className={`badge ${o.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>{o.side}</span></td>
                  <td className="td">{fmt(o.price)}</td>
                  <td className="td">{o.filled_qty}/{o.qty}</td>
                  <td className="td"><span className="badge bg-gray-100 text-gray-600">{o.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <Empty text="今日暂无订单" />}
      </div>
    </div>
  );
}
