import { useState, useEffect } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Search, Zap, Star, ArrowLeft } from "lucide-react";
import { api } from "../api/client";
import KlineChart from "../components/KlineChart";
import { fmt, Spin, Empty, chgColor } from "../components/Common";

/** 标的详情: K线 / 行情 / 盘口 / 指标 / 新闻公告 / 立即分析 */
export default function SymbolDetail() {
  const { code } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [searchCode, setSearchCode] = useState("");
  const symbol = (searchCode || code || "").toUpperCase();

  const { data: kline } = useQuery({
    queryKey: ["kline", symbol],
    queryFn: () => api.get(`/api/kline/${symbol}?days=250`),
    enabled: !!symbol,
  });
  const { data: detail } = useQuery({
    queryKey: ["symbol", symbol],
    queryFn: () => api.get(`/api/symbol/${symbol}`),
    enabled: !!symbol,
    refetchInterval: 15000,
  });

  const [analyzing, setAnalyzing] = useState(false);
  const [scanResult, setScanResult] = useState(null);
  const [scanTask, setScanTask] = useState(null);
  const [inWatch, setInWatch] = useState(false);
  const { data: watch } = useQuery({ queryKey: ["watchlist"], queryFn: () => api.get("/api/watchlist"), refetchInterval: 60000 });
  useEffect(() => {
    if (watch?.items) setInWatch(watch.items.some((i) => i.symbol === symbol));
  }, [watch, symbol]);
  const addWatch = useMutation({
    mutationFn: () => (inWatch
      ? api.delete(`/api/watchlist/${symbol}`)
      : api.post("/api/watchlist", { symbol, categories: ["watched"] })),
    onSuccess: () => { setInWatch(!inWatch); qc.invalidateQueries({ queryKey: ["watchlist"] }); },
  });
  const runScan = useMutation({
    mutationFn: async () => {
      setAnalyzing(true);
      setScanResult(null);
      const { task_id } = await api.post(`/api/scan/${symbol}`);
      setScanTask(task_id);
      return task_id;
    },
  });
  // 异步分析任务轮询
  const { data: scanStatus } = useQuery({
    queryKey: ["scantask", scanTask],
    queryFn: () => api.get(`/api/scan/status/${scanTask}`),
    enabled: !!scanTask,
    refetchInterval: 3000,
  });
  useEffect(() => {
    if (scanStatus?.status === "DONE") {
      setScanResult(scanStatus.result);
      setScanTask(null);
      setAnalyzing(false);
    }
    if (scanStatus?.status === "FAILED") {
      setScanTask(null);
      setAnalyzing(false);
    }
  }, [scanStatus]);

  const t = detail?.technical || {};
  const q = detail?.quote || {};
  const ob = detail?.order_book || {};

  const techItems = [
    ["MA5/20/60", `${fmt(t.ma5)}/${fmt(t.ma20)}/${fmt(t.ma60)}`],
    ["MACD", `DIF ${fmt(t.macd_dif, 4)} DEA ${fmt(t.macd_dea, 4)}`],
    ["RSI(14)", fmt(t.rsi, 1) + (t.rsi_overbought ? " 超买" : t.rsi_oversold ? " 超卖" : "")],
    ["20日波动率", (t.volatility_20d * 100)?.toFixed(1) + "%"],
    ["60日最大回撤", (t.max_drawdown_60d * 100)?.toFixed(1) + "%"],
    ["20日动量", (t.momentum_20d * 100)?.toFixed(2) + "%"],
    ["量比", fmt(t.volume_ratio, 2)],
    ["支撑/压力", `${fmt(t.support_20d)} / ${fmt(t.resistance_20d)}`],
    ["均线排列", t.bull_align ? "多头" : t.bear_align ? "空头" : "缠绕"],
    ["趋势强度", (t.trend_strength * 100)?.toFixed(0) + "%"],
  ];

  return (
    <div className="p-5 space-y-4">
      {/* 头部: 搜索 + 标的名称 */}
      <div className="flex items-center gap-3 flex-wrap">
        <Link to="/watchlist" className="text-gray-400 hover:text-brand-600"><ArrowLeft size={18} /></Link>
        <h1 className="text-lg font-bold text-brand-600">{symbol} {detail?.name || ""}</h1>
        <form className="flex gap-2 ml-auto" onSubmit={(e) => { e.preventDefault(); nav(`/symbol/${searchCode}`); }}>
          <input className="input w-32" placeholder="搜索代码" value={searchCode}
            onChange={(e) => setSearchCode(e.target.value)} />
          <button type="submit" className="btn-primary"><Search size={14} className="inline mr-1" />搜索</button>
        </form>
        <button className="btn-primary" disabled={runScan.isPending || analyzing} onClick={() => runScan.mutate()}>
          <Zap size={14} className="inline mr-1" />{analyzing ? "分析中..." : "立即分析"}
        </button>
        <button
          className={inWatch ? "btn-green" : "btn-ghost"}
          onClick={() => addWatch.mutate()}
          title={inWatch ? "已加入监控, 点击移除" : "加入监控列表"}
        >
          <Star size={14} className={`inline mr-1 ${inWatch ? "fill-current" : ""}`} />
          {inWatch ? "监控中" : "加入自选"}
        </button>
      </div>

      {/* 分析进行中提示 */}
      {analyzing && !scanResult && (
        <div className="card border-amber-200 bg-amber-50/40">
          <div className="flex items-center gap-3 text-sm">
            <span className="badge bg-amber-50 text-amber-700 animate-pulse">分析中</span>
            <span className="text-gray-600">正在调用 15 个 Agent(数据闸门→分析师→多空辩论→首席→交易员→风控→合规→执行), 约需 1-3 分钟...</span>
          </div>
        </div>
      )}

      {/* 分析结果 */}
      {scanResult && (
        <div className="card border-brand-200 bg-brand-50/40">
          <div className="card-title">分析结果 ({scanResult.trace_id})</div>
          <div className="flex gap-3 flex-wrap text-sm">
            <span>研究结论: <b className="text-brand-600">{scanResult.chief?.research_decision || "-"}</b>
              {" "}(置信 {(scanResult.chief?.confidence * 100)?.toFixed(0)}%)</span>
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
          <div className="text-lg font-bold">{q.amount >= 1e8 ? (q.amount / 1e8).toFixed(2) + "亿" : (q.amount / 1e4).toFixed(0) + "万"}</div>
          <div className="text-xs text-gray-500">{(q.volume / 1e8)?.toFixed(2)}亿份</div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">盘口(五档买卖)</div>
          <div className="flex justify-between text-sm mt-1">
            <span className="text-down">卖一 {fmt(ob.ask1)} × {fmt(ob.ask_vol1, 0)}</span>
            <span className="text-up">买一 {fmt(ob.bid1)} × {fmt(ob.bid_vol1, 0)}</span>
          </div>
          <div className="text-xs text-gray-400 mt-1">价差 {(ob.spread * 100)?.toFixed(2)}% · 质量 {detail?.order_book_quality}</div>
        </div>
        <div className="card">
          <div className="text-xs text-gray-500">ETF专项</div>
          <div className="text-lg font-bold">{(detail?.etf_info?.tracking_index || "-")}</div>
          <div className="text-xs text-gray-500">
            溢价 {(q.premium_rate * 100)?.toFixed(2)}% · IOPV {fmt(q.iopv)} · {detail?.etf_info?.is_qdii ? "QDII" : "境内"}
          </div>
        </div>
      </div>

      {/* K线 + 技术指标 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card lg:col-span-2">
          <div className="card-title">日K线(前复权)</div>
          {kline?.candles?.length ? <KlineChart candles={kline.candles} /> : <Empty text="无K线(先执行 fetch-daily)" />}
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

