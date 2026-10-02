"""数据模型单测：实例化、字段校验、from_akshare_* 归一。"""

from __future__ import annotations

from datetime import date, datetime

from ashare_prober.data.models import (
    Announcement,
    DragonTiger,
    ExternalQuote,
    LimitPoolItem,
    MoneyFlow,
    NewsItem,
    Quote,
    Seat,
)


def test_quote_instantiate_and_fields() -> None:
    q = Quote(symbol="600600", name="测试", close=10.5, pct_change=3.2)
    assert q.symbol == "600600"
    assert q.close == 10.5
    assert q.open == 0.0  # 默认值
    assert q.date is None


def test_quote_from_akshare_chinese_keys() -> None:
    row = {
        "代码": "600600",
        "名称": "贵州茅台",
        "最新价": "1685.00",
        "涨跌幅": "2.35",
        "今开": "1670.00",
        "最高": "1690.00",
        "最低": "1660.00",
        "昨收": "1646.30",
        "成交量": "1234567",
        "成交额": "2000000000",
    }
    q = Quote.from_akshare_row(row, trade_date=date(2026, 10, 9))
    assert q.symbol == "600600"
    assert q.name == "贵州茅台"
    assert q.close == 1685.00
    assert q.pct_change == 2.35
    assert q.date == date(2026, 10, 9)
    assert q.volume == 1234567.0


def test_quote_from_akshare_english_keys_and_bad_values() -> None:
    row = {
        "symbol": "000001",
        "name": "平安银行",
        "close": 12.34,
        "pct_change": "-1.2",
        "volume": "",
        "amount": None,
    }
    q = Quote.from_akshare_row(row)
    assert q.symbol == "000001"
    assert q.close == 12.34
    assert q.pct_change == -1.2
    assert q.volume == 0.0  # 空串回退默认
    assert q.amount == 0.0  # None 回退默认


def test_newsitem_normalization() -> None:
    row = {
        "新闻标题": "公司签订重大合同",
        "新闻内容": "摘要内容",
        "发布时间": "2026-10-09 15:00:00",
        "文章来源": "东方财富",
        "新闻链接": "http://example.com/1",
    }
    n = NewsItem.from_akshare_row(row, symbol="600600")
    assert n.title == "公司签订重大合同"
    assert n.source == "东方财富"
    assert n.published_at == datetime(2026, 10, 9, 15, 0, 0)
    assert n.symbols == ["600600"]


def test_announcement_normalization() -> None:
    row = {
        "公告标题": "关于重大事项停牌",
        "公告类型": "重大事项",
        "公告日期": "2026-10-08",
        "公告链接": "http://example.com/a",
    }
    a = Announcement.from_akshare_row(row, stock_code="600600")
    assert a.title == "关于重大事项停牌"
    assert a.category == "重大事项"
    assert a.stock_code == "600600"
    assert a.published_at == datetime(2026, 10, 8)


def test_moneyflow_normalization() -> None:
    row = {
        "代码": "600600",
        "名称": "贵州茅台",
        "主力净流入-净额": "123456789",
        "散户净流入-净额": "-12345",
        "日期": "2026-10-09",
    }
    m = MoneyFlow.from_akshare_row(row)
    assert m.code == "600600"
    assert m.main_net_inflow == 123456789.0
    assert m.retail_net_inflow == -12345.0
    assert m.date == date(2026, 10, 9)


def test_dragon_tiger_and_seat() -> None:
    buy = [{"营业部名称": "机构专用", "买入金额": "8000000"}]
    sell = [{"营业部名称": "游资席位", "卖出金额": "3000000"}]
    dt = DragonTiger.from_akshare(
        stock_code="600600",
        day=date(2026, 10, 9),
        reason="日涨幅偏离值达7%",
        buy_seats=buy,
        sell_seats=sell,
        net_buy=5000000.0,
    )
    assert dt.stock_code == "600600"
    assert dt.reason == "日涨幅偏离值达7%"
    assert dt.net_buy == 5000000.0
    assert dt.buy_seats[0].name == "机构专用"
    assert dt.buy_seats[0].side == "buy"
    assert dt.sell_seats[0].side == "sell"
    # Seat 类方法
    seat = Seat.from_akshare_row({"席位": "X", "金额": "100"}, side="sell")
    assert seat.amount == 100.0 and seat.side == "sell"


def test_limit_pool_item_normalization() -> None:
    row = {
        "代码": "600600",
        "名称": "贵州茅台",
        "最新价": "1685.00",
        "涨跌幅": "10.00",
        "涨停原因": "业绩超预期",
        "连板数": "3",
        "日期": "2026-10-09",
    }
    item = LimitPoolItem.from_akshare_row(row)
    assert item.stock_code == "600600"
    assert item.price == 1685.00
    assert item.consecutive_boards == 3
    assert item.reason == "业绩超预期"


def test_external_quote_normalization() -> None:
    row = {
        "名称": "恒生科技指数",
        "最新价": "4500.0",
        "涨跌幅": "1.25",
    }
    ext = ExternalQuote.from_akshare_row(row, category="港股指数")
    assert ext.name == "恒生科技指数"
    assert ext.category == "港股指数"
    assert ext.last == 4500.0
    assert ext.pct_change == 1.25


def test_model_roundtrip_json() -> None:
    q = Quote(symbol="600600", close=10.0, date=date(2026, 10, 9))
    dumped = q.model_dump(mode="json")
    assert isinstance(dumped, dict)
    restored = Quote.model_validate(dumped)
    assert restored == q
