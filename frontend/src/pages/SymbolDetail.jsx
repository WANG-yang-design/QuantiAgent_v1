import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Search, Zap, Star, ArrowLeft, TrendingUp, Wallet } from "lucide-react";
import { api } from "../api/client";
import KlineChart from "../components/KlineChart";
import IntradayChart from "../components/IntradayChart";
import { fmt, fmtPct, Spin, Empty, chgColor } from "../components/Common";
import { useScanStore } from "../store/scanStore";

/** 标的详情: 无代码时显示搜索引导页 */
export default function SymbolDetail() {
  const { code } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [searchCode, setSearchCode] = useState("");
  const [scanErrorMsg, setScanErrorMsg] = useState("");
  const [chartPeriod, setChartPeriod] = useState("day");   // day / intraday
  const symbol = (code || "").toUpperCase();
  const { taskId, symbol: scanSymbol, status: scanStatus, setTask, update } = useScanStore();

  // 注意: 搜索页的 return 必须放在所有 hooks 之后(文件末尾),
  // 否则路由切换时 hooks 数量变化会导致 React 崩溃白屏。

  const { data: kline } = useQuery({
    queryKey: ["kline", symbol],
    queryFn: () => api.get(`/api/kline/${symbol}?days=250`),
    enabled: !!symbol && chartPeriod === "day",
  });
  const { data: intraday, isFetching: intradayLoading, error: intradayErr } = useQuery({
    queryKey: ["intraday", symbol],
    queryFn: () => api.get(`/api/intraday/${symbol}?days=5`),
    enabled: !!symbol && chartPeriod === "intraday",
    refetchInterval: chartPeriod === "intraday" ? 30000 : false,
    retry: false,
  });
  const { data: detail } = useQuery({
    queryKey: ["symbol", symbol],
    queryFn: () => api.get(`/api/symbol/${symbol}`),
    enabled: !!symbol,
    refetchInterval: 15000,
  });
  // 当前标的的持仓(有持仓时展示持仓卡片)
  const { data: positions } = useQuery({
    queryKey: ["positions"],
    queryFn: () => api.get("/api/positions"),
    refetchInterval: 30000,
  });
  const myPosition = (positions || []).find((p) => p.symbol === symbol);

  const [analyzing, setAnalyzing] = useState(false);
  const [scanResult, setScanResult] = useState(null);
  const [inWatch, setInWatch] = useState(false);
  const { data: watch } = useQuery({ queryKey: ["watchlist"], queryFn: () => api.get("/api/watchlist"), refetchInterval: 60000 });
  useEffect(() => {
    if (watch?.items) setInWatch(watch.items.some((i) => i.symbol === symbol));
  }, [watch, symbol]);
  // 路由切换(标的变化)时重置本地分析状态, 防止残留上一个标的的分析结果
  useEffect(() => {
    setScanResult(null);
    setAnalyzing(false);
    setScanErrorMsg("");
    setChartPeriod("day");
  }, [symbol]);
  const addWatch = useMutation({
    mutationFn: () => (inWatch
      ? api.delete(`/api/watchlist/${symbol}`)
      : api.post("/api/watchlist", { symbol, categories: ["watched"] })),
    onSuccess: () => { setInWatch(!inWatch); qc.invalidateQueries({ queryKey: ["watchlist"] }); },
    onError: (e) => window.alert("操作失败: " + (e.response?.data?.detail || e.message)),
  });
  const runScan = useMutation({
    mutationFn: async () => {
      setAnalyzing(true);
      setScanResult(null);
      setScanErrorMsg("");
      const { task_id } = await api.post(`/api/scan/${symbol}`);
      setTask(task_id, symbol);
      return task_id;
    },
    onError: (e) => {
      // 修复: 分析失败时重置卡死状态并提示(原实现 analyzing 永远为 true)
      setAnalyzing(false);
      setScanErrorMsg(e.response?.data?.detail || e.message || "分析任务提交失败");
    },
  });
  // 异步分析任务轮询(全局store, 切页不丢); 终态后停止轮询
  const { data: scanStatusData } = useQuery({
    queryKey: ["scantask", taskId],
    queryFn: () => api.get(`/api/scan/status/${taskId}`),
    enabled: !!taskId && scanSymbol === symbol && scanStatus !== "DONE" && scanStatus !== "FAILED",
    refetchInterval: scanStatus === "RUNNING" ? 3000 : false,
    // 修复: 轮询接口异常(服务重启/网络)时不再无限卡在"分析中", 置为终态
    onError: () => {
      setAnalyzing(false);
      setScanErrorMsg("任务状态查询失败(服务可能已重启), 请重新分析");
      update({ status: "FAILED", error: "任务状态查询失败" });
    },
  });
  useEffect(() => {
    if (!scanStatusData) return;
    if (scanStatusData.status === "DONE") {
      setScanResult(scanStatusData.result);
      setAnalyzing(false);
      update({ status: "DONE" });
    } else if (scanStatusData.status === "FAILED") {
      setAnalyzing(false);
      setScanErrorMsg(scanStatusData.error || "分析失败");
      update({ status: "FAILED", error: scanStatusData.error || "" });
    }
  }, [scanStatusData]);

  const t = detail?.technical || {};
  const q = detail?.quote || {};
  const ob = detail?.order_book || {};

  const techItems = [
    ["MA5/20/60", `${fmt(t.ma5)}/${fmt(t.ma20)}/${fmt(t.ma60)}`],
    ["MACD", `DIF ${fmt(t.macd_dif, 4)} DEA ${fmt(t.macd_dea, 4)}`],
    ["RSI(14)", fmt(t.rsi, 1) + (t.rsi_overbought ? " 超买" : t.rsi_oversold ? " 超卖" : "")],
    ["20日波动率", fmtPct(t.volatility_20d, 1)],
    ["60日最大回撤", fmtPct(t.max_drawdown_60d, 1)],
    ["20日动量", fmtPct(t.momentum_20d, 2)],
    ["量比", fmt(t.volume_ratio, 2)],
    ["支撑/压力", `${fmt(t.support_20d)} / ${fmt(t.resistance_20d)}`],
    ["均线排列", t.bull_align ? "多头" : t.bear_align ? "空头" : "缠绕"],
    ["趋势强度", fmtPct(t.trend_strength, 0)],
  ];

  // 无代码 → 搜索引导页(所有 hooks 之后的条件渲染, 保证 hooks 顺序一致)
  if (!symbol) {
    return <SymbolSearchPage nav={nav} searchCode={searchCode} setSearchCode={setSearchCode} />;
  }

  return (
    <div className="p-3 md:p-5 space-y-4">
      {/* 头部: 搜索 + 标的名称
          修复: 原返回按钮固定跳转 /watchlist, 从任意页面进入详情后都回不到
          原页面。改为历史回退(无历史时退回实时盯盘)。 */}
      <div className="flex items-center gap-3 flex-wrap">
        <button className="text-gray-400 hover:text-brand-600" title="返回上一页"
          onClick={() => (window.history.length > 1 ? nav(-1) : nav("/watchlist"))}>
          <ArrowLeft size={18} />
        </button>
        <h1 className="text-lg font-bold text-brand-600">{symbol} {detail?.name || ""}</h1>
        <form className="flex gap-2 ml-auto" onSubmit={(e) => { e.preventDefault(); if (/^\d{6}$/.test(searchCode)) nav(`/symbol/${searchCode}`); }}>
          <input className="input w-32" placeholder="搜索6位代码" value={searchCode}
            onChange={(e) => setSearchCode(e.target.value)} />
          <button type="submit" className="btn-primary"><Search size={14} className="inline mr-1" />搜索</button>
        </form>
        <button className="btn-primary" disabled={runScan.isPending || analyzing || (scanStatus === "RUNNING" && scanSymbol === symbol)} onClick={() => runScan.mutate()}>
          <Zap size={14} className="inline mr-1" />{analyzing || (scanStatus === "RUNNING" && scanSymbol === symbol) ? "分析中..." : "立即分析"}
        </button>
        <button
          className={inWatch ? "btn-green" : "btn-primary"}
          onClick={() => addWatch.mutate()}
          title={inWatch ? "已加入监控, 点击移除" : "加入监控列表"}
        >
          <Star size={14} className={`inline mr-1 ${inWatch ? "fill-current" : ""}`} />
          {inWatch ? "监控中" : "加入自选"}
        </button>
      </div>

      {/* 分析进行中提示 + 节点级实时进度(修复: 原实现只有"分析中"无任何进度) */}
      {analyzing && !scanResult && (
        <div className="card border-amber-200 bg-amber-50/40">
          <div className="flex items-center gap-3 text-sm">
            <span className="badge bg-amber-50 text-amber-700 animate-pulse">分析中</span>
            <span className="text-gray-600">{scanStatusData?.current_node || "正在启动 15 个 Agent 链路(数据闸门→分析师→多空辩论→首席→交易员→风控→合规→执行)..."}</span>
            {scanStatusData?.progress_pct != null && (
              <span className="text-xs text-gray-500">进度 {scanStatusData.progress_pct}%</span>
            )}
          </div>
          {scanStatusData?.progress_pct != null && (
            <>
              <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden mt-2">
                <div className="h-full bg-amber-500 rounded-full transition-all duration-500"
                  style={{ width: `${scanStatusData.progress_pct || 0}%` }} />
              </div>
              {(scanStatusData.node_log || []).length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {scanStatusData.node_log.map((n, i) => (
                    <span key={i}
                      className={`badge text-[10px] ${
                        n.status === "done" ? "bg-green-50 text-green-600"
                        : n.status === "failed" ? "bg-red-50 text-red-600"
                        : "bg-amber-50 text-amber-700 animate-pulse"}`}
                      title={n.error || ""}>
                      {n.label} {n.status === "done" ? `(${n.cost}s)` : n.status === "failed" ? "失败" : "..."}
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* 分析失败提示(修复: 原实现错误被吞掉, 按钮永久卡死) */}
      {scanErrorMsg && !analyzing && (
        <div className="card border-red-200 bg-red-50 text-red-600 text-sm">
          分析失败: {scanErrorMsg}
        </div>
      )}

      {/* 分析结果 */}
      {scanResult && (
        <div className="card border-brand-200 bg-brand-50/40">
          <div className="card-title">分析结果 ({scanResult.trace_id})</div>
          <div className="flex gap-3 flex-wrap text-sm">
            <span>研究结论: <b className="text-brand-600">{scanResult.chief?.research_decision || "-"}</b>
              {" "}(置信 {((scanResult.chief?.confidence ?? 0) * 100)?.toFixed(0)}%)</span>
            <span>交易计划: <b>{scanResult.plan?.action || "-"} {scanResult.plan?.estimated_quantity || 0}份</b></span>
            <span>风控: <b className={scanResult.risk?.risk_decision === "REJECT" ? "text-red-600" : "text-green-600"}>
              {scanResult.risk?.risk_decision || "-"}</b>
              {scanResult.risk?.blocked_reason && <span className="text-red-500 ml-2">{scanResult.risk.blocked_reason}</span>}
            </span>
            <span>执行: <b>{scanResult.execution?.status || "-"}</b></span>
            <button className="btn-ghost ml-auto" onClick={() => nav("/agents")}>查看完整决策链路</button>
          </div>
        </div>
      )}

      {/* 持仓卡片(当前标的在模拟盘有持仓时展示) */}
      {myPosition && (
        <div className="card border-blue-200 bg-blue-50/40">
          <div className="card-title"><Wallet size={14} />当前持仓</div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
            <div><span className="text-xs text-gray-500 block">总数量 / 可用</span>
              <b>{myPosition.total_qty} / {myPosition.available_qty}</b></div>
            <div><span className="text-xs text-gray-500 block">成本价 / 现价</span>
              <b>{fmt(myPosition.cost_price)} / {fmt(myPosition.latest_price)}</b></div>
            <div><span className="text-xs text-gray-500 block">市值</span>
              <b>{fmt(myPosition.market_value, 2)}</b></div>
            <div><span className="text-xs text-gray-500 block">浮动盈亏</span>
              <b className={myPosition.pnl >= 0 ? "text-up" : "text-down"}>
                {myPosition.pnl >= 0 ? "+" : ""}{fmt(myPosition.pnl, 2)}
                <span className="ml-1">({((myPosition.pnl_pct || 0) * 100).toFixed(2)}%)</span>
              </b></div>
          </div>
        </div>
      )}

      {/* 行情卡片组 */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="card">
          <div className="text-xs text-gray-500">最新价</div>
          <div className={`text-2xl font-bold ${chgColor(q.change_pct)}`}>{fmt(q.latest_price)}</div>
          <div className={`text-sm ${chgColor(q.change_pct)}`}>
            {q.change_pct > 0 ? "+" : ""}{q.change_pct?.toFixed(2)}% · 更新 {detail?.time}
          </div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">成交额 / 量</div>
          <div className="text-lg font-bold">{(q.amount || 0) >= 1e8 ? ((q.amount || 0) / 1e8).toFixed(2) + "亿" : ((q.amount || 0) / 1e4).toFixed(0) + "万"}</div>
          <div className="text-xs text-gray-500">{q.volume ? (q.volume / 1e8).toFixed(2) : "-"}亿份</div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">盘口(五档买卖)</div>
          <div className="flex justify-between text-sm mt-1">
            <span className="text-down">卖一 {fmt(ob.ask1)} × {fmt(ob.ask_vol1, 0)}</span>
            <span className="text-up">买一 {fmt(ob.bid1)} × {fmt(ob.bid_vol1, 0)}</span>
          </div>
          <div className="text-xs text-gray-400 mt-1">价差 {ob.spread != null ? fmtPct(ob.spread, 2) : "-"} · 质量 {detail?.order_book_quality}</div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">ETF专项</div>
          <div className="text-lg font-bold">{(detail?.etf_info?.tracking_index || "-")}</div>
          <div className="text-xs text-gray-500">
            溢价 {q.premium_rate != null ? fmtPct(q.premium_rate, 2) : "-"} · IOPV {fmt(q.iopv)} · {detail?.etf_info?.is_qdii ? "QDII" : "境内"}
          </div>
        </div>
      </div>

      {/* K线 + 技术指标(日K/分时切换) */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card lg:col-span-2">
          <div className="card-title flex items-center gap-2">
            <span>{chartPeriod === "day" ? "日K线(前复权)" : "今日分时"}</span>
            <div className="flex gap-1 ml-auto">
              <button className={`btn !px-2 !py-0.5 text-xs ${chartPeriod === "day" ? "btn-primary" : "btn-ghost"}`}
                onClick={() => setChartPeriod("day")}>日K</button>
              <button className={`btn !px-2 !py-0.5 text-xs ${chartPeriod === "intraday" ? "btn-primary" : "btn-ghost"}`}
                onClick={() => setChartPeriod("intraday")}>分时</button>
            </div>
          </div>
          {chartPeriod === "day" ? (
            kline?.candles?.length ? <KlineChart candles={kline.candles} /> : <Empty text="无K线(先执行 fetch-daily)" />
          ) : intraday?.days?.length ? (
            <>
              <div className="text-xs text-gray-500 mb-1">
                {intraday.name || symbol} · 最近{intraday.days.length}个交易日分时 · 最新 {fmt(intraday.latest_price)}
                <span className={intraday.change_pct >= 0 ? "text-up ml-1" : "text-down ml-1"}>
                  {intraday.change_pct >= 0 ? "+" : ""}{intraday.change_pct}%
                </span> · 30秒自动刷新
              </div>
              <IntradayChart days={intraday.days} />
            </>
          ) : intradayLoading ? <div className="py-10"><Spin /></div> : <Empty text={intradayErr?.response?.data?.detail || "无分时数据(非交易时段)"} />
          }
        </div>
        <div className="card">
          <div className="card-title">技术指标(最新)</div>
          <div className="space-y-1">
            {techItems.map(([k, v]) => (
              <div key={k} className="flex justify-between py-1 border-b border-gray-50 text-sm">
                <span className="text-gray-500">{k}</span><span className="font-medium">{v}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* 新闻 + 公告 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card">
          <div className="card-title">近期新闻 ({(detail?.news || []).length})</div>
          <div className="space-y-2 max-h-80 overflow-y-auto">
            {detail?.news?.length ? detail.news.map((n, i) => (
              <div key={i} className="border border-gray-100 rounded-lg p-2.5">
                <div className="flex justify-between gap-2">
                  <span className="text-sm font-medium line-clamp-1">{n.title}</span>
                  <span className="text-[10px] text-gray-400 shrink-0">{n.publish_time}</span>
                </div>
                <div className="text-xs text-gray-500 mt-1 line-clamp-2">{n.content}</div>
              </div>
            )) : <Empty text="暂无新闻" />}
          </div>
        </div>
        <div className="card">
          <div className="card-title">近期公告 ({(detail?.announcements || []).length})</div>
          <div className="space-y-2 max-h-80 overflow-y-auto">
            {detail?.announcements?.length ? detail.announcements.map((a, i) => (
              <div key={i} className="border border-gray-100 rounded-lg p-2.5 flex items-center gap-2">
                <span className={`badge ${a.risk_level === "high" ? "bg-red-50 text-red-600" : a.risk_level === "medium" ? "bg-amber-50 text-amber-600" : "bg-gray-100 text-gray-500"}`}>
                  {a.risk_level}
                </span>
                <span className="text-xs text-gray-500">{a.event_type}</span>
                <span className="flex-1 text-sm line-clamp-1">{a.title}</span>
                <span className="text-[10px] text-gray-400 shrink-0">{a.publish_time}</span>
              </div>
            )) : <Empty text="暂无公告" />}
          </div>
        </div>
      </div>
    </div>
  );
}

/** 无代码时的搜索引导页(作为独立组件, 避免 hooks 顺序问题) */
function SymbolSearchPage({ nav, searchCode, setSearchCode }) {
  const { data: hotEtf } = useQuery({
    queryKey: ["universe-top"],
    queryFn: () => api.get("/api/universe/top?limit=24"),
    refetchInterval: 300000,
  });
  const { data: hotStock } = useQuery({
    queryKey: ["stocks-top"],
    queryFn: () => api.get("/api/stocks/spot?limit=12"),
    refetchInterval: 600000,
  });

  const renderHot = (items, label) => (
    <div className="card">
      <div className="card-title"><TrendingUp size={14} />{label}(点击查看详情)</div>
      {items?.length ? (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-1.5">
          {items.map((s) => (
            <button key={s.symbol} className="flex items-center gap-2 px-2 py-1.5 rounded-lg border border-gray-100 hover:border-brand-300 hover:bg-brand-50 text-left"
              onClick={() => nav(`/symbol/${s.symbol}`)}>
              <span className="badge shrink-0">{s.asset_type === "stock" ? "股票" : "ETF"}</span>
              <span className="flex-1 min-w-0">
                <span className="block text-xs font-medium truncate">{s.name || s.symbol}</span>
                <span className="block text-[10px] text-gray-400">{s.symbol}</span>
              </span>
              <span className={`text-xs font-semibold shrink-0 ${s.change_pct > 0 ? "text-up" : s.change_pct < 0 ? "text-down" : ""}`}>
                {s.change_pct == null || (!s.change_pct && !s.latest_price) ? "-" : `${s.change_pct > 0 ? "+" : ""}${s.change_pct?.toFixed(2)}%`}
              </span>
            </button>
          ))}
        </div>
      ) : <Empty text="热门标的加载失败(稍后自动重试)" />}
    </div>
  );

  return (
    <div className="p-5 max-w-4xl mx-auto space-y-6">
      <h1 className="text-lg font-bold text-brand-600">标的搜索</h1>
      <form className="card flex gap-2 items-center"
        onSubmit={(e) => { e.preventDefault(); if (/^\d{6}$/.test(searchCode)) nav(`/symbol/${searchCode}`); }}>
        <input className="input flex-1 text-base py-2.5" placeholder="输入6位代码, 如 510300 / 159915 / 600519"
          value={searchCode} onChange={(e) => setSearchCode(e.target.value)} autoFocus />
        <button type="submit" className="btn-primary"><Search size={15} className="inline mr-1" />搜索</button>
      </form>
      {renderHot(hotEtf, "热门ETF(按成交额, 动态)")}
      {renderHot(hotStock?.stocks, "热门股票(按成交额, 动态)")}      <div className="text-xs text-gray-400">
        提示: 热门标的由后端实时数据生成(不再写死); 查看过的标的记录在左侧最近工作流与监控列表中。
      </div>
    </div>
  );
}


