import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, Activity, CandlestickChart, Bot, FlaskConical,
  PiggyBank, Settings, ListChecks, Pause, Play, ShieldCheck, KeyRound, BarChart3,
  MoreHorizontal, X,
} from "lucide-react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api, setToken, getToken } from "./api/client";
import Dashboard from "./pages/Dashboard";
import Watchlist from "./pages/Watchlist";
import WatchMonitor from "./pages/WatchMonitor";
import SymbolDetail from "./pages/SymbolDetail";
import AgentCenter from "./pages/AgentCenter";
import BacktestCenter from "./pages/BacktestCenter";
import PaperLive from "./pages/PaperLive";
import AccountAnalysis from "./pages/AccountAnalysis";
import SettingsPage from "./pages/Settings";
import { SystemBar } from "./components/Common";

const NAV = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/watchlist", label: "实时盯盘", icon: Activity },
  { to: "/monitor", label: "监控标的", icon: ListChecks },
  { to: "/symbol", label: "标的搜索", icon: CandlestickChart },
  { to: "/agents", label: "Agent决策", icon: Bot },
  { to: "/backtest", label: "回测中心", icon: FlaskConical },
  { to: "/paper-live", label: "模拟盘/实盘", icon: PiggyBank },
  { to: "/analysis", label: "账户分析", icon: BarChart3 },
  { to: "/settings", label: "设置", icon: Settings },
];

/** 手机底部导航(前4个主入口 + 更多抽屉) */
const MAIN_TABS = NAV.slice(0, 3);
const MORE_TABS = NAV.slice(3);

function MobileNav() {
  const [moreOpen, setMoreOpen] = useState(false);
  return (
    <>
      <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-white border-t border-gray-200 flex
        items-stretch pb-[env(safe-area-inset-bottom)]">
        {MAIN_TABS.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} end={to === "/"}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center gap-0.5 py-2 text-[10px] ${
                isActive ? "text-brand-600 font-semibold" : "text-gray-400"
              }`}>
            <Icon size={19} />
            {label}
          </NavLink>
        ))}
        <button className="flex-1 flex flex-col items-center gap-0.5 py-2 text-[10px] text-gray-400"
          onClick={() => setMoreOpen(true)}>
          <MoreHorizontal size={19} />
          更多
        </button>
      </nav>
      {moreOpen && (
        <div className="md:hidden fixed inset-0 z-50 bg-black/40" onClick={() => setMoreOpen(false)}>
          <div className="absolute bottom-0 inset-x-0 bg-white rounded-t-2xl p-4 pb-[max(16px,env(safe-area-inset-bottom))]"
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <div className="text-sm font-bold text-brand-600">更多功能</div>
              <button className="text-gray-400 hover:text-gray-600" onClick={() => setMoreOpen(false)}>
                <X size={18} />
              </button>
            </div>
            <div className="grid grid-cols-3 gap-2">
              {MORE_TABS.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to} end={to === "/"}
                  onClick={() => setMoreOpen(false)}
                  className={({ isActive }) =>
                    `flex flex-col items-center gap-1.5 py-3 rounded-xl border text-xs ${
                      isActive ? "border-brand-600 text-brand-600 bg-brand-50" : "border-gray-100 text-gray-600"
                    }`}>
                  <Icon size={20} />
                  {label}
                </NavLink>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** 鉴权失败弹窗: 401 时提示输入令牌(修复: 原实现无任何用户可见的令牌入口) */
function TokenModal({ open, onClose }) {
  const [val, setVal] = useState(getToken());
  if (!open) return null;
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4"
      onClick={onClose}>
      <div className="card w-full max-w-sm" onClick={(e) => e.stopPropagation()}>
        <div className="card-title"><KeyRound size={15} />API 令牌设置</div>
        <p className="text-xs text-gray-500 mb-3">
          鉴权失败(401)。请在 <code className="bg-gray-100 px-1 rounded">config/config.yaml</code>
          的 <code className="bg-gray-100 px-1 rounded">web.admin_token</code> 配置并重启后端后,
          在这里填入相同的令牌。
        </p>
        <input className="input w-full" value={val} autoFocus
          placeholder="Bearer 令牌" onChange={(e) => setVal(e.target.value)} />
        <div className="flex gap-2 mt-3 justify-end">
          <button className="btn-ghost" onClick={onClose}>取消</button>
          <button className="btn-primary" onClick={() => {
            setToken(val.trim());
            onClose();
            window.location.reload();
          }}>保存并重试</button>
        </div>
      </div>
    </div>
  );
}

/** 顶部工具条: 系统状态 + 调度器状态 + 紧急暂停/恢复 */
function TopBar({ onAuthError }) {
  const [showToken, setShowToken] = useState(false);
  useEffect(() => {
    const h = () => setShowToken(true);
    window.addEventListener("auth-error", h);
    return () => window.removeEventListener("auth-error", h);
  }, []);
  useEffect(() => {
    if (onAuthError) setShowToken(true);
  }, [onAuthError]);

  const { data: mode } = useQuery({
    queryKey: ["sysmode"],
    queryFn: () => api.get("/api/system/mode"),
    refetchInterval: 15000,
  });
  // 调度器存活状态(修复: 用户曾一整天没有决策链产出, 因为调度器窗口被关了)
  const { data: sched } = useQuery({
    queryKey: ["scheduler-status"],
    queryFn: () => api.get("/api/scheduler/status"),
    refetchInterval: 30000,
  });
  const paused = mode?.circuit?.paused;
  const pause = useMutation({
    mutationFn: () => api.post(`/api/emergency/${paused ? "resume" : "pause"}`, null),
    onSuccess: () => window.location.reload(),
  });

  return (
    <>
      <div className="flex items-center gap-3 px-3 md:px-5 py-2 bg-white border-b border-gray-200 shrink-0 flex-wrap">
        <SystemBar />
        <span className="flex items-center gap-1.5 text-xs" title={sched?.running ? `调度器运行中(pid ${sched?.pid})` : "调度器未运行: 启动 Web 会自动拉起; 电脑休眠会暂停所有进程, 唤醒后自动补跑"}>
          <span className={`w-2 h-2 rounded-full ${sched?.running ? "bg-green-500" : "bg-red-500 animate-pulse"}`} />
          <span className={sched?.running ? "text-gray-600" : "text-red-500"}>调度</span>
        </span>
        <div className="ml-auto flex items-center gap-2">
          <button className="btn-ghost text-xs" title="设置访问令牌"
            onClick={() => setShowToken(true)}>
            <KeyRound size={13} className="inline mr-1" /><span className="hidden sm:inline">令牌</span>
          </button>
          <button
            className={paused ? "btn-green text-xs" : "btn-danger text-xs"}
            disabled={pause.isPending}
            onClick={() => {
              if (window.confirm(paused ? "确认恢复全部交易?" : "确认暂停全部交易?")) pause.mutate();
            }}
          >
            {paused ? <Play size={13} className="inline mr-1" /> : <Pause size={13} className="inline mr-1" />}
            <span className="hidden sm:inline">{paused ? "恢复交易" : "紧急暂停"}</span>
            <span className="sm:hidden">{paused ? "恢复" : "暂停"}</span>
          </button>
        </div>
      </div>
      <TokenModal open={showToken} onClose={() => setShowToken(false)} />
    </>
  );
}

export default function App() {
  const [authErrorFlag, setAuthErrorFlag] = useState(0);
  useEffect(() => {
    const h = () => setAuthErrorFlag((n) => n + 1);
    window.addEventListener("auth-error", h);
    return () => window.removeEventListener("auth-error", h);
  }, []);
  return (
    <div className="flex h-screen">
      {/* 桌面侧栏 */}
      <aside className="hidden md:flex w-52 shrink-0 bg-slate-900 text-gray-200 flex-col">
        <div className="px-4 py-5 border-b border-white/10">
          <div className="text-white font-bold text-lg tracking-wide">QuantAgent</div>
          <div className="text-[11px] text-gray-400 mt-0.5">多Agent智能量化交易系统</div>
        </div>
        <nav className="flex-1 py-3 space-y-0.5 overflow-y-auto">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-4 py-2.5 text-sm transition-colors ${
                  isActive ? "bg-brand-600 text-white font-medium" : "hover:bg-white/10"
                }`
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-4 text-[11px] text-gray-500 border-t border-white/10 space-y-1">
          <div className="flex items-center gap-1.5">
            <ShieldCheck size={12} className="text-green-400" />
            模拟盘运行中 · 实盘未接入
          </div>
        </div>
      </aside>
      <main className="flex-1 flex flex-col overflow-y-auto pb-16 md:pb-0">
        <TopBar onAuthError={authErrorFlag} />
        <div className="flex-1">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/watchlist" element={<Watchlist />} />
            <Route path="/monitor" element={<WatchMonitor />} />
            <Route path="/symbol" element={<SymbolDetail />} />
            <Route path="/symbol/:code" element={<SymbolDetail />} />
            <Route path="/agents" element={<AgentCenter />} />
            <Route path="/backtest" element={<BacktestCenter />} />
            <Route path="/paper-live" element={<PaperLive />} />
            <Route path="/analysis" element={<AccountAnalysis />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={
              <div className="p-5 flex flex-col items-center gap-3">
                <div className="text-4xl font-bold text-gray-300">404</div>
                <div className="text-sm text-gray-500">页面不存在</div>
              </div>
            } />
          </Routes>
        </div>
      </main>
      <MobileNav />
    </div>
  );
}
