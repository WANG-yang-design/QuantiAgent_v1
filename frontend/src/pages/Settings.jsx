import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { Mail, Database, Cpu, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { SystemBar } from "../components/Common";

/** 设置: 邮件测试 / 配置摘要 / 数据源健康 */
export default function SettingsPage() {
  const [mailResult, setMailResult] = useState(null);

  const { data: health } = useQuery({ queryKey: ["health"], queryFn: () => api.get("/api/health") });
  const { data: mode } = useQuery({ queryKey: ["sysmode"], queryFn: () => api.get("/api/system/mode") });
  const { data: riskLimits } = useQuery({ queryKey: ["risklimits"], queryFn: () => api.get("/api/risk/limits") });

  const testMail = useMutation({
    mutationFn: async () => {
      setMailResult(null);
      const r = await api.post("/api/email/test?subject=【量化测试】Web管理台邮件验证");
      setMailResult(r);
      return r;
    },
    onError: (e) => setMailResult({ sent: false, error: e.response?.data?.detail || e.message }),
  });

  const items = [
    { icon: Database, label: "数据库", value: health?.db ? "连接正常" : "异常!", ok: !!health?.db },
    { icon: Cpu, label: "LLM 接口", value: "真实模型(DeepSeek)" },
    { icon: Mail, label: "邮件(SMTP)", value: "已启用(QQ 465)" },
  ];

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">设置</h1>
        <SystemBar />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {items.map(({ icon: Icon, label, value, ok }) => (
          <div key={label} className="card flex items-center gap-3">
            <Icon size={22} className={ok === false ? "text-red-500" : "text-brand-600"} />
            <div>
              <div className="text-xs text-gray-500">{label}</div>
              <div className="font-medium text-sm">{value}</div>
            </div>
          </div>
        ))}
      </div>

      {/* 邮件测试 */}
      <div className="card">
        <div className="card-title">邮件测试</div>
        <div className="flex items-center gap-3 flex-wrap">
          <button className="btn-primary" disabled={testMail.isPending} onClick={() => testMail.mutate()}>
            <Mail size={14} className="inline mr-1" />{testMail.isPending ? "发送中..." : "发送测试邮件"}
          </button>
          {mailResult && (
            <span className={`text-sm ${mailResult.sent ? "text-green-600" : "text-red-600"}`}>
              {mailResult.sent ? "发送成功, 请查收邮箱" : "发送失败: " + (mailResult.error || mailResult.detail || "")}
            </span>
          )}
        </div>
        <div className="text-xs text-gray-400 mt-2">
          收件人: 见 .env EMAIL_RECEIVER; 若失败检查 QQ 邮箱授权码(EMAIL_SENDER_PASS)。
        </div>
      </div>

      {/* 风控限额 */}
      <div className="card">
        <div className="card-title">风控限额(只读, 修改见 config/risk_limits.yaml)</div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          {[
            ["最大总仓位", riskLimits?.account_level?.max_total_position],
            ["最大单日亏损", riskLimits?.account_level?.max_daily_loss],
            ["单标的仓位上限", riskLimits?.position_level?.max_single_position],
            ["ETF溢价禁买线", riskLimits?.position_level?.max_premium_rate],
            ["单笔最大金额", riskLimits?.order_level?.max_order_amount],
            ["自动执行上限", riskLimits?.confirmation_policy?.auto_execute?.max_order_amount],
            ["人工确认上限", riskLimits?.confirmation_policy?.email_or_ui_confirm?.max_order_amount],
            ["最大总回撤", riskLimits?.account_level?.max_total_drawdown],
          ].map(([k, v]) => (
            <div key={k} className="border border-gray-100 rounded-lg p-2.5">
              <div className="text-xs text-gray-500">{k}</div>
              <div className="font-semibold">{v != null ? v : "-"}</div>
            </div>
          ))}
        </div>
      </div>

      {/* 操作说明 */}
      <div className="card">
        <div className="card-title">常用操作</div>
        <table className="w-full text-sm">
          <thead><tr><th className="th">操作</th><th className="th">命令</th><th className="th">说明</th></tr></thead>
          <tbody>
            <tr><td className="td font-medium">启动管理台</td><td className="td text-brand-600">python main.py serve</td><td className="td text-gray-500">http://localhost:8080</td></tr>
            <tr><td className="td font-medium">启动调度器</td><td className="td text-brand-600">python main.py scheduler</td><td className="td text-gray-500">行情采集/盘中分析/日报</td></tr>
            <tr><td className="td font-medium">单标的分析</td><td className="td text-brand-600">python main.py scan 510300</td><td className="td text-gray-500">完整15+Agent链路</td></tr>
            <tr><td className="td font-medium">日线回测</td><td className="td text-brand-600">python main.py backtest --start ... --end ...</td><td className="td text-gray-500">或直接在回测中心操作</td></tr>
            <tr><td className="td font-medium">拉取日K</td><td className="td text-brand-600">python main.py fetch-daily --days 400</td><td className="td text-gray-500">新标的先执行此命令</td></tr>
            <tr><td className="td font-medium">日终复盘</td><td className="td text-brand-600">python main.py review</td><td className="td text-gray-500">生成日报+邮件</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
