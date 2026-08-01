import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useNavigate } from "react-router-dom";

/** 涨跌颜色工具 */
export function chgColor(v) {
  if (v > 0.05) return "text-up";
  if (v < -0.05) return "text-down";
  return "text-gray-700";
}
export function fmt(v, d = 3) {
  return v === null || v === undefined || isNaN(v) ? "-" : Number(v).toFixed(d);
}
export function fmtWan(v) {
  const n = Number(v) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(0) + "万";
  return n.toFixed(0);
}

/** 顶部系统状态条: 数据库/LLM/熔断/运行模式 */
export function SystemBar() {
  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get("/api/health"),
    refetchInterval: 15000,
  });
  const { data: mode } = useQuery({
    queryKey: ["sysmode"],
    queryFn: () => api.get("/api/system/mode"),
    refetchInterval: 15000,
  });
  const items = [
    { label: "数据库", ok: !!health?.db },
    { label: "熔断", ok: !health?.paused, warn: health?.paused_reason },
  ];
  return (
    <div className="flex items-center gap-4 flex-wrap text-xs">
      <span className="badge bg-brand-50 text-brand-600">
        模式: {mode?.trade_mode?.toUpperCase() || "-"} / {mode?.broker_adapter || "-"}
      </span>
      {items.map((it) => (
        <span key={it.label} className="flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full ${it.ok ? "bg-green-500" : "bg-red-500"}`} />
          <span className="text-gray-600">{it.label}</span>
          {it.warn && <span className="text-red-500">{it.warn}</span>}
        </span>
      ))}
      {mode?.circuit?.paused && (
        <span className="badge bg-red-50 text-red-600">熔断中: {mode.circuit.reason}</span>
      )}
    </div>
  );
}

/** 行情表格行(盯盘/仪表盘复用) */
export function QuoteRow({ q, onView }) {
  const nav = useNavigate();
  return (
    <tr
      className="cursor-pointer hover:bg-gray-50"
      onClick={() => (onView ? onView(q.symbol) : nav(`/symbol/${q.symbol}`))}
    >
      <td className="td font-medium">{q.symbol}</td>
      <td className="td text-gray-500">{q.name}</td>
      <td className={`td font-semibold ${chgColor(q.change_pct)}`}>{fmt(q.latest_price)}</td>
      <td className={`td font-semibold ${chgColor(q.change_pct)}`}>
        {q.change_pct > 0 ? "+" : ""}{q.change_pct?.toFixed(2)}%
      </td>
      <td className="td text-gray-600">{fmtWan(q.amount)}</td>
      <td className="td text-gray-600">{q.premium_rate ? (q.premium_rate * 100).toFixed(2) + "%" : "-"}</td>
    </tr>
  );
}

export function Empty({ text = "暂无数据" }) {
  return <div className="text-center text-gray-400 text-sm py-8">{text}</div>;
}

export function Spin() {
  return <div className="text-center text-gray-400 text-sm py-8">加载中...</div>;
}
