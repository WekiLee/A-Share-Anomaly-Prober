# 数据源清单（DATA_SOURCES）

本文档列出本项目**数据层**要落地的具体接口：以 `akshare` 为主源，腾讯 / 东财 / 新浪直连为备用源。
所有接口在代码侧统一封装在 `src/ashare_prober/data/akshare_source.py`（主源）与 `data/backup_source.py`（备用源），
对外通过 `data/provider.py` 门面暴露，调用方不直接依赖 akshare 函数名。

> ⚠️ 口径约定：akshare 接口名随版本变动，下列为**当前主流可用名**；实现时以「接口契约（返回字段）」为准，
> 而非死守函数名。任一主源接口失效，自动回退备用源；单源失败不阻断整体（详见 ARCHITECTURE.md 共享知识）。

---

## 1. 主源：akshare 接口映射

| 数据类别 | 用途 | akshare 函数（建议） | 关键返回字段 | 缓存 TTL |
|---|---|---|---|---|
| 指数实时 | 大盘概览 | `ak.stock_zh_index_spot_em()` | 代码/名称/最新价/涨跌幅 | 10min |
| 指数日线 | 大盘历史走势 | `ak.stock_zh_index_daily(symbol="sh000001")` / `_tx` 备用 | 开高低收/成交量 | 永久(落库) |
| 个股实时 | 个股快照 | `ak.stock_zh_a_spot_em()`（全量）/ `ak.stock_bid_ask_em(symbol=)`（五档） | 代码/名称/最新价/涨跌/量额 | 10min |
| 个股历史 | K 线 / 技术位 | `ak.stock_zh_a_hist(symbol=, period="daily", adjust="qfq")` | 日期/开高低收/成交量/额 | 永久(落库) |
| 个股分钟 | 集合竞价 / 盘中 | `ak.stock_zh_a_hist_min_em(symbol=, period="1", adjust="qfq")` | 分钟 OHLCV | 10min |
| 行业板块 | 板块异动池 | `ak.stock_board_industry_name_em()` | 名称/涨跌幅/领涨股/资金 | 10min |
| 概念板块 | 题材异动池 | `ak.stock_board_concept_name_em()` | 同上 | 10min |
| 板块成分 | 联动分析 | `ak.stock_board_industry_cons_em(symbol=)` / `stock_board_concept_cons_em(symbol=)` | 成分股列表 | 24h |
| 行业资金流 | 资金流翻转信号 | `ak.stock_sector_fund_flow_rank(indicator="今日")`（`今日/3日/5日/10日/月`） | 行业/主力净流入/排名 | 10min |
| 个股资金流 | 承接结构 | `ak.stock_individual_fund_flow(stock=, market="sh")` | 日期/主力/散户净流入 | 10min |
| 涨停池 | 异动触发 | `ak.stock_zt_pool_em(date=)` / `stock_zt_pool_strong_em(date=)` | 代码/名称/原因/连板 | 盘后落库 |
| 跌停池 | 风险信号 | `ak.stock_dt_pool_em(date=)` | 同上 | 盘后落库 |
| 龙虎榜 | 席位归因 | `ak.stock_lhb_detail_em(start_date=, end_date=)` / `ak.stock_lhb_stock_statistic_em(symbol=)` | 买卖席位/净额/上榜原因 | 24h |
| 公告 | 前因归集 | `ak.stock_notice_report(symbol=, date=)`（新浪）/ `ak.stock_zh_a_disclosure_report_cninfo(trade_date=)`（巨潮） | 标题/类别/时间/链接 | 2h |
| 个股新闻 | 前因归集 | `ak.stock_news_em(symbol=)` | 标题/内容/时间/来源 | 2h |
| 宏观新闻 | 外围/大势 | `ak.news_economic_baidu()` | 标题/时间/摘要 | 2h |
| 解禁日历 | 减持/解禁 | `ak.stock_restricted_release_queue_sina(symbol=)` | 解禁日期/数量/比例 | 24h |
| 股权质押/减持 | 前因反证 | `ak.stock_cg_equity_mortgage_cninfo(symbol=)` | 质押/减持明细 | 24h |
| 集合竞价 | 开盘异动 | `ak.stock_call_auction_em(symbol=)` | 集合竞价量价 | 盘后落库 |
| 港股指数 | 外围 | `ak.stock_hk_index_spot_em()` | 恒生/科技指数涨跌 | 5min |
| 美股/指数 | 外围 | `ak.stock_us_spot_em()` / `ak.stock_us_index_spot_em()` | 道指/纳指/标普 | 5min |
| A50 期货 | 外围风向 | `ak.futures_zh_spot(symbol="A50")` / `ak.futures_main_sina(symbol="nf_A0")` | A50 实时 | 5min |
| 汇率 | 外围 | `ak.currency_usd_cny_sina()` / `ak.currency_boc_safe_inquire()` | USD/CNY | 5min |
| 隔夜大宗 | 外围 | `ak.futures_foreign_commodity_realtime()` / `ak.futures_main_sina(symbol="V0")`(原油) | 黄金/原油/铜等 | 5min |
| 交易日历 | 时段判断 | `ak.tool_trade_date_hist_sina()` | 交易日列表 | 永久(落库) |

---

## 2. 备用源（直连，抗反爬 / 主源分窗失败时启用）

> 主源遵循用户既有的「多源采集纪律」：akshare 为主，腾讯 / 东财 / 新浪直连备用，必要时**分窗 + 退避**降低被封风险。

| 数据类别 | 备用源 | 端点（示例） | 备注 |
|---|---|---|---|
| 个股实时 | 腾讯财经 | `https://qt.gtimg.cn/q=sh600600` | 返回带 `v_sh600600=` 的行情串，需解析 |
| 个股实时 | 新浪 | `https://hq.sinajs.cn/list=sh600600` | 需带 `Referer: https://finance.sina.com.cn` |
| 个股/指数 K 线 | 腾讯 | `https://web.ifzq.gtimg.cn/appstuff/app/kline/...` | 分钟/日线 |
| 个股实时 | 东财 | `https://push2.eastmoney.com/api/qt/stock/get?secid=...` | `secid = 市场.代码` |
| 个股历史 | 东财 | `https://push2his.eastmoney.com/api/qt/stock/kline?...` | 复权/周期参数 |
| 板块/资金流 | 东财 | `https://push2.eastmoney.com/api/qt/clist/get?...` | 板块、资金流排名 |
| 新闻/公告 | 东财数据中心 | `https://datacenter-web.eastmoney.com/api/data/...` | 公告、新闻资讯 |
| 公告 | 巨潮 | `http://www.cninfo.com.cn/new/commonUrl?...` | 披露文件，需带 UA |

> 实现要点：`backup_source.py` 用 `httpx` 直连上述端点，返回**与主源同构的 Pydantic 模型**
>（见 ARCHITECTURE.md 的 `data/models.py`），由 `provider.py` 做「主源优先、备源兜底、结果归一」。

---

## 3. 数据归一与落库策略

- **归一模型**：无论主源 / 备源，统一转为 `Quote / MoneyFlow / NewsItem / Announcement / DragonTiger / LimitPoolItem / ExternalQuote` 等 Pydantic 模型（字段口径见 ARCHITECTURE.md）。
- **缓存（TTL）**：`cache.py` 用「键 + 过期时间」包裹，TTL 见共享知识表（行情 10min / 新闻 2h / 财务 24h / 外围 5min / 日线永久）。
- **落库**：`store.py` 将数据写入本地 SQLite（默认 `data/prober.db`）；原始缓存表 `raw_*`，清洗事实表 `fact_*`；日线 / 公告等长期数据**永久保留**用于复盘与相似案例检索。
- **同一条时间轴**：所有外围行情与 A 股行情统一以「北京时间交易日 + 时刻」对齐，归因时按 `detected_at` 取「同期」外围表现。

---

## 4. 待明确 / 风险

- akshare 个别接口（如龙虎榜、解禁）返回结构随版本微调，实现时以**字段契约**驱动，做好解析容错。
- 东财 / 腾讯直连端点可能变更或加签名，备用源需预留「端点配置化 + 失败告警」。
- 盘中高频采集（分钟线 / 实时）需控制频率，避免触发反爬；建议盘中使用「分窗 + 限速」策略。
