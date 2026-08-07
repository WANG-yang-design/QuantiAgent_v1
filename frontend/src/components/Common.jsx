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
/** 格式化大额: 支持负数(修复: 原实现对负值不换算) */
export function fmtWan(v) {
  const n = Number(v) || 0;
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e8) return sign + (abs / 1e8).toFixed(2) + "亿";
  if (abs >= 1e4) return sign + (abs / 1e4).toFixed(0) + "万";
  return n.toFixed(0);
}
/** 百分比格式化: null/undefined/NaN 一律显示 "-"(修复 NaN% 问题) */
export function fmtPct(v, d = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) + "%" : "-";
}

/** 顶部系统状态条: 数据库/LLM/熔断/运行模式。
 *  修复: 后端关闭时 react-query 保留旧 data, 状态永远显示绿色 ——
 *  改用 isError 实时判断, 失败显示红色"离线"并继续轮询。 */
export function SystemBar() {
  const { data: health, isError: healthErr } = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get("/api/health"),
    refetchInterval: (q) => (q.state.data ? 15000 : 5000),
    retry: 2,
  });
  const { data: mode, isError: modeErr } = useQuery({
    queryKey: ["sysmode"],
    queryFn: () => api.get("/api/system/mode"),
    refetchInterval: (q) => (q.state.data ? 15000 : 5000),
    retry: 2,
  });
  const offline = healthErr || modeErr;
  const items = [
    { label: "后端", ok: !offline && !!health?.db, warn: offline ? "连接失败(后端可能已关闭)" : "" },
    { label: "数据库", ok: !offline && !!health?.db },
    { label: "熔断", ok: !health?.paused, warn: health?.paused_reason },
  ];
  return (
    <div className="flex items-center gap-3 md:gap-4 flex-wrap text-xs">
      {offline && (
        <span className="badge bg-red-50 text-red-600 animate-pulse">
          后端离线 · 正在重连...
        </span>
      )}
      <span className="badge bg-brand-50 text-brand-600">
        模式: {mode?.trade_mode?.toUpperCase() || "-"} / {mode?.broker_adapter || "-"}
      </span>
      {items.map((it) => (
        <span key={it.label} className="hidden md:flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full ${it.ok ? "bg-green-500" : "bg-red-500"}`} />
          <span className="text-gray-600">{it.label}</span>
          {it.warn && <span className="text-red-500">{it.warn}</span>}
        </span>
      ))}
      {!offline && mode?.circuit?.paused && (
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
