import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Pause, Play, XCircle, Check, X, Info } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, fmt, fmtWan, Empty, Spin } from "../components/Common";

/** 模拟盘/实盘: 运行模式状态 + 控制 + 限额 + 人工确认 */
export default function PaperLive() {
  const qc = useQueryClient();
  const { data: mode, isLoading } = useQuery({
    queryKey: ["sysmode"],
    queryFn: () => api.get("/api/system/mode"),
    refetchInterval: 10000,
  });
  const { data: equity } = useQuery({ queryKey: ["equity"], queryFn: () => api.get("/api/equity?limit=500") });

  const pause = useMutation({ mutationFn: () => api.post("/api/emergency/pause?reason=paper-live"), onSuccess: () => qc.invalidateQueries() });
  const resume = useMutation({ mutationFn: () => api.post("/api/emergency/resume"), onSuccess: () => qc.invalidateQueries() });
  const cancelAll = useMutation({ mutationFn: () => api.post("/api/emergency/cancel_all"), onSuccess: () => qc.invalidateQueries() });
  const decide = useMutation({
    mutationFn: ({ id, ok }) => api.post(`/api/confirmations/${id}/decide`, { approved: ok, note: "web" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sysmode"] }),
  });

  if (isLoading) return <div className="p-5"><Spin /></div>;
  const acc = mode?.account || {};
  const today = mode?.today || {};
  const orderPct = Math.min(100, (today.order_count / (today.max_order_count || 1)) * 100);
  const amountPct = Math.min(100, (today.order_amount / (today.max_order_amount || 1)) * 100);

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">模拟盘 / 实盘</h1>
        <SystemBar />
      </div>

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
          <div className="text-xs text-gray-400 mt-1">账户 {mode?.account ? "PA-001" : "-"}</div>
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
          <button className="btn-danger" disabled={pause.isPending} onClick={() => pause.mutate()}>
            <Pause size={14} className="inline mr-1" />一键暂停交易
          </button>
          <button className="btn-green" disabled={resume.isPending} onClick={() => resume.mutate()}>
            <Play size={14} className="inline mr-1" />恢复交易
          </button>
          <button className="btn-danger" disabled={cancelAll.isPending} onClick={() => cancelAll.mutate()}>
            <XCircle size={14} className="inline mr-1" />撤销全部未成交委托
          </button>
          <button className="btn-ghost opacity-50 cursor-not-allowed" title="QMT/PTrade 未接入, 接入后开放">
            切换实盘(未接入)
          </button>
          <button className="btn-ghost opacity-50 cursor-not-allowed" title="实盘接入后开放">
            只读模式(预留)
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

      {/* 账户 + 限额 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card">
          <div className="card-title">账户快照</div>
          <div className="space-y-1.5">
            {[["总资产", acc.total_asset, ""], ["可用资金", acc.cash, ""], ["持仓市值", acc.market_value, ""],
              ["当日盈亏", acc.day_pnl, (acc.day_pnl || 0) >= 0 ? "text-up" : "text-down"],
              ["累计盈亏", acc.total_pnl, (acc.total_pnl || 0) >= 0 ? "text-up" : "text-down"]].map(([k, v, c]) => (
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

      {/* 人工确认队列 */}
      <div className="card">
        <div className="card-title">人工确认队列 ({(mode?.confirmations || []).length})</div>
        {(mode?.confirmations || []).length ? (
          <div className="space-y-2">
            {(mode?.confirmations || []).map((c) => (
              <div key={c.confirm_id} className="flex items-center gap-3 border border-amber-200 bg-amber-50/50 rounded-lg px-3 py-2">
                <span className={`badge ${c.risk_level === "HIGH" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-700"}`}>{c.risk_level}</span>
                <span className="text-sm font-medium">{c.symbol} {c.action}</span>
                <span className="text-sm text-gray-600">¥{fmt(c.amount, 0)}</span>
                <span className="flex-1 text-xs text-gray-500 truncate">{c.reason}</span>
                <span className="text-[10px] text-gray-400">{c.created_at}</span>
                <button className="btn-green" onClick={() => decide.mutate({ id: c.confirm_id, ok: true })}>
                  <Check size={14} className="inline mr-1" />批准
                </button>
                <button className="btn-danger" onClick={() => decide.mutate({ id: c.confirm_id, ok: false })}>
                  <X size={14} className="inline mr-1" />拒绝
                </button>
              </div>
            ))}
          </div>
        ) : <Empty text="无待确认交易" />}
      </div>
    </div>
  );
}
