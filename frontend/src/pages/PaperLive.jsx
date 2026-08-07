import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Pause, Play, XCircle, Check, X, Info, ShieldAlert, Activity, Wallet, ChevronDown } from "lucide-react";
import { api } from "../api/client";
import { SystemBar, fmt, fmtWan, Empty, Spin } from "../components/Common";

/** 模拟盘/实盘: 运行模式状态 + 控制 + 持仓明细 + 限额 + 人工确认 */
export default function PaperLive() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const { data: mode, isLoading } = useQuery({
    queryKey: ["sysmode"],
    queryFn: () => api.get("/api/system/mode"),
    refetchInterval: 10000,
  });
  const { data: equity } = useQuery({ queryKey: ["equity"], queryFn: () => api.get("/api/equity?limit=500") });
  // 持仓明细(修复: 原页面只有汇总数字, 看不到持仓详细情况)
  const { data: positions } = useQuery({
    queryKey: ["positions"],
    queryFn: () => api.get("/api/positions"),
    refetchInterval: 10000,
  });
  const { data: trades } = useQuery({
    queryKey: ["trades"],
    queryFn: () => api.get("/api/trades?limit=30"),
    refetchInterval: 15000,
  });

  const pause = useMutation({
    mutationFn: () => api.post("/api/emergency/pause?reason=paper-live"),
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["sysmode"] }); qc.invalidateQueries({ queryKey: ["orders"] }); },
    onError: (e) => window.alert("撤单失败: " + (e.response?.data?.detail || e.message)),
  });
  const decide = useMutation({
    mutationFn: ({ id, ok }) => api.post(`/api/confirmations/${id}/decide`, { approved: ok, note: "web" }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["sysmode"] });
      // 批准后自动恢复下单; 非 ORDERED/REJECTED 的结果提示用户
      if (r?.status && !["ORDERED", "REJECTED"].includes(r.status)) {
        window.alert(`确认处理结果: ${r.status} ${r.reason || ""}`);
      }
    },
    onError: (e) => window.alert("确认处理失败: " + (e.response?.data?.detail || e.message)),
  });

  // 持仓风控巡检
  const { data: pm } = useQuery({
    queryKey: ["position-monitor"],
    queryFn: () => api.get("/api/risk/position-monitor"),
    refetchInterval: 30000,
  });
  const [pmResult, setPmResult] = useState(null);
  const runPm = useMutation({
    mutationFn: () => api.post("/api/risk/position-monitor/run"),
    onSuccess: (r) => setPmResult(r),
  });
  // 最近成交展开/收起(修复: 原实现一次性全铺, 无折叠)
  const [showAllTrades, setShowAllTrades] = useState(false);

  if (isLoading) return <div className="p-5"><Spin /></div>;
  const acc = mode?.account || {};
  const today = mode?.today || {};
  const orderPct = Math.min(100, (today.order_count / (today.max_order_count || 1)) * 100);
  const amountPct = Math.min(100, (today.order_amount / (today.max_order_amount || 1)) * 100);

  return (
    <div className="p-3 md:p-5 space-y-4">
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
        <button className="btn-danger" disabled={pause.isPending} onClick={() => {
          if (window.confirm("确认暂停全部交易?")) pause.mutate();
        }}>
          <Pause size={14} className="inline mr-1" />一键暂停交易
        </button>
        <button className="btn-green" disabled={resume.isPending} onClick={() => resume.mutate()}>
          <Play size={14} className="inline mr-1" />恢复交易
        </button>
        <button className="btn-danger" disabled={cancelAll.isPending} onClick={() => {
          if (window.confirm("确认撤销全部未成交委托? 该操作不可恢复。")) cancelAll.mutate();
        }}>
          <XCircle size={14} className="inline mr-1" />撤销全部未成交委托
        </button>
          {/* 持仓风控巡检按钮(修复: 原在页面最底部, 用户找不到, 移到控制区) */}
          <button className="btn-primary" disabled={runPm.isPending} onClick={() => runPm.mutate()}>
            <Activity size={14} className="inline mr-1" />{runPm.isPending ? "巡检中..." : "持仓风控巡检"}
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

      {/* 持仓风控巡检(移到上部, 常驻可见) */}
      <div className="card">
        <div className="card-title"><ShieldAlert size={14} />持仓风控巡检(硬性止损, 不依赖Agent及时性)</div>
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <span className="text-xs text-gray-500">
            自动巡检: {pm?.config?.check_interval_seconds ? `${pm.config.check_interval_seconds / 60}分钟/次` : "-"} ·
            硬止损 {((pm?.config?.stop_loss_pct ?? 0.08) * 100).toFixed(0)}% · 移动止盈 {((pm?.config?.trailing_stop_pct ?? 0.08) * 100).toFixed(0)}% ·
            <b>仅个股止损/止盈</b>(已移除市场降仓, 大盘状态只作Agent参考) ·
            {pm?.config?.auto_execute ? " 自动执行" : " 仅告警"}
            <span className="ml-1 text-gray-400">(阈值修改见 config/risk_limits.yaml position_monitor)</span>
          </span>
          {pm?.config?.auto_execute && (
            <span className="w-full text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1">
              自动执行已开启: 仅当个股触发硬止损(浮亏超{(pm?.config?.stop_loss_pct ?? 0.08) * 100}%)或移动止盈(从最高回撤{(pm?.config?.trailing_stop_pct ?? 0.08) * 100}%)时自动卖出,
              同一标的同一天只执行一次; 市场涨跌不会自动卖股。
              (仪表盘"最近订单"来源显示为"风控巡检"; 如需仅告警不下单, 将
              config/risk_limits.yaml 的 position_monitor.auto_execute 改为 false)
            </span>
          )}
        </div>
        {pmResult && (
          <div className="text-sm">
            <div className="text-xs text-gray-500 mb-1">巡检结果: 检查 {pmResult.checked ?? 0} 只持仓 · 触发 {(pmResult.triggered || []).length} · 执行 {(pmResult.executed || []).length} · 跳过 {(pmResult.skipped || []).length}</div>
            {(pmResult.executed || []).map((e, i) => (
              <div key={i} className="flex items-center gap-2 border border-red-200 bg-red-50/50 rounded-lg px-3 py-1.5 mb-1">
                <span className="badge bg-red-50 text-red-600">{e.type}</span>
                <span className="font-medium">{e.symbol}</span>
                <span>卖出 {e.qty}份 @ {fmt(e.price)}</span>
                <span className="text-xs text-gray-500 truncate flex-1">{e.reason}</span>
                <span className="text-[10px] text-gray-400">{e.order_id}</span>
              </div>
            ))}
            {(pmResult.triggered || []).filter((t) => !(pmResult.executed || []).some((e) => e.symbol === t.symbol)).map((t, i) => (
              <div key={`t${i}`} className="flex items-center gap-2 border border-amber-200 bg-amber-50/50 rounded-lg px-3 py-1.5 mb-1">
                <span className="badge bg-amber-50 text-amber-700">{t.type}触发(未执行)</span>
                <span className="font-medium">{t.symbol}</span>
                <span className="text-xs text-gray-500 truncate">{t.reason}</span>
              </div>
            ))}
            {(pmResult.skipped || []).map((s, i) => (
              <div key={`s${i}`} className="text-xs text-gray-400">跳过: {s}</div>
            ))}
            {!(pmResult.triggered || []).length && <div className="text-xs text-green-600">持仓健康, 无触发</div>}
          </div>
        )}
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

      {/* 持仓明细(用户核心诉求: 必须能看到持仓详细情况) */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span><Wallet size={14} className="inline mr-1" />持仓明细 ({(positions || []).length})</span>
          <span className="text-xs text-gray-400 font-normal">点击持仓跳转标的详情 · 10秒自动刷新</span>
        </div>
        {(positions || []).length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[800px]">
              <thead>
                <tr>
                  <th className="th">代码</th><th className="th">名称</th>
                  <th className="th">总数量</th><th className="th">可用(T+1)</th>
                  <th className="th">成本价</th><th className="th">现价</th>
                  <th className="th">市值</th><th className="th">浮盈亏</th><th className="th">盈亏率</th>
                </tr>
              </thead>
              <tbody>
                {(positions || []).map((p) => {
                  const pnl = p.pnl || 0;
                  return (
                    <tr key={p.symbol} className="cursor-pointer hover:bg-gray-50"
                      onClick={() => nav(`/symbol/${p.symbol}`)}>
                      <td className="td font-medium">{p.symbol}</td>
                      <td className="td text-gray-500">{p.name || "-"}</td>
                      <td className="td">{p.total_qty}</td>
                      <td className="td">
                        {p.available_qty}
                        {p.today_buy_qty > 0 && (
                          <span className="badge bg-amber-50 text-amber-700 ml-1 ml-1" title="今日买入T+1锁定">T+1 {p.today_buy_qty}</span>
                        )}
                      </td>
                      <td className="td">{fmt(p.cost_price)}</td>
                      <td className="td font-semibold">{fmt(p.latest_price)}</td>
                      <td className="td">{fmt(p.market_value, 2)}</td>
                      <td className={`td font-semibold ${pnl >= 0 ? "text-up" : "text-down"}`}>
                        {pnl >= 0 ? "+" : ""}{fmt(pnl, 2)}
                      </td>
                      <td className={`td font-semibold ${pnl >= 0 ? "text-up" : "text-down"}`}>
                        {(p.pnl_pct ?? 0) >= 0 ? "+" : ""}{(p.pnl_pct ?? 0) * 100}%
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : <Empty text="暂无持仓(有交易后自动出现)" />}
      </div>

      {/* 人工确认队列(修复: 移到最近成交上方, 待确认事项优先可见) */}
      <div className="card">
        <div className="card-title">人工确认队列 ({(mode?.confirmations || []).length})</div>
        {(mode?.confirmations || []).length ? (
          <div className="space-y-2">
            {(mode?.confirmations || []).map((c) => (
              <div key={c.confirm_id} className="flex items-start gap-3 border border-amber-200 bg-amber-50/50 rounded-lg px-3 py-2">
                <span className={`badge shrink-0 mt-0.5 ${c.risk_level === "HIGH" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-700"}`}>{c.risk_level}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium">{c.symbol} {c.name && <span className="text-gray-500">{c.name}</span>}</span>
                    <span className="text-sm font-semibold">{c.action}</span>
                    <span className="text-sm text-gray-600">¥{fmt(c.amount, 0)}</span>
                    <span className="text-[10px] text-gray-400 ml-auto">{c.created_at}</span>
                  </div>
                  {/* 分析结果/原因(修复: 原只显示一行截断的 reason, 看不到分析依据) */}
                  <pre className="mt-1.5 text-[11px] text-gray-600 bg-white/60 rounded px-2 py-1.5 whitespace-pre-wrap max-h-40 overflow-y-auto">{c.reason}</pre>
                  <div className="flex gap-2 mt-2">
                    <button className="btn-green" onClick={() => {
                      if (window.confirm("确认批准该交易计划并提交模拟盘订单?")) decide.mutate({ id: c.confirm_id, ok: true });
                    }}>
                      <Check size={14} className="inline mr-1" />批准
                    </button>
                    <button className="btn-danger" onClick={() => {
                      if (window.confirm("确认拒绝该交易计划?")) decide.mutate({ id: c.confirm_id, ok: false });
                    }}>
                      <X size={14} className="inline mr-1" />拒绝
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : <Empty text="无待确认交易(交易员标注需人工确认或中高风险时出现, 附完整分析原因)" />}
      </div>

      {/* 今日成交 */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span>最近成交 ({(trades || []).length})</span>
          {/* 修复: 成交列表没有展开收起功能, 一页铺满几十条 */}
          {(trades || []).length > 10 && (
            <button className="btn-ghost text-xs" onClick={() => setShowAllTrades(!showAllTrades)}>
              {showAllTrades ? "收起" : `展开全部(${(trades || []).length}条)`}
              <ChevronDown size={13} className={`inline ml-1 transition-transform ${showAllTrades ? "rotate-180" : ""}`} />
            </button>
          )}
        </div>
        {(trades || []).length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px]">
              <thead>
                <tr>
                  <th className="th">时间</th><th className="th">代码</th><th className="th">名称</th>
                  <th className="th">方向</th><th className="th">价格</th><th className="th">数量</th>
                  <th className="th">手续费</th><th className="th">盈亏</th>
                </tr>
              </thead>
              <tbody>
                {(showAllTrades ? trades : trades.slice(0, 10)).map((t) => {
                  // 清仓标注(修复): 该标的当前无持仓的卖出成交标记为清仓
                  const closed = t.side === "SELL" && !(positions || []).some((p) => p.symbol === t.symbol);
                  const pnl = t.pnl != null ? Number(t.pnl) : null;
                  return (
                    <tr key={t.trade_id} className="cursor-pointer hover:bg-gray-50"
                      onClick={() => nav(`/symbol/${t.symbol}`)}>
                      <td className="td text-gray-500">{(t.trade_time || "").slice(0, 19)}</td>
                      <td className="td font-medium">{t.symbol}</td>
                      <td className="td text-gray-500">{t.name || "-"}</td>
                      <td className="td">
                        <span className={`badge ${t.side === "BUY" ? "bg-red-50 text-up" : "bg-green-50 text-down"}`}>
                          {t.side === "BUY" ? "买入" : "卖出"}
                        </span>
                        {closed && <span className="badge bg-purple-50 text-purple-600 ml-1" title="该标的已全部卖出">清仓</span>}
                      </td>
                      <td className="td">{fmt(t.price)}</td>
                      <td className="td">{t.qty}</td>
                      <td className="td text-gray-500">{fmt(t.fee, 2)}</td>
                      <td className={`td font-semibold ${pnl == null ? "text-gray-400" : pnl >= 0 ? "text-up" : "text-down"}`}>
                        {pnl == null ? "-" : `${pnl >= 0 ? "+" : ""}${fmt(pnl, 2)}`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : <Empty text="暂无成交" />}
      </div>
    </div>
  );
}

