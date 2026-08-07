import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Plus, Trash2, ArrowUpDown, RefreshCw, Star } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, fmt, fmtWan, Empty } from "../components/Common";

/** 实时盯盘: 自选池来自"监控标的"(DB持久化), 10s自动刷新 */
export default function Watchlist() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [addCode, setAddCode] = useState("");
  const [mode, setMode] = useState("watchlist");   // watchlist / top100
  const [sortKey, setSortKey] = useState("change_pct");
  const [prev, setPrev] = useState({});
  const [flash, setFlash] = useState({});

  const { data: watch } = useQuery({
    queryKey: ["watchlist"],
    queryFn: () => api.get("/api/watchlist"),
    refetchInterval: 60000,
  });
  const watchSymbols = (watch?.items || []).filter((i) => i.enabled).map((i) => i.symbol);

  const { data, isFetching } = useQuery({
    queryKey: ["quotes", mode, watchSymbols],
    queryFn: () => api.get("/api/quotes", { symbols: mode === "watchlist" ? watchSymbols.join(",") : "", limit: 60 }),
    enabled: mode === "top100" || watchSymbols.length > 0,
    refetchInterval: 10000,
  });

  // 大波动检测(>0.2%): 高亮 2 秒
  useEffect(() => {
    const q = data?.quotes || [];
    const now = {};
    const flashNow = {};
    for (const it of q) {
      now[it.symbol] = it.latest_price;
      if (prev[it.symbol] && prev[it.symbol] !== it.latest_price) {
        const chg = (it.latest_price - prev[it.symbol]) / prev[it.symbol];
        if (Math.abs(chg) > 0.002) flashNow[it.symbol] = chg > 0 ? "up" : "down";
      }
    }
    setPrev(now);
    if (Object.keys(flashNow).length) {
      setFlash(flashNow);
      const t = setTimeout(() => setFlash({}), 2000);
      return () => clearTimeout(t);   // 清理定时器(修复: 组件卸载后仍 setState)
    }
  }, [data]);

  const add = useMutation({
    mutationFn: async (codeArg) => {
      const code = (codeArg || addCode).trim().toUpperCase();
      if (!/^\d{6}$/.test(code)) throw new Error("请输入6位代码");
      await api.post("/api/watchlist", { symbol: code, categories: ["watched"] });
      setAddCode("");
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e) => window.alert("添加失败: " + (e.response?.data?.detail || e.message)),
  });
  const remove = useMutation({
    mutationFn: (code) => api.delete(`/api/watchlist/${code}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e) => window.alert("移除失败: " + (e.response?.data?.detail || e.message)),
  });
  // 监控开关(修复: 自选池每行没有"加入监控/停用监控"入口)
  const toggleWatch = useMutation({
    mutationFn: ({ symbol, enabled }) => api.post(`/api/watchlist/${symbol}/enable`, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e) => window.alert("操作失败: " + (e.response?.data?.detail || e.message)),
  });
  const watchState = Object.fromEntries((watch?.items || []).map((i) => [i.symbol, i]));

  const rows = useMemo(() => {
    const arr = [...(data?.quotes || [])];
    if (sortKey === "change_pct") arr.sort((a, b) => b.change_pct - a.change_pct);
    else if (sortKey === "amount") arr.sort((a, b) => b.amount - a.amount);
    else if (sortKey === "latest_price") arr.sort((a, b) => b.latest_price - a.latest_price);
    else arr.sort((a, b) => a.symbol.localeCompare(b.symbol));
    return arr;
  }, [data, sortKey]);

  // 排序方法下拉(修复: 原实现只能点表头排序, 用户无从发现, 且无法按最新价排序)
  const SORT_OPTIONS = [
    ["change_pct", "按涨跌幅(降序)"],
    ["amount", "按成交额(降序)"],
    ["latest_price", "按最新价(降序)"],
    ["symbol", "按代码"],
  ];

  return (
    <div className="p-3 md:p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">实时盯盘</h1>
        <SystemBar />
      </div>

      {/* 工具条 */}
      <div className="card flex flex-wrap items-center gap-3">
        <div className="flex gap-2">
          <button className={`btn ${mode === "watchlist" ? "btn-primary" : "btn-ghost"}`} onClick={() => setMode("watchlist")}>自选池</button>
          <button className={`btn ${mode === "top100" ? "btn-primary" : "btn-ghost"}`} onClick={() => setMode("top100")}>成交额Top60</button>
        </div>
        {mode === "watchlist" && (
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
            <input className="input w-32" placeholder="6位代码" value={addCode}
              onChange={(e) => setAddCode(e.target.value)} />
            <button type="submit" className="btn-primary"><Plus size={14} className="inline mr-1" />添加</button>
          </form>
        )}
        <select className="input !w-44 text-sm" value={sortKey}
          onChange={(e) => setSortKey(e.target.value)} title="排序方法">
          {SORT_OPTIONS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </select>
        <button className="btn-ghost ml-auto" onClick={() => qc.invalidateQueries({ queryKey: ["quotes"] })}>
          <RefreshCw size={14} className={`inline mr-1 ${isFetching ? "animate-spin" : ""}`} />刷新
        </button>
        <span className="text-xs text-gray-400">10秒自动刷新 · 更新于 {data?.time}</span>
      </div>

      {/* 行情表 */}
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[640px]">
          <thead>
            <tr>
              <th className="th cursor-pointer" onClick={() => setSortKey("symbol")}>代码 <ArrowUpDown size={11} className="inline" /></th>
              <th className="th">名称</th>
              <th className="th cursor-pointer" onClick={() => setSortKey("latest_price")}>最新价 <ArrowUpDown size={11} className="inline" /></th>
              <th className="th cursor-pointer" onClick={() => setSortKey("change_pct")}>涨跌幅</th>
              <th className="th cursor-pointer" onClick={() => setSortKey("amount")}>成交额</th>
              <th className="th">溢价率</th>
              <th className="th">IOPV</th>
              <th className="th"></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((q) => {
              const f = flash[q.symbol];
              const bg = f === "up" ? "bg-red-100" : f === "down" ? "bg-green-100" : "";
              const inWatch = watchSymbols.includes(q.symbol);
              return (
                <tr key={q.symbol} className={`cursor-pointer hover:bg-gray-50 ${bg} transition-colors`}
                  onClick={() => nav(`/symbol/${q.symbol}`)}>
                  <td className="td font-medium">{q.symbol}</td>
                  <td className="td text-gray-500">{q.name}</td>
                  <td className={`td font-semibold ${q.change_pct > 0 ? "text-up" : q.change_pct < 0 ? "text-down" : ""}`}>{fmt(q.latest_price)}</td>
                  <td className={`td font-semibold ${q.change_pct > 0 ? "text-up" : q.change_pct < 0 ? "text-down" : ""}`}>
                    {q.change_pct > 0 ? "+" : ""}{q.change_pct?.toFixed(2)}%
                  </td>
                  <td className="td text-gray-600">{fmtWan(q.amount)}</td>
                  <td className="td text-gray-600">{q.premium_rate ? (q.premium_rate * 100).toFixed(2) + "%" : "-"}</td>
                  <td className="td text-gray-500">{fmt(q.iopv)}</td>
                  <td className="td" onClick={(e) => e.stopPropagation()}>
                    {mode === "watchlist" ? (
                      <div className="flex items-center gap-1">
                        <button
                          className={watchState[q.symbol]?.enabled ? "text-brand-500" : "text-gray-300 hover:text-brand-500"}
                          title={watchState[q.symbol]?.enabled ? "监控中, 点击停用" : "停用中, 点击加入监控"}
                          onClick={() => toggleWatch.mutate({ symbol: q.symbol, enabled: !watchState[q.symbol]?.enabled })}
                        >
                          <Star size={15} className={watchState[q.symbol]?.enabled ? "fill-current" : ""} />
                        </button>
                        <button className="text-gray-300 hover:text-red-500" title="移除监控"
                          onClick={() => remove.mutate(q.symbol)}>
                          <Trash2 size={14} />
                        </button>
                      </div>
                    ) : (
                      <button
                        className={inWatch ? "text-brand-500" : "text-gray-300 hover:text-brand-500"}
                        title={inWatch ? "已在监控列表" : "一键加入监控列表"}
                        onClick={() => { if (!inWatch) add.mutate(q.symbol); }}
                      >
                        <Star size={15} className={inWatch ? "fill-current" : ""} />
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && (isFetching ? <div className="text-center text-gray-400 text-sm py-8">加载中...</div> : <Empty text="无行情数据(先执行 fetch-symbols)" />)}
      </div>
    </div>
  );
}

