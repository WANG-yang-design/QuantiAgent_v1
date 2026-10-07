import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

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
/** 盈亏颜色: 0 用中性灰(A股习惯红涨绿跌, 0 显示红色会误导) */
export function pnlColor(v) {
  const n = Number(v) || 0;
  if (n > 1e-9) return "text-up";
  if (n < -1e-9) return "text-down";
  return "text-gray-500";
}
/** 错误信息提取(axios error → 可读文案) */
export function errMsg(e) {
  return e?.response?.data?.detail || e?.message || "请求失败";
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

export function Empty({ text = "暂无数据" }) {
  return <div className="text-center text-gray-400 text-sm py-8">{text}</div>;
}

/** 查询失败提示(替代"暂无数据"误导) */
export function ErrorBox({ error, onRetry, text = "加载失败" }) {
  return (
    <div className="text-center py-8 space-y-2">
      <div className="text-sm text-red-500">{text}: {errMsg(error)}</div>
      {onRetry && <button className="btn-ghost text-xs" onClick={onRetry}>重试</button>}
    </div>
  );
}

export function Spin({ text = "加载中..." }) {
  return (
    <div className="flex items-center justify-center gap-2 text-gray-400 text-sm py-8">
      <span className="w-4 h-4 border-2 border-gray-300 border-t-brand-600 rounded-full animate-spin" />
      {text}
    </div>
  );
}

/** 骨架屏(卡片内容加载中) */
export function SkeletonLines({ rows = 3 }) {
  return (
    <div className="space-y-2.5 py-1">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton-line" style={{ width: `${88 - i * 12}%` }} />
      ))}
    </div>
  );
}

