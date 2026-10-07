# 行情数据源调研与接入说明

> 调研日期: 2026-09-24 · 结论基于本机(中国大陆家庭宽带, 系统代理不可用)实测

## 一、结论(先说重点)

1. **交易所官方没有"免登录、逐标的、多年日线历史"的公开 API。**
   上交所/深交所官网「数据/行情信息」栏目提供的是**汇总统计**(日度/周度/月度成交概况)、
   **产品目录**(股票列表/ETF 列表可下载)与**指数行情**(分时/日线/周线/月线, 页面展示);
   逐笔/历史明细属于**持牌行情数据**，需通过券商或行情服务商获取，不存在可直接抓取的
   官方 CSV/JSON 接口。所谓"行情全部免费开放"指的是**投资者经券商免费获得 Level-1
   实时行情**，不等于官网开放全量历史数据下载。
2. 官方渠道里**可以免费、稳定拿到**的是:
   - 中证指数官网(csindex.com.cn)/国证指数(cnindex.com.cn): 指数历史日线 CSV 下载;
   - 上交所/深交所: 股票列表、ETF 列表、统计月报(Excel/CSV);
   - 巨潮资讯(cninfo): 上市公司公告(本项目已接入 `data_sources/cninfo_client.py`)。
3. 因此本项目对**逐标的日线历史**采用"免费公开行情接口 + 多源容灾":
   新浪 / 腾讯 / 东方财富 / baostock / tushare(可选 token)。
   这些接口与官网同源(腾讯自选股、新浪财经、东财行情中心), 免费且无需注册。

## 二、实测结果(2026-09-24, 本机网络)

| 源 | 用途 | 实测 | 说明 |
|---|---|---|---|
| 新浪 `money.finance.sina.com.cn` | 日K(1023根≈4年) | ✅ 稳定 | ETF/股票/指数通用; 未复权; 单次约0.3s |
| 新浪 `hq.sinajs.cn` | 实时行情 | ✅ 稳定 | 需 Referer 头, GBK |
| 新浪 `vip.stock.finance.sina.com.cn` | ETF列表/A股成交额榜 | ✅ 稳定 | 热门股票页现用此源 |
| 腾讯 `qt.gtimg.cn` | 实时行情/五档 | ✅ 稳定 | 极快, 含昨收/盘口 |
| 腾讯 `web.ifzq.gtimg.cn` | 日K(前复权) | ⚠️ 会限流 | 连续约200次请求后返回 501 反爬页; 需降速/冷却 |
| 东财 `push2.eastmoney.com/api/qt/stock/get` | 实时行情(备源) | ✅ 可用 | 单只查询正常 |
| 东财 `push2.eastmoney.com/api/qt/clist/get` | 全市场列表 | ⚠️ 会限流 | 高频后 RemoteDisconnected, 冷却后恢复 |
| akshare(东财分页封装) | 全市场ETF列表 | ❌ 常失败 | 分页请求多、易被断开; 已被东财直连/新浪替代 |
| baostock | 日K/交易日历 | ✅ 可用 | 免费稳定但 ETF 覆盖不全、单只约1-2s |
| tushare | 日K(备源) | ⏸ 需 token | 配置 `TUSHARE_TOKEN` 后启用 |
| 网易 `quotes.money.163.com` | 日K CSV | ❌ 502 | 本机不可用 |

**限流规律**: 免费接口对**突发批量**敏感(连续几百次请求会封禁几分钟到几十分钟),
对**低频单只**请求宽容。因此本项目的策略是:
- 常态运行: 只拉监控池/持仓/候选池(几十只), 不会触发限流;
- 批量回填: `python main.py backfill` 使用**多源链 + 低并发(2) + 间隔**, 任一源被限流
  自动切换下一源(新浪→腾讯→baostock);
- 全市场列表: 磁盘缓存 + 陈旧回退(东财限流时用上次缓存, 后台慢速刷新)。

## 三、本项目的多源容灾配置(config/data_sources.yaml)

```yaml
daily_bar:   primary sina    backups [tencent, baostock, akshare]   # 历史日K
daily_gap:   primary sina    backups [tencent, baostock, akshare]   # 缺口补齐
realtime_quote: primary tencent backups [eastmoney, sina, akshare]
etf_info:    primary eastmoney backups [sina, akshare]              # 全市场ETF列表
announcement: primary cninfo backups [eastmoney]
trade_calendar: primary baostock backups [akshare, tushare]
```

- 每个类别独立容灾链: 主源失败→备源依次尝试, 全部失败才阻断(数据质量层标记 MISSING)。
- 同一日期多源数据并存时, 按上述优先级去重(`repository.get_daily_bars`)。
- 前复权(qfq)与未复权数据**不混用**: 同一日期只保留一个来源的行; 回测器另有
  价格断层修复(`_normalize_price_regimes`)。

## 四、常用命令

```bash
python main.py fetch-symbols              # 更新ETF池(东财直连→新浪→akshare)
python main.py fetch-daily --days 400     # 增量更新日K(多源)
python main.py backfill --days 700        # 批量回填历史(腾讯/新浪链, 低并发)
python main.py backfill --all --workers 2 # 全市场回填(约1500只, 数分钟)
```

## 五、如何新增数据源

1. 在 `data_sources/` 新建客户端, 继承 `BaseDataSource`(按需实现
   `get_daily_bars/get_realtime_quote/get_etf_spot/...`);
2. 在 `data_sources/hub.py::_get_client` 注册类名;
3. 在 `config/data_sources.yaml` 的对应类别加入主/备源顺序;
4. 在 `scripts/backfill_history.py` 的源链中按需加入(批量回填用)。

## 六、官方数据获取路径(备查)

| 数据 | 官方入口 | 形式 |
|---|---|---|
| 股票/ETF 列表 | 深交所 市场数据→产品目录; 上交所 数据→基金数据 | 网页/Excel/CSV 下载 |
| 指数历史日线 | 中证指数 csindex.com.cn → 指数数据下载; 国证指数 cnindex.com.cn | CSV |
| 上市公司公告 | 巨潮资讯 cninfo.com.cn | 网页/接口(已接入) |
| 汇总成交统计 | 上交所/深交所 市场数据→成交概况(日/周/月/年) | 网页/Excel |
| 实时 Level-1 行情 | 经券商交易终端/行情服务商(投资者免费) | 券商 API(QMT/PTrade 等) |

> 若后续要"官方直连", 现实路径是**通过券商 QMT/PTrade 获取行情与交易权限**
> (本项目 `live_trading/` 已预留适配器接口), 而不是抓交易所官网。
