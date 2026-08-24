import { useState } from "react";
import { useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Mail, Database, Cpu, ShieldAlert, CheckCircle2, XCircle, Clock, PlayCircle } from "lucide-react";
import { api } from "../api/client";
import { SystemBar } from "../components/Common";

/** 设置: 邮件测试 / 配置状态(动态读取) / 调度器状态 / 风控限额 / 常用操作 */
export default function SettingsPage() {
  const qc = useQueryClient();
  const [mailResult, setMailResult] = useState(null);
  const [confirmForm, setConfirmForm] = useState({ timeout_action: "cancel", timeout_seconds: 600 });

  const { data: health } = useQuery({ queryKey: ["health"], queryFn: () => api.get("/api/health") });
  const { data: info } = useQuery({ queryKey: ["settings-info"], queryFn: () => api.get("/api/settings/info") });
  const { data: riskLimits } = useQuery({ queryKey: ["risklimits"], queryFn: () => api.get("/api/risk/limits") });
  const { data: sched } = useQuery({ queryKey: ["scheduler-status"], queryFn: () => api.get("/api/scheduler/status"), refetchInterval: 20000 });
  const { data: mailStatus } = useQuery({
    queryKey: ["email-status"], queryFn: () => api.get("/api/email/status"),
    refetchInterval: 10000,
  });
  const { data: confirmPolicy } = useQuery({
    queryKey: ["confirmation-settings"], queryFn: () => api.get("/api/confirmations/settings"),
    onSuccess: (r) => setConfirmForm(r),
  });
  useEffect(() => { if (confirmPolicy) setConfirmForm(confirmPolicy); }, [confirmPolicy]);
  const saveConfirmPolicy = useMutation({
    mutationFn: () => api.put("/api/confirmations/settings", confirmForm),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["confirmation-settings"] }); window.alert("确认超时设置已保存，新建确认单立即生效"); },
    onError: (e) => window.alert("保存失败: " + (e.response?.data?.detail || e.message)),
  });

  // 手动拉起调度器(独立进程, 单例锁保证不重复)
  const startSched = useMutation({
    mutationFn: () => api.post("/api/scheduler/start"),
    onSuccess: () => {
      window.alert("调度器启动命令已发送, 约10秒后刷新本页查看状态。");
      qc.invalidateQueries({ queryKey: ["scheduler-status"] });
    },
    onError: (e) => window.alert("启动失败: " + (e.response?.data?.detail || e.message)),
  });

  const testMail = useMutation({
    mutationFn: async () => {
      setMailResult(null);
      const r = await api.post("/api/email/test?subject=【量化测试】Web管理台邮件验证");
      setMailResult(r);
      return r;
    },
    onError: (e) => setMailResult({ sent: false, error: e.response?.data?.detail || e.message }),
  });

  // 修复: 原实现硬编码"LLM接口=真实模型(DeepSeek)/邮件=已启用(QQ 465)",
  // 实际配置变化后页面仍显示旧信息误导用户 —— 全部改为后端动态状态。
  const llm = info?.llm || {};
  const email = info?.email || {};
  const items = [
    { icon: Database, label: "数据库", value: health?.db ? "连接正常" : "异常!", ok: !!health?.db },
    {
      icon: Cpu, label: "LLM 接口",
      value: llm.configured
        ? (llm.mock_mode ? `已配置(当前模拟模式) · ${llm.deep_model || "-"}` : `已配置 · ${llm.deep_model || "-"}`)
        : "未配置(运行在规则模拟模式)",
      ok: !!llm.configured,
    },
    {
      icon: Mail, label: "邮件(SMTP)",
      value: email.enabled ? `已启用(${email.smtp_host || "-"}:${email.smtp_port || "-"})` : "未配置",
      ok: !!email.enabled,
    },
  ];

  // Agent 开关(成本控制: 关闭的分析师不再调用 LLM, 决策链路用规则占位保持完整)
  const { data: agentCfg } = useQuery({ queryKey: ["agents-config"], queryFn: () => api.get("/api/agents/config") });
  const agentMasterEnabled = agentCfg?.master_enabled !== false;
  const toggleAgentMaster = useMutation({
    mutationFn: (enabled) => api.post("/api/agents/config/master", { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents-config"] }),
    onError: (e) => window.alert("切换失败: " + (e.response?.data?.detail || e.message)),
  });
  const toggleAgent = useMutation({
    mutationFn: ({ agent, enabled }) => api.post("/api/agents/config", { agent, enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents-config"] }),
    onError: (e) => window.alert("切换失败: " + (e.response?.data?.detail || e.message)),
  });
  // 真实 token 用量(修复: 输出token是成本大头, 逐Agent统计)
  const { data: usage } = useQuery({
    queryKey: ["agents-usage"],
    queryFn: () => api.get("/api/agents/usage", { days: 7 }),
    refetchInterval: 120000,
  });

  return (
    <div className="p-3 md:p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-bold text-brand-600">设置</h1>
        <SystemBar />
      </div>

      {/* 安全提示 */}
      {info?.admin_token_default && (
        <div className="card border-red-200 bg-red-50/50 text-sm text-red-600 flex items-start gap-2">
          <ShieldAlert size={15} className="shrink-0 mt-0.5" />
          <span>管理令牌未安全配置，后端会拒绝启动。
            请在 <code className="bg-red-100 px-1 rounded">.env</code> 中设置
            <code className="bg-red-100 px-1 rounded">WEB_ADMIN_TOKEN</code> 和
            <code className="bg-red-100 px-1 rounded">WEB_CONFIRM_SECRET</code>。</span>
        </div>
      )}

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

      {/* Agent 开关 + 智能扫描(成本控制, 修复: 15个Agent无条件全量参与) */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span><Cpu size={14} className="inline mr-1" />Agent 开关(成本控制)</span>
          <span className="text-[11px] text-gray-400 font-normal">运行时生效，无需重启</span>
        </div>
        <div className={`rounded-lg border p-3 mb-3 flex items-center justify-between gap-3 ${agentMasterEnabled ? "border-green-200 bg-green-50/60" : "border-gray-200 bg-gray-50"}`}>
          <div>
            <div className={`text-sm font-semibold ${agentMasterEnabled ? "text-green-700" : "text-gray-600"}`}>
              Agent 系统{agentMasterEnabled ? "已启用" : "已停用"}
            </div>
            <div className="text-xs text-gray-500 mt-0.5">
              {agentMasterEnabled
                ? "自动与手动分析可以调用 LLM；下方可继续控制单个 Agent。"
                : "自动扫描和手动分析均直接跳过，不调用 LLM；策略轮动、行情和硬风控继续运行。"}
            </div>
          </div>
          <button
            className={agentMasterEnabled ? "btn-danger shrink-0" : "btn-green shrink-0"}
            disabled={toggleAgentMaster.isPending}
            onClick={() => toggleAgentMaster.mutate(!agentMasterEnabled)}>
            {toggleAgentMaster.isPending ? "切换中…" : agentMasterEnabled ? "停用全部 Agent" : "启用 Agent"}
          </button>
        </div>
        <div className={`flex flex-wrap gap-1.5 mb-2 ${agentMasterEnabled ? "" : "opacity-50"}`}>
          {(agentCfg?.agents || []).map((a) => (
            <button key={a.agent}
              className={`badge ${a.required ? "bg-slate-100 text-slate-500 cursor-not-allowed" : a.enabled ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-400"}`}
              disabled={!agentMasterEnabled || a.required || toggleAgent.isPending}
              title={!agentMasterEnabled ? "请先启用 Agent 总开关" : a.required ? "Agent运行时必须启用(决策链路依赖)" : a.enabled ? "点击关闭(省token)" : "点击启用"}
              onClick={() => toggleAgent.mutate({ agent: a.agent, enabled: !a.enabled })}>
              {a.label}
              {a.required ? " 必开" : a.enabled ? " ✓" : " ✕"}
            </button>
          ))}
        </div>
        <div className="text-xs text-gray-500 space-y-1">
          <div>· 总开关关闭后不会生成规则模拟结论，避免 API 故障时产生误导性的“买入候选”</div>
          <div>· 当前为影子模式：仅数据闸门/首席研究员必开；Agent不会创建或阻止订单，策略独立交易</div>
          <div>· 建议: ETF 场景可关闭「基本面分析师」(无数据)、保守可关「情绪/资金流分析师」</div>
          <div>· 智能扫描(smart_scan): 持仓标的+近3日轮动交易标的每轮必分析; 其余监控标的每4轮轮询一次(每轮≤8只), 避免20只标的每30分钟全量跑11个Agent</div>
        </div>
      </div>

      {/* 真实 token 用量(成本审计) */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span><Cpu size={14} className="inline mr-1" />LLM Token 用量(近{usage?.days ?? 7}天, 真实计费值)</span>
          <span className="text-[11px] text-gray-400 font-normal">
            {usage?.total ? `共 ${usage.total.calls} 次 · 输入 ${(usage.total.prompt_tokens / 10000).toFixed(1)}万 · 输出 ${(usage.total.completion_tokens / 10000).toFixed(1)}万` : "暂无数据(重启后开始统计)"}
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px]">
            <thead><tr>
              <th className="th">Agent</th><th className="th">调用</th>
              <th className="th">输入token</th><th className="th">输出token</th><th className="th">单次输出</th>
            </tr></thead>
            <tbody>
              {(usage?.by_agent || []).map((a) => (
                <tr key={a.agent}>
                  <td className="td font-medium">{a.agent}</td>
                  <td className="td">{a.calls}</td>
                  <td className="td">{(a.prompt_tokens / 10000).toFixed(2)}万</td>
                  <td className={`td font-semibold ${a.completion_tokens > 400000 ? "text-down" : ""}`}>
                    {(a.completion_tokens / 10000).toFixed(2)}万
                  </td>
                  <td className="td text-gray-500">{Math.round(a.completion_tokens / Math.max(a.calls, 1))}</td>
                </tr>
              ))}
              {!(usage?.by_agent || []).length && (
                <tr><td className="td text-gray-400" colSpan="5">重启服务后开始统计(输出token是成本大头, 已在Prompt中限幅+max_tokens收紧)</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="text-[11px] text-gray-400 mt-1">
          数据来自每次调用的 completion_tokens/prompt_tokens(OpenAI usage 字段)落库; 输出token是费用大头, 已通过 ①输出限幅Prompt ②max_tokens 2048→1024 ③分析师开关 ④智能扫描 四层削减。
        </div>
      </div>

      {/* 调度器状态(修复: 曾有一整天没有任何 Agent 决策链产出——调度器没在跑) */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span><Clock size={14} className="inline mr-1" />调度器状态</span>
          {sched?.running && (
            <span className="badge bg-green-50 text-green-600">运行中 (pid {sched.pid})</span>
          )}
        </div>
        {sched?.running ? (
          <div className="text-sm space-y-1.5">
            <div className="text-xs text-gray-500 mb-1">
              心跳: {sched.heartbeat?.ts || "-"} · 例行任务: 每30分钟扫描动态策略池生成影子观点、收盘后更新1/3/5/10日归因、持仓硬风控与实时行情采集
            </div>
            <div className="flex flex-wrap gap-1.5">
              {(sched.heartbeat?.jobs || []).map((j) => (
                <span key={j.name}
                  className={`badge ${j.status === "FAILED" ? "bg-red-50 text-red-600" : "bg-gray-50 text-gray-600"}`}
                  title={`状态: ${j.status || "PENDING"}\n上次完成: ${j.last_finished || "-"}\n下次运行: ${j.next_run}${j.error ? `\n错误: ${j.error}` : ""}`}>
                  {j.name} <span className="text-gray-400">({j.status || "PENDING"} · {j.next_run})</span>
                </span>
              ))}
            </div>
            <div className="text-xs text-gray-400 mt-2">
              调度器由 Web 自动内嵌启动(单例锁), 无需手动维护; 也可 <code className="bg-gray-100 px-1 rounded">python main.py scheduler</code> 独立运行。
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-sm text-red-500">调度器未运行</span>
            <button className="btn-primary" disabled={startSched.isPending} onClick={() => startSched.mutate()}>
              <PlayCircle size={14} className="inline mr-1" />{startSched.isPending ? "启动中..." : "启动调度器"}
            </button>
            <span className="text-xs text-gray-400">启动后约10秒内就位(重启 Web 也会自动拉起)</span>
          </div>
        )}
        <div className="mt-2 text-xs text-amber-600 bg-amber-50 rounded-lg px-3 py-2">
          注意: 电脑休眠/锁屏会挂起所有进程(调度器与 Web 都会暂停)。长时间无人值守运行请把
          电源设置改为「睡眠: 从不」; 休眠错过的时间窗口在唤醒后会自动补跑(1小时容错)。
        </div>
      </div>

      {/* 系统与数据源状态 */}
      <div className="card">
        <div className="card-title">系统与数据源(动态)</div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
          <div className="border border-gray-100 rounded-lg p-2.5">
            <div className="text-xs text-gray-500">系统</div>
            <div className="font-semibold">V{info?.version} · {info?.trade_mode === "paper" ? "模拟盘" : info?.trade_mode} · {info?.timezone}</div>
            <div className="text-xs text-gray-400">初始资金 ¥{fmtNum(info?.initial_cash)}</div>
          </div>
          <div className="border border-gray-100 rounded-lg p-2.5">
            <div className="text-xs text-gray-500">LLM 模型</div>
            <div className="font-semibold">{llm.fast_model || "-"} / {llm.deep_model || "-"}</div>
            <div className="text-xs text-gray-400">Embedding: {llm.embedding_model || "-"}</div>
          </div>
          <div className="border border-gray-100 rounded-lg p-2.5">
            <div className="text-xs text-gray-500">邮件</div>
            <div className="font-semibold">{email.sender || "-"}</div>
            <div className="text-xs text-gray-400">收件: {email.receiver || "-"}</div>
          </div>
        </div>
        <div className="mt-3">
          <div className="text-xs text-gray-500 mb-1.5">数据源容灾链(主源 → 备源)</div>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(info?.data_sources || {}).map(([cat, v]) => (
              <span key={cat} className="badge bg-gray-50 text-gray-600" title={`${v.primary} → ${(v.backups || []).join(", ")}`}>
                {cat}: <b>{v.primary}</b>{(v.backups || []).length ? `→${v.backups.join(",")}` : ""}
              </span>
            ))}
          </div>
          <div className="text-xs text-gray-400 mt-2">配置见 config/data_sources.yaml; 主源失败自动切换备源, 全部失败触发数据质量拦截。</div>
        </div>
      </div>

      {/* 邮件测试 */}
      <div className="card">
        <div className="card-title flex items-center justify-between">
          <span>邮件发送状态与测试</span>
          <span className={`badge ${mailStatus?.worker_alive ? "bg-green-50 text-green-600" : "bg-red-50 text-red-600"}`}>
            {mailStatus?.worker_alive ? "发送线程正常" : "发送线程异常"} · 队列 {mailStatus?.queue_size ?? "-"}
          </span>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <button className="btn-primary" disabled={testMail.isPending} onClick={() => testMail.mutate()}>
            <Mail size={14} className="inline mr-1" />{testMail.isPending ? "发送中..." : "发送测试邮件"}
          </button>
          {mailResult && (
            <span className={`text-sm flex items-center gap-1 ${mailResult.sent ? "text-green-600" : "text-red-600"}`}>
              {mailResult.sent ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
              {mailResult.sent ? "发送成功, 请查收邮箱" : "发送失败: " + (mailResult.error || mailResult.detail || "")}
            </span>
          )}
        </div>
        <div className="text-xs text-gray-400 mt-2">
          收件人: 见 .env EMAIL_RECEIVER; 若失败检查 SMTP 授权码(EMAIL_SENDER_PASS)。
        </div>
        <div className="mt-3 overflow-x-auto max-h-52 overflow-y-auto">
          <table className="w-full min-w-[680px]">
            <thead><tr><th className="th">创建时间</th><th className="th">邮件标题</th><th className="th">状态</th><th className="th">尝试</th><th className="th">更新时间</th></tr></thead>
            <tbody>{(mailStatus?.recent || []).map((m) => (
              <tr key={m.id}><td className="td text-xs">{(m.created_at || "").slice(0, 19)}</td>
                <td className="td text-xs font-medium">{m.subject}</td>
                <td className="td"><span className={`badge ${m.status === "SENT" ? "bg-green-50 text-green-600" : m.status === "FAILED" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-700"}`}>{m.status}</span></td>
                <td className="td">{m.attempts}</td><td className="td text-xs text-gray-400">{(m.updated_at || "").slice(0, 19)}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>

      {/* 风控限额 */}
      <div className="card">
        <div className="card-title">人工确认超时设置</div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-gray-500">超时后动作
            <select className="input block mt-1" value={confirmForm.timeout_action}
              onChange={(e) => setConfirmForm({ ...confirmForm, timeout_action: e.target.value })}>
              <option value="cancel">自动撤销（推荐）</option>
              <option value="execute">自动执行（重新取价并复检风控）</option>
            </select>
          </label>
          <label className="text-xs text-gray-500">等待分钟数
            <input className="input block mt-1 w-32" type="number" min="1" max="1440"
              value={Math.round(confirmForm.timeout_seconds / 60)}
              onChange={(e) => setConfirmForm({ ...confirmForm, timeout_seconds: Math.max(60, Number(e.target.value || 1) * 60) })} />
          </label>
          <button className="btn-primary" disabled={saveConfirmPolicy.isPending}
            onClick={() => saveConfirmPolicy.mutate()}>保存确认设置</button>
        </div>
        <div className="text-xs text-gray-400 mt-2">设置只固化到新建确认单；已存在的确认单继续按创建时策略处理，确保审计结果不会被事后改写。默认自动撤销更安全。</div>
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
        <div className="overflow-x-auto">
        <table className="w-full min-w-[640px]">
          <thead><tr><th className="th">操作</th><th className="th">命令</th><th className="th">说明</th></tr></thead>
          <tbody>
            <tr><td className="td font-medium">启动管理台</td><td className="td text-brand-600">python main.py serve</td><td className="td text-gray-500">http://localhost:8080</td></tr>
            <tr><td className="td font-medium">启动调度器</td><td className="td text-brand-600">python main.py scheduler</td><td className="td text-gray-500">行情采集/盘中分析/日报/持仓巡检</td></tr>
            <tr><td className="td font-medium">单标的分析</td><td className="td text-brand-600">python main.py scan 510300</td><td className="td text-gray-500">Agent影子研究链路（不下单）</td></tr>
            <tr><td className="td font-medium">导入真实持仓</td><td className="td text-brand-600">python main.py init-portfolio --file data/portfolio_init.json</td><td className="td text-gray-500">重置账户并写入真实持仓(盈亏自动按持仓成本计算)</td></tr>
            <tr><td className="td font-medium">日线回测</td><td className="td text-brand-600">python main.py backtest --start ... --end ...</td><td className="td text-gray-500">或直接在回测中心操作</td></tr>
            <tr><td className="td font-medium">拉取日K</td><td className="td text-brand-600">python main.py fetch-daily --days 400</td><td className="td text-gray-500">新标的先执行此命令</td></tr>
            <tr><td className="td font-medium">日终复盘</td><td className="td text-brand-600">python main.py review</td><td className="td text-gray-500">生成日报+邮件+账户快照</td></tr>
            <tr><td className="td font-medium">运行测试套件</td><td className="td text-brand-600">python -m tests.test_suite</td><td className="td text-gray-500">技术指标/撮合/风控/回测(使用独立测试账户, 不影响真实模拟盘)</td></tr>
          </tbody>
        </table>
        </div>
      </div>
    </div>
  );
}

function fmtNum(v) {
  if (v == null) return "-";
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
