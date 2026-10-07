# 多Agent智能量化交易系统 V1

面向 A 股 ETF 的多 Agent 投研 + 动量轮动 + 模拟盘交易系统。
**当前运行模式**: 策略独立交易(模拟盘) · Agent 影子观察(只分析留档, 不下单) · 五层硬风控。

- 唯一买卖决策源: **动态ETF池 + 动量轮动策略**(与回测共用同一信号函数)
- **市场状态自适应切换**(regime switch, 当前激活): 沪深300 MA20/MA60+动量识别
  risk_on/neutral/risk_off, 14:40 轮动时自动在
  `V4-切换-进攻/稳健/防守` 三档间切换(两次切换≥20个交易日,
  3年离线验证 +58.26%/夏普1.15/-8.93%, 优于任一静止档);
  可在模拟盘页"市场状态切换"面板开关/手动改档
- Agent(15+1)只做研究、留档与收益归因; 数据闸门/硬风控/合规/熔断拥有绝对否决权
- 实盘未接入: `live_trading/` 预留 QMT/PTrade 适配器

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.13 | venv 见 `.venv` |
| PostgreSQL | 16 | 库 `quantiagent`, 用户 `quantiagent/quantiagent` |
| pgvector | 0.8.6 | 向量检索(RAG) |
| Node.js | 18+ | 前端构建(`frontend/`) |

## 快速开始

```bash
# 1. 配置密钥(复制 .env.example 为 .env)
#    LLM: OpenAI 兼容接口(不填自动进入"规则模拟"模式)
#    邮件: SMTP(QQ邮箱 465)
#    Web: WEB_ADMIN_TOKEN / WEB_CONFIRM_SECRET(各≥32字符, 否则拒绝启动)
cp .env.example .env

# 2. 初始化数据库(建表+种子数据)
python main.py init-db

# 3. 标的池 + 历史行情(首次务必回填, 否则回测/分析数据不全)
python main.py fetch-symbols                 # 全市场ETF池(东财直连→新浪→akshare)
python main.py backfill --days 700           # 监控池+持仓+成交额Top150+指数
# python main.py backfill --all --workers 4 --days 1700   # 全市场+近3年(批量回测用)

# 4. 单标的Agent分析(数据闸门→7分析师→多空辩论→首席; 影子模式不下单)
python main.py scan 510300

# 5. 回测(日线, 动态ETF池, 含沪深300基准)
python main.py backtest --start 2026-01-01 --end 2026-09-24

# 6. Web 管理台(内嵌调度器): 仪表盘/盯盘/标的/Agent观察/回测/模拟盘/账户分析/设置
python main.py serve                         # http://localhost:8080

# 7. 独立调度器(Web 已内嵌, 一般无需单独启动)
python main.py scheduler

# 8. 重置模拟盘(自动归档上一轮运行记录, 可随时回看)
python main.py reset-paper --yes --initial-cash 100000 --note "策略V2上线"

# 9. 辅助命令
python main.py rotate                        # 手动执行一次ETF轮动调仓(回测外)
python main.py universe                      # 查看当前动态ETF池候选/母池/分布
python main.py status                        # 系统状态(调度器/账户/持仓)
```

前端开发: `cd frontend && npm run dev`(5173, 已代理 /api); 上线 `npm run build`。

## 系统架构

```
┌─────────────────────────── Web 管理台 (React+Vite+Tailwind+ECharts) ───────────────────────────┐
│ 仪表盘 · 实时盯盘 · 监控标的 · 标的搜索 · Agent观察 · 回测中心 · 模拟盘/实盘 · 账户分析 · 设置      │
└───────────────────────────────────────┬───────────────────────────────────────────────────────┘
                                        │ REST /api (Bearer Token)
┌───────────────────────────────────────▼───────────────────────────────────────────────────────┐
│ Web API (FastAPI: web/api/main.py + extra_api.py)                                              │
│ 行情/详情/K线/分时 · 异步回测 · 工作流trace · 确认单 · 系统日志 · 模拟盘重置/归档               │
└───────┬───────────────────────┬───────────────────────┬───────────────────────────────────────┘
        │                       │                       │
┌───────▼─────────┐   ┌─────────▼──────────┐   ┌────────▼───────────────────────────────────────┐
│ 调度器(内嵌)     │   │ 策略执行层          │   │ 账户/订单层                                     │
│ APScheduler     │   │ live_rotation      │   │ PaperBroker(OrderManager/PaperAccount/         │
│ 交易日历/行情/   │   │ 14:40 动态池轮动    │   │ PortfolioManager): T+1/手续费/滑点/幂等下单     │
│ 新闻/Agent分析/  │   │ (与回测同一信号函数) │   │ 账户快照/净值曲线/运行记录归档                   │
│ 持仓巡检/日报    │   └─────────┬──────────┘   └────────┬───────────────────────────────────────┘
└───────┬─────────┘             │                       │
        │               ┌───────▼───────────────────────▼────────┐
        │               │ 硬闸门: 数据质量 → 五层风控 → 合规 → 熔断 │
        │               └───────┬────────────────────────────────┘
        │                       │
┌───────▼───────────────────────▼───────────────────────────────────────────────────────────────┐
│ 多Agent投研层 (workflows/ + agents/)  影子模式: 只输出观点/留档/收益归因, 不产生订单            │
│ data_admin → 7分析师并行 → bull/bear辩论 → chief → [影子留档] → 复盘                            │
└───────────────────────────────────────┬───────────────────────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼───────────────────────────────────────────────────────┐
│ 策略/特征层 (strategies/ + features/)                                                          │
│ 动态ETF池两阶段选池 · 20日动量轮动 · 技术指标/市场状态 · 参数预设(presets)                      │
└───────────────────────────────────────┬───────────────────────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼───────────────────────────────────────────────────────┐
│ 数据服务层 (data_service/): 多源容灾 → 质量校验 → 落库 → 缓存 → 统一出口                        │
│  market_data_service · live_quote_service · news_service · rag_service · data_quality          │
└───────────────────────────────────────┬───────────────────────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼───────────────────────────────────────────────────────┐
│ 数据接入层 (data_sources/hub.py 多源容灾链)                                                     │
│  新浪(日K/实时/榜单) · 腾讯(实时/分时/日K前复权) · 东财(实时/列表) · baostock · tushare · cninfo │
└───────────────────────────────────────┬───────────────────────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼───────────────────────────────────────────────────────┐
│ 存储: PostgreSQL(pgvector)  行情/账户/订单/成交/Agent输出/审计/报告/归档                        │
│ 文件: logs/(分模块日志) reports/(报告+归档JSON) data/(缓存/预设/状态)                            │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 一次决策的数据流

```
定时任务/手动 → 行情采集(多源校验) → 特征计算 → [Agent影子分析(可选)]
                                      ↓
                    动态ETF池(周/月) + 动量轮动信号(14:40)
                                      ↓
                 硬风控(账户/标的/策略/订单/模型/组合) → 合规 → 熔断检查
                                      ↓
                 模拟撮合(T+1/手续费/滑点) → 持仓/账户更新 → 审计日志/邮件
                                      ↓
                 收盘快照(30分钟) → 净值曲线 → 账户分析/日报/复盘
```

## 目录结构

```
QuantiAgent/
├── main.py                  # CLI 入口(init-db/fetch/backfill/scan/backtest/serve/reset-paper...)
├── agents/                  # 16个Agent: 分析师/研究员/交易员/风控/合规/执行/数据闸门
├── analytics/               # Agent影子评估与策略信号配对归因
├── backtest/                # 回测引擎(无未来函数/涨跌停/基准/覆盖度校验)
├── config/                  # 全部YAML配置 + Agent提示词
├── core/                    # 配置加载/日志/LLM/ID/符号工具/防休眠/Agent开关
├── data_service/            # 统一数据服务(行情/实时/新闻/RAG/缓存/质量校验)
├── data_sources/            # 数据源客户端 + hub 多源容灾
├── database/                # SQLAlchemy模型 + repository + 初始化迁移
├── docs/                    # 技术方案/表结构/数据源调研/审计文档
├── features/                # 技术指标/市场状态特征
├── frontend/                # React 前端(Vite+Tailwind+ECharts, 构建产物由后端托管)
├── live_trading/            # 实盘预留(BrokerAdapter/QMT/PTrade 桩)
├── memory/                  # 审计日志
├── notification/            # 邮件通知(队列/去重/确认链接)
├── paper_trading/           # 模拟盘(账户/撮合/组合/重置归档)
├── reports/                 # 报告生成 + paper_archive/(运行记录JSON)
├── risk/                    # 五层风控/熔断器/持仓巡检
├── scheduler/               # APScheduler 调度(单例锁+心跳)
├── scripts/                 # 历史回填/网格搜索/分析/状态回验
├── strategies/              # 动态ETF池/轮动信号/市场状态/参数预设/实盘落地
├── tests/                   # 单元测试(55项)
├── web/api/                 # FastAPI 路由
└── workflows/               # 研究/交易/盘中监控/日终复盘 工作流
```

## 策略与风控

### 轮动策略(唯一交易源)

| 环节 | 说明 |
|---|---|
| 选池 | 两阶段: ①上市时长/流动性/完整度/波动率筛选(最多40只) ②20日动量排名 |
| 建仓 | Top N(默认4), 单标的目标21%, 总仓位≤85%, 首次建仓50% |
| 调仓 | 每日评估(间隔可配), 排名跌出 top_n+hold_buffer 才卖, 最小持仓3日 |
| 止损 | 成本硬止损8%; 移动止盈: 浮盈≥4%后从最高回撤6% |
| 过滤 | 站上MA20/距MA20过热保护/最低动量/追高保护/市场风险过滤(可选) |
| 参数 | 命名策略(presets)可在回测中心保存并一键应用到模拟盘; 当前生效见 `data/strategy_presets.json` |

### 参数优选(GRIDV3/V4)

- **GRIDV3**(9个月, 500组): 参数影响力排序、分组与交互分析 → `data/backtest_grid/v3/analysis/REPORT.md`
- **GRIDV4**(近3年, 707组, 分段+滚动前推+邻域稳健性): 最终报告
  `data/backtest_grid/v4/analysis/FINAL_REPORT.md`; 6类(稳健综合/收益进攻/低回撤/风险调整/分段一致/低换手)
  各 Top5 已保存为 `V4-*` 命名策略, 可在回测中心查看/应用(未自动切换 active_paper)。
- **市场状态自适应切换**: `regime_switch`(默认启用, config.yaml 可一键关闭)。
  按沪深300状态(MA20/MA60+20日动量, 连续5日确认, 间隔≥20交易日)自动在
  `V4-切换-进攻/稳健/防守` 之间切换; 离线3年验证 +58.26%/夏普1.15/回撤-8.93%
  (见 `data/backtest_grid/v4/analysis/REGIME_SWITCH_REPORT.md`); 模拟盘页顶部实时显示状态与当前策略。
- **共享研究资料**(已随仓库分发, 供共同验证): 38个命名策略(`data/strategy_presets.json`,
  含 V2-Top5 / V4-* / V4-切换-*); V2/V3 参数实验汇总(`data/strategy_grid_*.json`);
  v3/v4 全部分析表与报告(`data/backtest_grid/{v3,v4}/analysis/`, 其中 `ranked_all.csv`
  含全部候选参数与分段指标); 原始逐日净值(results_*.jsonl, ~15MB)未入库, 可用 `scripts/` 重跑生成。
- 批量运行: `python -m scripts.grid_search_v4 run --candidates ... --results ...`(见脚本头注释)。

### 五层风控 + 组合 + 熔断

账户级(仓位/单日亏损/现金比例/次数金额) → 标的级(溢价/波动率/流动性/黑名单) →
策略级 → 订单级(单笔限额/价格偏离/超卖) → 模型级(置信度/可解释) →
组合级(集中度) + 熔断器(单日亏损/连续失败/行情延迟/人工暂停)。

持仓巡检: 交易时段每5分钟扫描持仓, 硬止损8%/移动止盈8%自动卖出(可配置为仅告警)。

## Agent 影子模式

```
data_admin(数据闸门) → 7分析师并行 → bull/bear辩论 → chief(首席)
  ↓ (影子留档: 观点+置信度+当时行情快照+trace_id)
策略信号与最近Agent观点配对 → 1/3/5/10日方向收益归因(Agent观察页)
```

Agent 总开关/单 Agent 开关在设置页可调(成本控制); 关闭后自动与手动扫描均跳过,
策略轮动、行情采集、硬风控与模拟撮合仍独立运行。

## 数据源

多源容灾链(主源失败自动切换), 详见 **[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)**:

| 类别 | 主源 → 备源 | 说明 |
|---|---|---|
| 历史日K | 新浪 → 腾讯 → baostock → akshare | 新浪单次1023根(≈4年), ETF/股票/指数通用 |
| 实时行情 | 腾讯 → 东财 → 新浪 | 双源交叉校验, 价差超阈值拒绝 |
| 全市场列表 | 东财直连 → 新浪 → akshare | 磁盘缓存+陈旧回退, 防限流 |
| 公告 | 巨潮 → 东财 | |
| 交易日历 | baostock → akshare → tushare | 失败时 fail-closed 不交易 |

批量回填(`backfill`)采用低并发多源链, 避免触发免费接口限流。

## 模拟盘重置与运行记录

- Web「模拟盘/实盘」→ 重置按钮(勾选确认, 可设新初始资金/名称/备注), 或 `python main.py reset-paper --yes`
- 重置前自动归档: 账户/持仓/订单/成交/净值/确认单 → `paper_run_archives` 表 + `reports/paper_archive/*.json`
- 归档永久保留, 页面可查看明细/下载/删除; 审计日志不删除
- API: `GET /api/paper/archives`、`POST /api/paper/reset`(需 `{"confirm":"RESET"}`)

## 日志

- 位置: `logs/system.log`(主) / `logs/error.log`(错误) / `logs/{data,agent,risk,order,audit}/`
- 滚动: 按天 + 单文件20MB, gzip 压缩; system 保留30天, error 保留90天
- 查看: 设置页「系统日志」(选择/尾行/自动刷新/清理); API `GET /api/logs`
- 降噪: httpx/uvicorn.access/apscheduler 等仅记录 WARNING+

## 配置

| 文件 | 内容 |
|---|---|
| `config/config.yaml` | 系统/LLM/标的池/动态池/盘中监控/Agent开关/策略参数/Web |
| `config/risk_limits.yaml` | 五层风控限额/熔断/确认分级/持仓巡检 |
| `config/data_sources.yaml` | 多源容灾链/新鲜度/限流 |
| `config/model_routes.yaml` | 快/深模型任务路由 |
| `config/agent_schedule.yaml` | 调度任务(cron/间隔) |
| `config/trading_rules.yaml` | 手续费/涨跌停/交易时段/T+1 |
| `config/prompts/agents.yaml` | Agent 提示词 |
| `.env` | 密钥(DB/LLM/邮件/Web令牌) |

## 测试

```bash
python -m unittest tests.test_suite -v   # 55项: 指标/撮合/T+1/风控/回测/质量/工作流/调度/行情共识
```

## 实盘接入(预留)

`live_trading/broker_adapter.py` 为标准接口(QMT/PTrade 桩已就位)。
按文档23阶段推进: 只读同步 → 撮合校准 → 半自动 → 小额度自动, 不可跳级。

## 常见问题

| 现象 | 处理 |
|---|---|
| 回测行情大量缺失/停在几周前 | `python main.py backfill --days 700` 回填后再回测 |
| 页面数据加载失败 | 检查设置页系统日志与数据源状态; 免费接口限流时等待冷却或切换源 |
| 回测没有基准曲线 | 确认已回填指数(`backfill` 默认包含 000300/000905/000001/399006) |
| 调度器未运行 | 启动 Web 会自动拉起(单例锁); 或 `python main.py scheduler` |
| 电脑休眠导致停摆 | 系统已启用防自动睡眠; 仍建议电源设置"睡眠: 从不" |
