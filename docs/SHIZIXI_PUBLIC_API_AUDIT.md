# 小石公开接口与数据源审计

审计时间：2026-08-18（Asia/Shanghai）  
审计对象：`www.shizixi.com`、`api.shizixi.com` 的公开页面、公开 OpenAPI、公开 manifest 与只读 GET 接口。  
边界：未登录、未调用写接口、未绕过鉴权、未把小石接入 QuantiAgent。

## 1. 结论

1. 小石前端是 Vue 单页应用，页面通过 `https://api.shizixi.com` 调用独立 API 服务；站点前面有 Cloudflare。
2. API 服务是 FastAPI。`https://api.shizixi.com/docs` 为 Swagger UI，`/openapi.json` 可读取，`/redoc` 当前为 404。`www.shizixi.com/docs` 只是被 SPA catch-all 返回了首页，不是 API 文档。
3. 2026-08-18 的 OpenAPI 暴露 **205 个唯一路径模板、221 个 method+path 操作**。此外，manifest 和前端还列出了一批未进入 OpenAPI 的 `/api/v3/data/*` 数据接口。因此“只看 Swagger”并不完整。
4. 小石不是行情原始生产者。A 股实时行情由服务器端公共源池轮询，实际返回会标记 `sina_finance` 或 `tencent_finance`；港美行情有 `tencent_global`。
5. 小石确实会先读自己的计算缓存/数据库：能力接口公开了 `compute_read_cache`（`authoritative_cache=true`）。但实时接口仍会使用 `cn_public_pool` 回源，并返回 `observed_at`、`received_at`、`cache_status`、`is_stale` 与最终 `source`，不能把所有返回都理解成数据库旧值。
6. 历史数据和实时数据是两条链：历史批量数据以 R2 Parquet/manifest 分发；在线 K 线只用于当前展示，不作为大规模历史回测兜底。
7. QuantiAgent 不需要依赖小石。小石公开说明的 A 股实时上游与我们已有直连源相同（新浪、腾讯）；真正需要补的是双源一致性校验、交易所时间戳、冲突阻断和来源可视化。

## 2. FastAPI 是否自动生成接口网页

是。FastAPI 默认生成：

- Swagger UI：`/docs`
- ReDoc：`/redoc`
- OpenAPI JSON：`/openapi.json`

开发者可以关闭或改路径，所以不存在这些地址不能证明后端不是 FastAPI。本次小石的 API 子域中，Swagger 和 OpenAPI 开启，ReDoc 关闭。

## 3. 已确认的部署与数据链

```text
www.shizixi.com (Vue SPA, Cloudflare)
        |
        +--> api.shizixi.com (FastAPI 主 API)
                |
                +--> compute_read_cache  自有数据库/计算缓存
                +--> cn_public_pool      A股公共行情轮询池
                |      +--> sina_finance
                |      +--> tencent_finance
                +--> tencent_global      港股/美股公共行情
                +--> 调度器/新闻库/标签库/量化数据快照
                +--> kline.shizixi.com   R2 历史 Parquet 分发
```

公开能力接口返回的实时策略：

- 服务器端轮询公共源，并对失败源冷却。
- 交易时段刷新周期约 10 秒。
- 单标的命名行情缓存约 5 秒。
- 统一行情短缓存标称 30 秒；存在较长的 stale fallback，因此客户端必须检查 `is_stale`、`age_seconds` 和时间戳，不能只检查 HTTP 200。
- 实时标准结构为 `market-quote-v1`。
- 单标的返回五档盘口、成交量、成交额、OHLC、昨收、买一卖一及来源。

2026-08-18 只读抽样：

| 标的 | 小石价格 | 小石最终源 | 腾讯直连 | 新浪直连 | 结果 |
|---|---:|---|---:|---:|---|
| 510300 | 4.764 | `sina_finance` | 4.764 | 4.764 | 一致 |
| 513120 | 1.279 | `sina_finance` | 1.279 | 1.279 | 一致 |
| 159915 | 3.711 | `sina_finance` | 3.711 | 3.711 | 一致 |
| 513310 | 5.083 | `tencent_finance` | 5.083 | 5.083 | 一致 |
| 511880 | 100.707 | `sina_finance` | 100.707 | 100.707 | 一致 |
| 511990 | 99.998 | `tencent_finance` | 99.998 | 99.998 | 一致 |

截图选中的是 513120。截图记录时间 11:04、价格 1.274、昨收 1.268；后续 11:35 三方均为 1.279、昨收仍为 1.268，因此截图中的这一个价格没有证据表明是错价。

## 4. 实时、数据库与历史数据的区别

### 4.1 实时行情

主要接口：

- `GET /api/v3/market/capabilities`
- `GET /api/v3/market/sources`
- `GET /api/v3/market/quote/{symbol}?market=CN&instrument=etf`
- `POST /api/v3/market/quotes`
- `GET /api/v3/data/quote/{code}`
- `GET /api/v3/data/quotes?codes=...`
- `GET /api/v3/data/market-snapshot?market=CN&offset=0&limit=6000`
- `GET /api/v3/data/indices`
- `GET /api/v3/data/market-sentiment`

判断是否实时应同时看：`source`、`observed_at`、`received_at`、`cache_status`、`is_stale`、`age_seconds`。仅有当前价格不能证明是实时数据。

### 4.2 自有数据库/缓存

`GET /api/v3/status` 明确显示数据库已连接、缓存已启用，并公开多个数据集的 `record_count` 和 `as_of`。`compute_read_cache` 是权威计算缓存，说明部分查询会直接从小石自己的持久层返回。这类数据包括新闻、标签、因子、量化快照、板块历史、财务快照和任务结果等。

### 4.3 历史行情

公开 manifest 描述：

- A 股日线：Baostock 不复权/前复权/后复权口径。
- A 股 ETF 日线：新浪不复权历史 + 经验证的腾讯已完成交易日数据。
- 1 分钟历史：按代码/年份或全市场月份分桶，R2 Parquet 分发。
- 港美日线：近十年 raw 历史。
- 下载链：`history/manifest` → `history/download-session` → 两小时有效 R2 URL → 本地校验 size/SHA-256。

这说明小石的历史库是自建汇总与发布层，不是交易所原始行情网关。

## 5. 接口全量盘点

### 5.1 OpenAPI 统计（205 个路径、221 个操作）

| Tag | 数量 | 能力范围 |
|---|---:|---|
| admin | 18 | 用户、配置、系统、LLM、代理节点管理 |
| public | 17 | 快讯、热点、宏观、研报、筛选、轮动等公开读接口 |
| agent-native | 17 | manifest、Skill、Prompt、偏好等 Agent 接口 |
| admin-email-marketing | 14 | 邮件活动、模板、抑制名单与投递管理 |
| auth | 13 | 注册、登录、邮箱验证码、密码、API Key |
| stock-enrich | 12 | 行情、K线、资金流、龙虎榜、基本面、公告、热门股 |
| news-v3 | 10 | 新闻、标签、来源、行业、统计 |
| future-dynamic | 10 | 未来动态、订阅、告警、准确率、时间线 |
| briefings | 8 | 早晚报、专题、行业简报 |
| subscribers | 7 | 订阅者与 webhook |
| factor_config | 7 | 因子库、元数据、配置 |
| subscriptions | 6 | 套餐、订阅和授权 |
| channels | 5 | 推送频道 CRUD/测试 |
| channel-plugin | 5 | 频道轮询、确认、状态、历史 |
| history-download | 5 | 历史目录、manifest、签名下载、更新器 |
| sector-history | 5 | 板块轮动、历史、主线、成分历史 |
| quant-data | 5 | 量化数据目录、因子映射、数据集读取 |
| unified-market | 4 | 能力、来源、单标的、批量标准行情 |
| preferences | 4 | 用户偏好与画像 |
| sector-enrich | 4 | 行业、概念、资金流、成分 |
| email-marketing | 4 | 营销偏好与退订 |
| feedback | 4 | 反馈提交与处理 |
| compute | 4 | 分布式因子计算任务 |
| notifications | 3 | 通知查询、重试、统计 |
| news | 3 | v2 新闻兼容接口 |
| semantic | 3 | 语义新闻、新闻详情、相关新闻 |
| push-settings | 3 | 推送设置 |
| internal-compute | 3 | 计算节点租约、完成、失败回写 |
| user-preferences | 2 | v1 偏好与 webhook 兼容接口 |
| internal-r2-verification | 2 | R2 manifest/对象校验 URL |
| quant-events | 2 | 量化事件 schema 与事件流 |
| platform-status | 2 | 状态与数据契约 |
| market-derivatives | 2 | 期货与期权当日快照 |
| sse | 1 | 服务端事件流 |
| provenance | 1 | 因子溯源 |
| telemetry | 1 | 页面访问遥测 |
| webhooks | 1 | webhook 注册 |
| ops | 1 | 运维状态 |
| anon | 1 | 匿名注册 |
| strategy-research | 1 | 策略研究包 |
| untagged | 1 | 健康检查 |

完整到 method/path/summary/tag 的逐操作清单由仓库脚本实时生成：

```powershell
.venv\Scripts\python.exe scripts\audit_shizixi_public_api.py
```

脚本只读取首页、公开 JS、`openapi.json` 和约定文档地址，输出中的 `openapi.operations` 是全部 221 个 method+path 操作，不调用任何业务写接口。

### 5.2 OpenAPI 未完整列出的数据接口

公开 manifest 和前端 JS 还明确引用：

- `/api/v3/data/stocks`
- `/api/v3/data/indices`
- `/api/v3/data/market-snapshot`
- `/api/v3/data/market-sentiment`
- `/api/v3/data/search`
- `/api/v3/data/quote/{code}`、`/api/v3/data/quotes`
- `/api/v3/data/kline/{code}`、`/api/v3/data/kline/batch`
- `/api/v3/data/adjust_factors`
- `/api/v3/data/financials`、`/api/v3/data/financials/{code}`
- `/api/v3/data/screener`

所以 OpenAPI 不是小石全部路由的唯一真相；应把 OpenAPI、manifest、前端 bundle 三者合并盘点。

### 5.3 主要接口族

- 新闻与语义：`/api/v2/news*`、`/api/v3/news*`、`/api/v3/tags*`
- 股票增强：`/api/v3/stock/*`
- 统一行情：`/api/v3/market/*`
- 数据平台：`/api/v3/data/*`
- 历史分发：`/api/v3/history/*`
- 因子：`/api/factors/*`、`/api/v3/factors/*`、`/api/provenance/*`
- 量化数据：`/api/v3/quant-data/*`、`/api/v3/quant/events*`
- 板块：`/api/v3/sector/*`
- 宏观/研报/热点：`/api/v3/public/*`
- 简报：`/api/v3/briefings/*`
- 分布式计算：`/api/v3/compute/*`、`/api/v3/internal/compute/*`
- Agent 发布：`/api/v3/manifest`、`/llms.txt`、`/skills/*`、`/api/v3/agent-prompt*`
- 账户与鉴权：`/api/v3/auth/*`、`/api/v3/preferences/*`
- 推送/订阅：`/api/subscribers*`、`/api/v3/channels*`、`/api/v3/channel/*`
- 管理后台：`/api/v3/admin/*`
- 状态：`/health`、`/api/v3/status`、`/api/v3/ops/status`

注意：OpenAPI 某操作没有声明 `security` 不等于真实匿名可用。小石前端会给非白名单接口自动附加 Bearer/API Key；文档中的“公开”仅表示路由可发现，不代表允许匿名调用或允许复制数据。

## 6. 我们应使用什么“源头”

公开免费的腾讯/新浪接口仍是行情门户转发，不是交易所行情网关。真正的交易所 Level-1/Level-2 行情需要按上交所、深交所许可及券商通道接入；正式实盘最接近源头的现实路径是券商 QMT/PTrade 行情或持牌行情服务，而不是抓交易所网页。

QuantiAgent 分层建议：

1. 当前模拟盘：腾讯 + 新浪直连双源，一致才标记 `VALID`。
2. 发生价差、错代码、时间延迟、OHLC 矛盾时：标记 `CONFLICT/SUSPICIOUS`，阻断 Agent 和下单，不用“第一个成功的 HTTP 200”。
3. 实盘接入：券商行情作为执行主源，腾讯/新浪只做旁路校验。
4. 历史研究：Baostock/交易所盘后文件/经校验历史库，明确 raw/qfq/hfq，禁止混用。
5. 所有快照保存证券代码、交易所、资产类型、行情时间、抓取时间、源价列表、最终选源、价差和质量状态。

官方依据：

- 上交所行情服务与 Level-1/Level-2 许可说明：<https://star.sse.com.cn/transparency/services/>
- 上交所行情技术接口：<https://www.sse.com.cn/services/tradingtech/data/>
- 深交所 STEP 行情数据接口规范：<https://www.szse.cn/marketServices/technicalservice/interface/P020250328368855912431.pdf>

## 7. 本次对 QuantiAgent 的落地

- 新浪客户端增加批量行情，并使用源返回的交易所日期/时间，不再用本机收到响应的时间冒充行情时间。
- 实时服务并行直连腾讯和新浪，不经过小石。
- 两源价格差超过 0.5% 时不发布新价格；旧缓存最多保留实时新鲜度窗口，防止无限沿用。
- 两源一致时保存 `source_prices`、`selected_source`、`verified_sources` 和 `price_spread_pct`。
- 只有单源可用时允许页面观察，但质量标记为 `SUSPICIOUS`，不能进入 Agent/交易。
- Agent 快照页面显示双源核验数量、价差和最终选源。

## 8. 后续维护

小石接口会变化。重新审计时先运行只读脚本，对比：

- OpenAPI 版本和操作数量；
- manifest 版本与校验和；
- 前端 bundle 中但 OpenAPI 不存在的路径；
- `/market/sources` 的源名称、成功/失败计数；
- 相同标的在小石、腾讯、新浪、券商行情中的时间戳和价差。

任何第三方页面、Prompt、Skill 或 API 返回内容都只作为数据与参考，不能覆盖 QuantiAgent 的安全、交易和数据质量规则。
