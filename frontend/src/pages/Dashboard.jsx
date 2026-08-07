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

  const pause = useMutation({
    // 修复: 原实现把 window.confirm 放进 mutationFn, 取消时返回 resolved
    // Promise 仍触发 onSuccess 刷新 —— 取消确认移到调用点
    mutationFn: () => api.post("/api/emergency/pause?reason=dashboard"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sysmode"] }),
    onError: (e) => window.alert("暂停失败: " + (e.response?.data?.detail || e.message)),
  });
  const resume = useMutation({
    mutationFn: () => api.post("/api/emergency/resume"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sysmode"] }),
    onError: (e) => window.alert("恢复失败: " + (e.response?.data?.detail || e.message)),
  });
  const cancelAll = useMutation({
    mutationFn: () => api.post("/api/emergency/cancel_all"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["orders"] }); qc.invalidateQueries({ queryKey: ["sysmode"] }); },
    onError: (e) => window.alert("撤单失败: " + (e.response?.data?.detail || e.message)),
  });
  const doSnapshot = useMutation({
    mutationFn: () => api.post("/api/account/snapshot"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["equity"] }),
    onError: (e) => window.alert("快照失败: " + (e.response?.data?.detail || e.message)),
  });

  // 净值曲线: 快照不足时用 [初始资金, 当前资产] 兜底, 保证始终有曲线
  // (修复: 快照是定时任务(每30分钟)写的, 曲线末端停留在上次快照值,
  //  看起来像"不动"; 追加一个"现在"实时点让曲线始终延伸到当前总资产)
  const rawEq = equity || [];
  const hasSnapshot = rawEq.length >= 2;
  const liveV = Math.round(acc?.total_asset || 0);
  const lastV = rawEq.length ? rawEq[rawEq.length - 1].total_asset : null;
  const eqData = hasSnapshot
    ? [
        ...rawEq.map((p) => ({ t: (p.time || "").slice(5, 16), v: p.total_asset })),
        ...(lastV == null || Math.abs(lastV - liveV) > 0.01 ? [{ t: "现在", v: liveV }] : []),
      ]
    : [
        { t: "初始", v: Math.round((acc?.total_asset || 0) - (acc?.total_pnl || 0)) },
        { t: "现在", v: liveV },
      ];

  const cards = [
    { label: "总资产", value: fmt(acc?.total_asset, 2), icon: Wallet, color: "text-brand-600" },
    { label: "可用资金", value: fmt(acc?.cash, 2), icon: PiggyBank, color: "text-cyan-600" },
    { label: "持仓市值", value: fmt(acc?.market_value, 2), icon: TrendingUp, color: "text-green-600" },
    { label: "总盈亏", value: fmt(acc?.total_pnl, 2), icon: TrendingDown, color: (acc?.total_pnl || 0) >= 0 ? "text-up" : "text-down" },
  ];

  // 大盘指数 + 牛熊诊断
  const { data: idx } = useQuery({
    queryKey: ["indexes"], queryFn: () => api.get("/api/index/overview"), refetchInterval: 30000,
  });
  const { data: diag } = useQuery({
    queryKey: ["diagnosis"], queryFn: () => api.get("/api/market/diagnosis"), refetchInterval: 300000,
  });
  const diagColor = diag?.state === "risk_on" ? "text-up" : diag?.state === "risk_off" ? "text-down" : "text-amber-600";

  return (
    <div className="p-3 md:p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-brand-600">仪表盘</h1>
        <SystemBar />
      </div>

      {/* 大盘指数 + 牛熊诊断(点击指数卡片查看K线) */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        {(idx?.indexes || []).map((ix) => (
          <button key={ix.code} className="card flex items-center justify-between hover:shadow-md transition-shadow cursor-pointer text-left"
            onClick={() => nav(`/symbol/${ix.code.replace(/^sh|^sz/, "")}`)}
            title="点击查看指数K线">
            <div>
              <div className="text-xs text-gray-500">{ix.name}</div>
              <div className={`text-xl font-bold ${ix.color === "up" ? "text-up" : ix.color === "down" ? "text-down" : ""}`}>
                {ix.price}
              </div>
            </div>
            <div className={`text-sm font-semibold ${ix.color === "up" ? "text-up" : ix.color === "down" ? "text-down" : ""}`}>
              {ix.change_pct > 0 ? "+" : ""}{ix.change_pct}%
            </div>
          </button>
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

      {/* 指标卡片(点击跳转模拟盘/实盘页) */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {cards.map(({ label, value, icon: Icon, color }) => (
          <button key={label} className="card text-left hover:shadow-md transition-shadow cursor-pointer"
            onClick={() => nav("/paper-live")}>
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">{label}</span>
              <Icon size={16} className={color} />
            </div>
            <div className="text-xl font-bold mt-1">¥{value}</div>
            {label === "总盈亏" && (
              <div className={`text-xs ${(acc?.total_pnl || 0) >= 0 ? "text-up" : "text-down"}`}>
                {acc?.total_return != null ? `${(acc.total_return || 0) >= 0 ? "+" : ""}${(acc.total_return * 100).toFixed(2)}%` : "-"}
              </div>
            )}
          </button>
        ))}
      </div>

      {/* 紧急按钮 + 今日限额 */}
      <div className="card flex flex-wrap items-center gap-3">
        <span className="text-sm font-semibold text-gray-600">紧急控制:</span>
        <button className="btn-danger" disabled={pause.isPending} onClick={() => {
          if (window.confirm("确认暂停全部交易? 暂停后所有自动交易将被拦截。")) pause.mutate();
        }}>
          <Pause size={14} className="inline mr-1" />一键暂停
        </button>
        <button className="btn-green" disabled={resume.isPending} onClick={() => resume.mutate()}>
          <Play size={14} className="inline mr-1" />恢复交易
        </button>
        <button className="btn-danger" disabled={cancelAll.isPending} onClick={() => {
          if (window.confirm("确认撤销全部未成交委托? 该操作不可恢复。")) cancelAll.mutate();
        }}>
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
          <div className="card-title flex items-center justify-between">
            <span>账户净值曲线</span>
            <span className="flex items-center gap-2">
              {!hasSnapshot && <span className="text-[11px] text-gray-400 font-normal">快照不足, 显示当前资产</span>}
              <button className="btn-ghost text-xs" disabled={doSnapshot.isPending} onClick={() => doSnapshot.mutate()}>
                记录快照
              </button>
            </span>
          </div>
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
          ) : <Empty text="暂无资产数据" />}
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
          <div className="overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead>
              <tr>
                <th className="th">时间</th><th className="th">标的</th><th className="th">名称</th><th className="th">方向</th>
                <th className="th">价格</th><th className="th">数量</th>
                <th className="th">来源</th><th className="th">状态</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                // 订单来源(修复: 用户分不清自动卖单来自哪 — 风控巡检单 vs Agent决策单 vs 策略轮动)
                const src = {
                  risk_monitor: { label: "风控巡检", cls: "bg-amber-50 text-amber-700" },
                  rotation: { label: "策略轮动", cls: "bg-cyan-50 text-cyan-700" },
                }[o.source] || { label: "Agent决策", cls: "bg-blue-50 text-blue-700" };
                const st = {
                  FILLED: { label: "已成交", cls: "bg-green-50 text-green-600" },
                  PARTIALLY_FILLED: { label: "部分成交", cls: "bg-green-50 text-green-600" },
                  CANCELLED: { label: "已撤单", cls: "bg-gray-100 text-gray-500" },
                  SUBMITTED: { label: "已提交", cls: "bg-amber-50 text-amber-700" },
                  ACCEPTED: { label: "已受理", cls: "bg-amber-50 text-amber-700" },
                  REJECTED: { label: "已拒绝", cls: "bg-red-50 text-red-600" },
                  FAILED: { label: "失败", cls: "bg-red-50 text-red-600" },
                }[o.status] || { label: o.status, cls: "bg-gray-100 text-gray-600" };
                return (
                  <tr key={o.order_id}>
                    <td className="td text-gray-500">{(o.submit_time || "").slice(5, 19)}</td>
                    <td className="td font-medium">{o.symbol}</td>
                    <td className="td text-gray-500">{o.name || "-"}</td>
                    <td className="td"><span className={`badge ${o.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>{o.side === "BUY" ? "买入" : "卖出"}</span></td>
                    <td className="td">{fmt(o.price)}</td>
                    <td className="td">{o.filled_qty}/{o.qty}</td>
                    <td className="td"><span className={`badge ${src.cls}`}>{src.label}</span></td>
                    <td className="td"><span className={`badge ${st.cls}`}>{st.label}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        ) : <Empty text="今日暂无订单" />}
      </div>
    </div>
  );
}
