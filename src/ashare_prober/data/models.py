"""数据模型（T03）。

用 pydantic v2 定义数据层归一模型，字段口径严格对齐 ARCHITECTURE.md 第 3.1 节类图：
- ``Quote`` 个股行情快照
- ``MoneyFlow`` 个股资金流
- ``NewsItem`` 个股新闻
- ``Announcement`` 公告
- ``DragonTiger`` 龙虎榜（含买卖席位）
- ``Seat`` 龙虎榜席位
- ``LimitPoolItem`` 涨跌停池条目
- ``ExternalQuote`` 外围行情（港股/美股/期货/汇率等）

每个模型提供 ``from_akshare_*`` 类方法，将主源（akshare）原始行归一为本模型，
键名兼容「中文列名」与「英文别名」，缺失字段以 ``0 / "" / []`` 兜底，保证归一稳健。

大盘看板相关归一模型（``IndexQuote`` / ``MarketBreadth`` / ``SectorFlow``）同属
**数据源产出的归一数据行**，故也定义在本文件（数据层），供
``dashboard``（表现/聚合层）单向引用，避免 dashboard → data → dashboard 循环导入。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

# 类型别名：避免字段名 ``date`` 与类型名 ``date`` 在 pydantic 前向引用解析时冲突
#（见 from __future__ import annotations 下的字符串注解解析）。
Date = date
DateTime = datetime

# --------------------------------------------------------------------------- #
# 通用工具：容错取值与类型转换
# --------------------------------------------------------------------------- #
def _pick(row: Mapping[str, Any], *keys: str) -> Any:
    """从行中按候选键顺序取第一个存在且非 None 的值；都没有则返回 None。"""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    """将任意值转为 float；空串 / None / 非数字均回退到 default。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "--", "None", "nan", "NaN"):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _to_str(value: Any, default: str = "") -> str:
    """将任意值转为 str；None 回退到 default。"""
    if value is None:
        return default
    text = str(value).strip()
    return text if text not in ("None", "nan") else default


def _to_date(value: Any) -> Optional[date]:
    """尽力将值解析为 date；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    # 常见格式：2026-10-09 / 20261009 / 2026/10/09
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _to_datetime(value: Any) -> Optional[datetime]:
    """尽力将值解析为 datetime；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# 模型定义
# --------------------------------------------------------------------------- #
class Quote(BaseModel):
    """个股行情快照（日线 / 实时归一）。"""

    model_config = ConfigDict(extra="ignore")

    symbol: str  # 代码，如 "600600"
    name: str = ""
    date: Optional[Date] = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    pre_close: float = 0.0
    volume: float = 0.0  # 成交量（手）
    amount: float = 0.0  # 成交额（元）
    pct_change: float = 0.0  # 涨跌幅（%）

    @classmethod
    def from_akshare_row(
        cls, row: Mapping[str, Any], trade_date: Optional[date] = None
    ) -> "Quote":
        """从 akshare 行情行（如 stock_zh_a_spot_em / stock_zh_a_hist）归一。

        Args:
            row: 一行原始数据，键兼容中文与英文别名。
            trade_date: 实时快照无日期时由调用方传入交易日。
        """
        return cls(
            symbol=_to_str(_pick(row, "symbol", "代码", "code")),
            name=_to_str(_pick(row, "name", "名称", "name")),
            date=_to_date(_pick(row, "date", "日期")) or trade_date,
            open=_to_float(_pick(row, "open", "今开", "开盘")),
            high=_to_float(_pick(row, "high", "最高")),
            low=_to_float(_pick(row, "low", "最低")),
            close=_to_float(_pick(row, "close", "最新价", "收盘价", "收盘")),
            pre_close=_to_float(_pick(row, "pre_close", "昨收")),
            volume=_to_float(_pick(row, "volume", "成交量")),
            amount=_to_float(_pick(row, "amount", "成交额")),
            pct_change=_to_float(_pick(row, "pct_change", "涨跌幅")),
        )


class MoneyFlow(BaseModel):
    """个股资金流（主力 / 散户净流入）。"""

    model_config = ConfigDict(extra="ignore")

    code: str
    name: str = ""
    date: Optional[Date] = None
    main_net_inflow: float = 0.0  # 主力净流入（元）
    retail_net_inflow: float = 0.0  # 散户净流入（元）
    rank: int = 0  # 行业/市场排名

    @classmethod
    def from_akshare_row(
        cls, row: Mapping[str, Any], trade_date: Optional[date] = None
    ) -> "MoneyFlow":
        """从 stock_individual_fund_flow 等行归一。"""
        return cls(
            code=_to_str(_pick(row, "code", "代码", "stock")),
            name=_to_str(_pick(row, "name", "名称")),
            date=_to_date(_pick(row, "date", "日期")) or trade_date,
            main_net_inflow=_to_float(
                _pick(row, "main_net_inflow", "主力净流入-净额", "主力净流入")
            ),
            retail_net_inflow=_to_float(
                _pick(row, "retail_net_inflow", "散户净流入-净额", "散户净流入")
            ),
            rank=int(_to_float(_pick(row, "rank", "排名"))),
        )


class NewsItem(BaseModel):
    """个股新闻条目。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    source: str = ""  # 来源，如 "东方财富"
    title: str = ""
    url: str = ""
    published_at: Optional[datetime] = None
    summary: str = ""
    symbols: list[str] = Field(default_factory=list)

    @classmethod
    def from_akshare_row(
        cls, row: Mapping[str, Any], symbol: Optional[str] = None
    ) -> "NewsItem":
        """从 stock_news_em 等行归一；symbol 用于补全关联标的。"""
        title = _to_str(_pick(row, "title", "新闻标题", "标题"))
        source = _to_str(_pick(row, "source", "文章来源", "新闻来源"))
        url = _to_str(_pick(row, "url", "新闻链接", "链接"))
        published_at = _to_datetime(
            _pick(row, "published_at", "发布时间", "时间")
        )
        summary = _to_str(_pick(row, "summary", "新闻内容", "内容", "摘要"))
        item_id = _to_str(_pick(row, "id", "新闻ID"))
        # 关联标的：优先用行内字段，否则用入参 symbol。
        symbols = []
        raw_symbols = _pick(row, "symbols", "codes", "代码")
        if raw_symbols:
            if isinstance(raw_symbols, str):
                symbols = [s.strip() for s in raw_symbols.split(",") if s.strip()]
            elif isinstance(raw_symbols, (list, tuple)):
                symbols = [str(s) for s in raw_symbols]
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        return cls(
            id=item_id,
            source=source,
            title=title,
            url=url,
            published_at=published_at,
            summary=summary,
            symbols=symbols,
        )


class Announcement(BaseModel):
    """个股公告。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    stock_code: str = ""
    title: str = ""
    published_at: Optional[datetime] = None
    category: str = ""  # 公告类别，如 "重大事项"
    url: str = ""

    @classmethod
    def from_akshare_row(
        cls, row: Mapping[str, Any], stock_code: Optional[str] = None
    ) -> "Announcement":
        """从 stock_notice_report / 巨潮披露行归一。"""
        return cls(
            id=_to_str(_pick(row, "id", "公告ID")),
            stock_code=_to_str(_pick(row, "stock_code", "代码", "stock")) or (stock_code or ""),
            title=_to_str(_pick(row, "title", "公告标题", "标题")),
            published_at=_to_datetime(_pick(row, "published_at", "公告日期", "日期")),
            category=_to_str(_pick(row, "category", "公告类型", "类别")),
            url=_to_str(_pick(row, "url", "公告链接", "链接")),
        )


class Seat(BaseModel):
    """龙虎榜席位（买卖方向 + 金额）。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    amount: float = 0.0  # 成交金额（元）
    side: str = "buy"  # "buy" / "sell"

    @classmethod
    def from_akshare_row(
        cls, row: Mapping[str, Any], side: str = "buy"
    ) -> "Seat":
        """从龙虎榜席位行归一。"""
        return cls(
            name=_to_str(_pick(row, "name", "营业部名称", "席位")),
            amount=_to_float(_pick(row, "amount", "买入金额", "卖出金额", "金额")),
            side=side,
        )


class DragonTiger(BaseModel):
    """龙虎榜明细（含买卖席位与净额）。"""

    model_config = ConfigDict(extra="ignore")

    stock_code: str = ""
    date: Optional[Date] = None
    reason: str = ""  # 上榜原因，如 "日涨幅偏离值达7%"
    buy_seats: list[Seat] = Field(default_factory=list)
    sell_seats: list[Seat] = Field(default_factory=list)
    net_buy: float = 0.0  # 净买入（元）

    @classmethod
    def from_akshare(
        cls,
        stock_code: str,
        day: Optional[date],
        reason: str = "",
        buy_seats: Optional[Sequence[Mapping[str, Any]]] = None,
        sell_seats: Optional[Sequence[Mapping[str, Any]]] = None,
        net_buy: float = 0.0,
    ) -> "DragonTiger":
        """由已解析的席位列表组装龙虎榜。"""
        return cls(
            stock_code=stock_code,
            date=day,
            reason=reason,
            buy_seats=[Seat.from_akshare_row(r, side="buy") for r in (buy_seats or [])],
            sell_seats=[Seat.from_akshare_row(r, side="sell") for r in (sell_seats or [])],
            net_buy=_to_float(net_buy),
        )


class LimitPoolItem(BaseModel):
    """涨跌停池条目（异动触发信号）。"""

    model_config = ConfigDict(extra="ignore")

    stock_code: str = ""
    name: str = ""
    price: float = 0.0
    pct_change: float = 0.0
    reason: str = ""  # 涨停/跌停原因
    consecutive_boards: int = 0  # 连板数
    date: Optional[Date] = None

    @classmethod
    def from_akshare_row(
        cls, row: Mapping[str, Any], trade_date: Optional[date] = None
    ) -> "LimitPoolItem":
        """从 stock_zt_pool_em / stock_dt_pool_em 等行归一。"""
        return cls(
            stock_code=_to_str(_pick(row, "stock_code", "代码")),
            name=_to_str(_pick(row, "name", "名称")),
            price=_to_float(_pick(row, "price", "最新价", "价格")),
            pct_change=_to_float(_pick(row, "pct_change", "涨跌幅")),
            reason=_to_str(_pick(row, "reason", "涨停原因", "跌停原因", "原因")),
            consecutive_boards=int(
                _to_float(_pick(row, "continuous_board_num", "连板数", "continuity"))
            ),
            date=_to_date(_pick(row, "date", "日期")) or trade_date,
        )


class ExternalQuote(BaseModel):
    """外围行情（港股/美股指数、A50、汇率、大宗等）。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    category: str = ""  # 如 "港股指数" / "美股指数" / "期货" / "汇率"
    timestamp: Optional[datetime] = None
    last: float = 0.0  # 最新价
    pct_change: float = 0.0  # 涨跌幅（%）

    @classmethod
    def from_akshare_row(
        cls, row: Mapping[str, Any], category: str = "", trade_time: Optional[datetime] = None
    ) -> "ExternalQuote":
        """从外围行情行（如 stock_hk_index_spot_em）归一。"""
        return cls(
            name=_to_str(_pick(row, "name", "名称")),
            category=category or _to_str(_pick(row, "category", "类别")),
            timestamp=_to_datetime(_pick(row, "timestamp", "时间")) or trade_time,
            last=_to_float(_pick(row, "last", "最新价", "price", "现价")),
            pct_change=_to_float(_pick(row, "pct_change", "涨跌幅", "涨跌额")),
        )


# --------------------------------------------------------------------------- #
# 大盘类归一模型（供 dashboard 层聚合使用）
#
# 分层说明：这三个模型是**数据源产出的归一数据行**，属于数据层；dashboard 层
# 只做聚合。历史上曾误置于 ``dashboard.models``，导致 ``data.provider`` 反向依赖
# dashboard 而形成循环导入（先 import dashboard 必崩）。下沉至此以保证依赖单向：
#   dashboard → data
# --------------------------------------------------------------------------- #
class IndexQuote(BaseModel):
    """指数行情快照。"""

    model_config = ConfigDict(extra="ignore")

    code: str = ""  # 指数代码，如 "000001"
    name: str = ""  # 指数名称，如 "上证指数"
    last: float = 0.0  # 最新点位
    pct_change: float = 0.0  # 涨跌幅（%）
    amount: float = 0.0  # 成交额（元）
    change: float = 0.0  # 涨跌额（点）

    @classmethod
    def from_row(
        cls, row: Mapping[str, Any], trade_date: Optional[date] = None
    ) -> "IndexQuote":
        """从任意行情行归一（键兼容中文列名与英文别名）。

        Args:
            row: 一行原始数据。
            trade_date: 交易日（本模型不落日期字段，仅用于签名兼容）。
        """
        del trade_date  # 指数快照不落日期字段，显式标记入参未使用
        return cls(
            code=_to_str(_pick(row, "code", "代码", "symbol")),
            name=_to_str(_pick(row, "name", "名称")),
            last=_to_float(_pick(row, "last", "最新价", "price", "现价", "收盘")),
            pct_change=_to_float(_pick(row, "pct_change", "涨跌幅")),
            amount=_to_float(_pick(row, "amount", "成交额")),
            change=_to_float(_pick(row, "change", "涨跌额")),
        )


class MarketBreadth(BaseModel):
    """市场宽度（涨跌家数统计）。"""

    model_config = ConfigDict(extra="ignore")

    up_count: int = 0  # 上涨家数（涨跌幅 > 0）
    down_count: int = 0  # 下跌家数（涨跌幅 < 0）
    flat_count: int = 0  # 平盘家数（涨跌幅 == 0）
    limit_up_count: int = 0  # 涨停家数
    limit_down_count: int = 0  # 跌停家数
    total: int = 0  # 统计样本总数

    @property
    def up_ratio(self) -> float:
        """上涨家数占比（%）；``total`` 为 0 时返回 ``0.0``（不做除零）。"""
        if not self.total:
            return 0.0
        return round(self.up_count * 100.0 / self.total, 2)


class SectorFlow(BaseModel):
    """板块资金流（主力净流入）。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""  # 板块名称
    pct_change: float = 0.0  # 板块涨跌幅（%）
    main_net_inflow: float = 0.0  # 主力净流入（元；负值表示净流出）
    rank: int = 0  # 排名：正数=净流入榜第 n 名；负数=净流出榜第 n 名
