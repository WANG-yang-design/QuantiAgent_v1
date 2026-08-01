import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, Activity, CandlestickChart, Bot, FlaskConical,
  PiggyBank, Settings,
} from "lucide-react";
import Dashboard from "./pages/Dashboard";
import Watchlist from "./pages/Watchlist";
import SymbolDetail from "./pages/SymbolDetail";
import AgentCenter from "./pages/AgentCenter";
import BacktestCenter from "./pages/BacktestCenter";
import PaperLive from "./pages/PaperLive";
import SettingsPage from "./pages/Settings";

const NAV = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/watchlist", label: "实时盯盘", icon: Activity },
  { to: "/symbol/510300", label: "标的详情", icon: CandlestickChart },
  { to: "/agents", label: "Agent决策", icon: Bot },
  { to: "/backtest", label: "回测中心", icon: FlaskConical },
  { to: "/paper-live", label: "模拟盘/实盘", icon: PiggyBank },
  { to: "/settings", label: "设置", icon: Settings },
];

function Sidebar() {
  const navigate = useNavigate();
  return (
    <aside className="w-52 shrink-0 bg-brand-900 text-gray-200 flex flex-col">
      <div className="px-4 py-5 border-b border-white/10">
        <div className="text-white font-bold text-lg">QuantAgent V1</div>
        <div className="text-xs text-gray-400 mt-0.5">多Agent量化交易系统</div>
      </div>
      <nav className="flex-1 py-3 space-y-0.5 overflow-y-auto">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
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
      <div className="px-4 py-4 text-[11px] text-gray-500 border-t border-white/10">
        模拟盘运行中 · 实盘未接入
      </div>
    </aside>
  );
}

export default function App() {
  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/watchlist" element={<Watchlist />} />
          <Route path="/symbol/:code" element={<SymbolDetail />} />
          <Route path="/agents" element={<AgentCenter />} />
          <Route path="/backtest" element={<BacktestCenter />} />
          <Route path="/paper-live" element={<PaperLive />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  );
}
