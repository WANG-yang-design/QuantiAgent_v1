# 多Agent智能量化交易系统 V1

按《多Agent量化交易系统技术方案文档 V1》实现的完整量化交易系统底座：
数据系统 + 15 Agent 投研决策 + 五层风控 + 模拟盘 + 回测 + 报告通知 + 实盘预留。

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.13 | venv 见 `.venv` |
| PostgreSQL | 16 | 本机已装, 库 `quantiagent`, 用户 `quantiagent/quantiagent` |
| pgvector | 0.8.6 | 已编译安装(向量检索) |

## 快速开始

```bash
# 1. 配置密钥(复制 .env.example 为 .env 并填写)
#    - LLM: OpenAI 兼容接口(base_url/api_key/快慢模型名), 不填则自动进入"规则模拟"模式
#    - 邮件: SMTP 配置(QQ邮箱 465)
cp .env.example .env

# 2. 初始化数据库(建表+种子数据)
python main.py init-db

# 3. 拉取数据
python main.py fetch-symbols                 # 1560只ETF池
python main.py fetch-daily --days 400        # 日K入库

# 4. 单标的完整Agent分析(数据闸门→7分析师→多空辩论→首席→交易员→风控→合规→执行)
python main.py scan 510300

# 5. 回测(日线, 动量轮动, 含沪深300基准对比)
python main.py backtest --start 2025-06-01 --end 2026-07-31

# 6. Web 管理台(仪表盘/实时盯盘/标的详情/Agent决策/回测中心/模拟盘实盘/设置)
python main.py serve          # http://localhost:8080  (Bearer token: quantiagent-admin)

# 7. 调度器(交易日历/行情/新闻/Agent常规分析/日报)
python main.py scheduler
```

前端为 React 工程(`frontend/`, Vite+Tailwind+ECharts), 构建产物由后端托管:
- 修改前端: `cd frontend && npm run dev`(热更新, 5173端口, 已配置 /api 代理)
- 上线: `npm run build` 后重启 serve 即可

## 运行模式说明

| 模式 | 说明 |
|---|---|
| 模拟盘(默认) | paper_trading 完整撮合, 支持 T+1/手续费/冻结资金/部分成交 |
| 实盘 | 未接入; BrokerAdapter 接口就绪, 按文档23阶段推进 |
| 回测 | 不影响真实账户的独立模拟 |

## 系统架构

```
9. Web管理台 / 邮件通知 / 报告导出      ← web/, notification/, reports/
8. 日志/记忆/审计/复盘                  ← memory/, workflows/daily_review_workflow.py
7. 回测/模拟盘/实盘执行层               ← backtest/, paper_trading/, live_trading/
6. 风控/订单/持仓账户系统               ← risk/, paper_trading/
5. 多Agent投研与交易决策层              ← agents/(15个), workflows/
4. 策略信号与特征工程层                 ← strategies/, features/
3. 数据清洗/质量校验/统一数据服务       ← data_service/, database/
2. 数据采集调度层                       ← scheduler/, data_sources/hub.py
1. 多源数据接入层                       ← data_sources/(akshare/eastmoney/sina/cninfo/tushare)
```

## 15 个 Agent 与调用链

```
data_admin(数据闸门) → 7分析师并行 → bull/bear辩论 → chief(首席)
  ↓
trader(交易计划) → risk_manager(五层风控) → compliance(合规) → 执行/人工确认
  ↓
execution_supervisor(订单监控) → review(日终复盘)
```

| # | Agent | 职责 | 实现 |
|---|---|---|---|
| 1 | 数据管理员 | 数据完整性闸门, 不合格阻断 | 规则为主 |
| 2 | 技术分析师 | 趋势/均线/MACD/RSI/支撑压力/突破 | LLM+特征 |
| 3 | ETF专项分析师 | 流动性/折溢价/IOPV/QDII风险 | LLM+特征 |
| 4 | 基本面分析师 | 股票估值/成长/财务风险 | LLM(P1简化) |
| 5 | 新闻公告分析师 | 事件抽取/风险分级 | 深模型+规则 |
| 6 | 情绪分析师 | 舆情热度/情绪分(辅助) | LLM(P1简化) |
| 7 | 资金流分析师 | 主力资金方向 | LLM(P1简化) |
| 8 | 宏观分析师 | 大盘状态/攻防判断 | LLM+特征 |
| 9 | 看多研究员 | 构造买入理由 | 深模型 |
| 10 | 看空研究员 | 专门找反对理由 | 深模型 |
| 11 | 首席研究员 | 综合多空形成结论(不下单) | 深模型 |
| 12 | 交易员 | 研究→交易计划(T+1/资金/整数股) | 深模型+规则修正 |
| 13 | 风险管理 | 五层风控审核 | 硬规则优先 |
| 14 | 合规审计 | 时段/重复单/次数金额限额 | 纯规则 |
| 15 | 执行监督 | 订单状态/部分成交/超时撤单 | 纯规则 |
| + | 复盘总结 | 日终复盘/改进建议 | 统计+LLM |

**简化说明**(对比文档15个Agent, 实际实现16个):
- 未裁减任何 Agent; 数据管理员/合规审计/执行监督为规则引擎(确定性优于LLM)
- 情绪/资金流/基本面按 P1 简化: 无数据时返回低置信度中性, 不阻塞主流程
- 复盘总结为统计+LLM(未做Agent贡献度归因, 属文档22.3改进项)

## 五层风控

账户级(仓位/单日亏损/现金比例/次数金额) → 标的级(溢价/波动率/流动性/黑名单) →
策略级(预留) → 订单级(单笔限额/价格偏离/超卖) → 模型级(置信度/可解释) +
组合级(同类集中度) + 熔断器(单日亏损/连续失败/行情延迟/人工暂停)

人工确认分级: ≤1000元低风险自动执行 / ≤5000元中风险邮件界面确认 / 高风险禁止。

## 关键设计

1. **无未来函数回测**: 决策只用 ≤T 数据, T+1 开盘+滑点成交; 分钟回测增量喂数据
2. **多源容灾**: 东财限流自动切新浪(tqdm 进度条为东财分页, 已加缓存)
3. **幂等下单**: order_intent_id 唯一, 重复提交拦截
4. **摘要式喂给LLM**: 特征层计算数字 → 紧凑中文摘要 → Agent 分析(token 可控)
5. **LLM降级**: 未配置 API Key 时规则模拟输出, 全链路可跑, 填配置后无缝切换
6. **RAG**: pgvector 余弦检索 + 关键词兜底(哈希向量)

## 配置

| 文件 | 内容 |
|---|---|
| `config/config.yaml` | 系统/LLM/标的池/盘中监控/Web |
| `config/risk_limits.yaml` | 五层风控限额/熔断/确认分级 |
| `config/data_sources.yaml` | 主备源/多源比对/新鲜度 |
| `config/model_routes.yaml` | 快/深模型任务路由 |
| `config/agent_schedule.yaml` | 调度任务 |
| `config/trading_rules.yaml` | 手续费/涨跌停/交易时段/T+1 |
| `config/prompts/agents.yaml` | 15个Agent提示词 |
| `.env` | 密钥(DB/LLM/邮件) |

## 测试

```bash
python -m unittest tests.test_suite -v    # 14项: 指标/撮合/T+1/风控/回测指标/质量/工作流
```

## 实盘接入(预留)

`live_trading/broker_adapter.py` 为标准接口(QMT/PTrade 桩已就位)。
按文档23阶段推进: 只读同步 → 撮合校准 → 半自动 → 小额度自动, 不可跳级。
