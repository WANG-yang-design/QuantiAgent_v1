import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Plus, Trash2, Star, Power, Search } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, fmtWan, fmt } from "../components/Common";
import { decisionMeta } from "./AgentCenter";

/** 分类中文名与配色 */
const CAT_META = {
  holding: { label: "持仓", color: "bg-blue-50 text-blue-700" },
  hot: { label: "热门ETF", color: "bg-orange-50 text-orange-700" },
  watched: { label: "主动监控", color: "bg-green-50 text-green-700" },
  stock: { label: "股票", color: "bg-purple-50 text-purple-700" },
  etf: { label: "ETF", color: "bg-cyan-50 text-cyan-700" },
  default: { label: "默认池", color: "bg-gray-100 text-gray-600" },
};

/**
 * 监控标的: 盘中/每日监控池管理
 * 分类: 持仓(自动)/热门ETF(自动)/主动勾选/股票/ETF
 */
export default function WatchMonitor() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [addCode, setAddCode] = useState("");
  const [searchName, setSearchName] = useState("");

  const { data } = useQuery({
    queryKey: ["watchlist"],
    queryFn: () => api.get("/api/watchlist"),
    refetchInterval: 20000,
  });
  // 行情(给列表显示价格, 失败不影响)
  const { data: quotes } = useQuery({
    queryKey: ["quotes", (data?.items || []).map((i) => i.symbol).join(",")],
    queryFn: () => api.get("/api/quotes", { symbols: (data?.items || []).map((i) => i.symbol).join(",") }),
    enabled: (data?.items || []).length > 0,
    refetchInterval: 10000,
  });
  const qmap = Object.fromEntries((quotes?.quotes || []).map((q) => [q.symbol, q]));

  // 每个标的最近一次 Agent 决策结论(修复: 监控标的页看不到 Agent 对它的判断)
  const { data: decisions } = useQuery({
    queryKey: ["latest-decisions"],
    queryFn: () => api.get("/api/agents/latest_decisions", { days: 7 }),
    refetchInterval: 60000,
  });
  const dmap = Object.fromEntries((decisions?.items || []).map((d) => [d.symbol, d]));

  const add = useMutation({
    mutationFn: () => {
      const code = addCode.trim().toUpperCase();
      if (!/^\d{6}$/.test(code)) throw new Error("请输入6位代码");
      return api.post("/api/watchlist", { symbol: code, categories: ["watched"] }).then(() => setAddCode(""));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e) => window.alert("添加失败: " + (e.response?.data?.detail || e.message)),
  });
  const remove = useMutation({
    mutationFn: (symbol) => api.delete(`/api/watchlist/${symbol}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
    onError: (e) => window.alert("移除失败: " + (e.response?.data?.detail || e.message)),
  });
  const toggle = useMutation({
    mutationFn: ({ symbol, enabled }) => api.post(`/api/watchlist/${symbol}/enable`, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
  });
  const setCats = useMutation({
    mutationFn: ({ symbol, cats }) => api.post(`/api/watchlist/${symbol}/categories`, { categories: cats }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  const items = (data?.items || []).filter((i) => !searchName || (i.name || "").includes(searchName) || (i.symbol || "").includes(searchName));
  const groups = [
    ["holding", "持仓(自动监控)"],
    ["watched", "主动勾选监控"],
    ["stock", "股票"],
    ["etf", "ETF"],
    ["hot", "热门ETF(系统自动加入)"],
  ].map(([cat, label]) => ({
    cat, label,
    items: items.filter((i) => i.categories.includes(cat)),
  })).filter((g) => g.items.length);

  return (
    <div className="p-3 md:p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">监控标的</h1>
        <SystemBar />
      </div>

      <div className="card flex flex-wrap items-center gap-3">
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
          <input className="input w-32" placeholder="6位代码" value={addCode}
            onChange={(e) => setAddCode(e.target.value)} />
          <button type="submit" className="btn-primary" disabled={add.isPending}>
            <Plus size={14} className="inline mr-1" />添加监控
          </button>
        </form>
        <div className="flex items-center gap-2">
          <Search size={14} className="text-gray-400" />
          <input className="input w-40" placeholder="搜索代码/名称" value={searchName}
            onChange={(e) => setSearchName(e.target.value)} />
        </div>
        <span className="ml-auto text-xs text-gray-400">
          共 {items.length} 只 · 持仓与热门ETF由系统自动维护 · 调度器每30分钟扫描启用的标的
        </span>
      </div>

      {groups.map(({ cat, label, items: gitems }) => (
        <div key={cat} className="card">
          <div className="card-title">
            <span className={`badge ${CAT_META[cat]?.color || "bg-gray-100"}`}>{CAT_META[cat]?.label}</span>
            {label} <span className="text-gray-400 font-normal">({gitems.length})</span>
          </div>
          <div className="overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead>
              <tr>
                <th className="th">代码</th><th className="th">名称</th><th className="th">类型</th>
                {cat === "holding" && <>
                  <th className="th">持仓(总/可用)</th><th className="th">成本/现价</th><th className="th">浮盈亏</th>
                </>}
                <th className="th">最新价</th><th className="th">涨跌幅</th><th className="th">成交额</th>
                <th className="th">最近Agent结论</th>
                <th className="th">分类</th><th className="th">监控</th><th className="th">操作</th>
              </tr>
            </thead>
            <tbody>
              {gitems.map((i) => {
                const q = qmap[i.symbol] || {};
                const chg = q.change_pct;
                const pos = i.position;
                return (
                  <tr key={i.symbol} className="cursor-pointer hover:bg-gray-50" onClick={() => nav(`/symbol/${i.symbol}`)}>
                    <td className="td font-medium">{i.symbol}</td>
                    <td className="td">{i.name || "-"}</td>
                    <td className="td"><span className={`badge ${i.asset_type === "stock" ? "bg-purple-50 text-purple-700" : i.asset_type === "index" ? "bg-indigo-50 text-indigo-700" : "bg-cyan-50 text-cyan-700"}`}>{i.asset_type === "stock" ? "股票" : i.asset_type === "index" ? "指数" : "ETF"}</span></td>
                    {cat === "holding" && (
                      <>
                        <td className="td">{pos ? `${pos.total_qty}/${pos.available_qty}` : "-"}</td>
                        <td className="td">{pos ? `${fmt(pos.cost_price)}/${fmt(pos.latest_price)}` : "-"}</td>
                        <td className={`td font-semibold ${!pos ? "" : pos.pnl >= 0 ? "text-up" : "text-down"}`}>
                          {pos ? `${pos.pnl >= 0 ? "+" : ""}${fmt(pos.pnl, 2)} (${((pos.pnl_pct || 0) * 100).toFixed(2)}%)` : "-"}
                        </td>
                      </>
                    )}
                    <td className={`td font-semibold ${chg > 0 ? "text-up" : chg < 0 ? "text-down" : ""}`}>{q.latest_price ?? "-"}</td>
                    <td className={`td ${chg > 0 ? "text-up" : chg < 0 ? "text-down" : ""}`}>{chg != null ? `${chg > 0 ? "+" : ""}${chg.toFixed(2)}%` : "-"}</td>
                    <td className="td text-gray-600">{q.amount ? fmtWan(q.amount) : "-"}</td>
                    {/* 最近一次 Agent 决策结论: 点击跳转 Agent 决策页对应链路(修复) */}
                    <td className="td" onClick={(e) => e.stopPropagation()}>
                      {dmap[i.symbol] ? (() => {
                        const d = dmap[i.symbol];
                        const dm = decisionMeta(d.decision);
                        return (
                          <button className="flex flex-col items-start gap-0.5 hover:opacity-80"
                            title={`${d.time} 首席结论, 点击查看完整决策链路`}
                            onClick={() => nav(`/agents?trace=${d.trace_id}`)}>
                            <span className={`badge border ${dm.cls}`}>
                              <span className="w-1.5 h-1.5 rounded-full inline-block mr-1" style={{ background: dm.dot }} />
                              {dm.label}
                              {d.confidence != null ? ` ${Math.round(d.confidence * 100)}%` : ""}
                            </span>
                            <span className="text-[10px] text-gray-400">{d.time.slice(5, 16)}</span>
                          </button>
                        );
                      })() : <span className="text-xs text-gray-300">无</span>}
                    </td>
                    <td className="td">
                      <div className="flex gap-1 flex-wrap">
                        {i.categories.map((c) => (
                          <span key={c} className={`badge ${CAT_META[c]?.color || "bg-gray-100"}`}>{CAT_META[c]?.label || c}</span>
                        ))}
                      </div>
                    </td>
                    <td className="td" onClick={(e) => e.stopPropagation()}>
                      <button
                        className={`btn ${i.enabled ? "btn-green" : "btn-ghost"}`}
                        onClick={() => toggle.mutate({ symbol: i.symbol, enabled: !i.enabled })}
                        title={i.enabled ? "停用监控" : "启用监控"}
                      >
                        <Power size={13} className="inline mr-0.5" />{i.enabled ? "监控中" : "已停用"}
                      </button>
                    </td>
                    <td className="td" onClick={(e) => e.stopPropagation()}>
                      <div className="flex gap-1 items-center">
                        <button className="btn-ghost" title="设为主动监控"
                          onClick={() => setCats.mutate({
                            symbol: i.symbol,
                            // 修复: 重复点击会重复添加"watched"分类 → 重复badge+DB冗余
                            cats: i.categories.includes("watched")
                              ? i.categories
                              : [...i.categories, "watched"],
                          })}>
                          <Star size={13} />
                        </button>
                        {!["holding", "hot"].includes(cat) && (
                          <button className="text-gray-300 hover:text-red-500" title="移除"
                            onClick={() => remove.mutate(i.symbol)}>
                            <Trash2 size={14} />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        </div>
      ))}
      {!groups.length && <div className="card text-center text-gray-400 py-8">暂无监控标的, 在上方输入代码添加</div>}
    </div>
  );
}
