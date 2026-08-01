import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Plus, Trash2, ArrowUpDown, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, fmt, fmtWan, Empty, Spin } from "../components/Common";

const LS_KEY = "quantiagent_watchlist";
const DEFAULTS = ["510300", "159915", "588000", "512100", "159949", "513100", "512690", "515880"];

function loadList() {
  try { return JSON.parse(localStorage.getItem(LS_KEY)) || DEFAULTS; } catch { return DEFAULTS; }
}
function saveList(l) { localStorage.setItem(LS_KEY, JSON.stringify(l)); }

/** 实时盯盘: 自选池 10s 自动刷新, 红涨绿跌, 大波动高亮 */
export default function Watchlist() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [list, setList] = useState(loadList);
  const [addCode, setAddCode] = useState("");
  const [mode, setMode] = useState("watchlist");   // watchlist / top100
  const [sortKey, setSortKey] = useState("change_pct");
  const [prev, setPrev] = useState({});            // 上一轮价格(检测大波动)
  const [flash, setFlash] = useState({});

  const { data, isFetching } = useQuery({
    queryKey: ["quotes", mode, list],
    queryFn: () => api.get("/api/quotes", { symbols: mode === "watchlist" ? list.join(",") : "", limit: 60 }),
    refetchInterval: 10000,
  });

  // 大波动检测(>2%): 高亮 2 秒
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
      setTimeout(() => setFlash({}), 2000);
    }
  }, [data]);

  const add = useMutation({
    mutationFn: async () => {
      const code = addCode.trim().toUpperCase();
      if (!/^\d{6}$/.test(code)) throw new Error("请输入6位代码");
      if (!list.includes(code)) {
        const nl = [...list, code];
        saveList(nl);
        setList(nl);
      }
      setAddCode("");
    },
  });
  const remove = (code) => {
    const nl = list.filter((c) => c !== code);
    saveList(nl);
    setList(nl);
  };

  const rows = useMemo(() => {
    const arr = [...(data?.quotes || [])];
    if (sortKey === "change_pct") arr.sort((a, b) => b.change_pct - a.change_pct);
    else if (sortKey === "amount") arr.sort((a, b) => b.amount - a.amount);
    else arr.sort((a, b) => a.symbol.localeCompare(b.symbol));
    return arr;
  }, [data, sortKey]);

  return (
    <div className="p-5 space-y-4">
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
              <th className="th cursor-pointer" onClick={() => setSortKey("change_pct")}>最新价 <ArrowUpDown size={11} className="inline" /></th>
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
                  <td className="td">
                    {mode === "watchlist" && (
                      <button className="text-gray-300 hover:text-red-500" onClick={(e) => { e.stopPropagation(); remove(q.symbol); }}>
                        <Trash2 size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && <Empty text="无行情数据(先执行 fetch-symbols)" />}
      </div>
    </div>
  );
}
