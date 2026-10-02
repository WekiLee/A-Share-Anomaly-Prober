"""本地库单测：建表 + upsert + 读写往返。"""

from __future__ import annotations

from datetime import date, datetime

from ashare_prober.data.models import NewsItem, Quote
from ashare_prober.data.store import Store


def test_init_schema_and_quote_roundtrip(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    q = Quote(
        symbol="600600",
        name="贵州茅台",
        date=date(2026, 10, 9),
        open=1670,
        close=1685,
        pct_change=2.35,
        volume=12345,
        amount=2e9,
    )
    store.save_quote(q)
    got = store.get_quote_raw("600600", date(2026, 10, 9))
    assert got is not None
    assert got.symbol == "600600"
    assert got.close == 1685.0
    assert got.pct_change == 2.35

    # 不存在返回 None
    assert store.get_quote_raw("600600", date(2020, 1, 1)) is None


def test_news_roundtrip_and_symbol_filter(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    items = [
        NewsItem(
            id="N1",
            source="东方财富",
            title="A 相关新闻",
            published_at=datetime(2026, 10, 9, 15, 0),
            symbols=["600600"],
        ),
        NewsItem(
            id="N2",
            source="新浪",
            title="B 相关新闻",
            published_at=datetime(2026, 10, 9, 14, 0),
            symbols=["000001"],
        ),
    ]
    assert store.save_news(items) == 2

    # 全量
    all_news = store.get_news()
    assert len(all_news) == 2

    # 按标的过滤
    filtered = store.get_news(symbol="600600")
    assert len(filtered) == 1
    assert filtered[0].id == "N1"

    # 按时间过滤
    since = datetime(2026, 10, 9, 14, 30)
    recent = store.get_news(since=since)
    assert len(recent) == 1 and recent[0].id == "N1"


def test_upsert_idempotent(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    # save_quote 内部走 upsert（按 symbol+date 主键），重复写入应覆盖而非重复。
    store.save_quote(
        Quote(symbol="600600", date=date(2026, 10, 9), close=100.0, name="茅台")
    )
    store.save_quote(
        Quote(symbol="600600", date=date(2026, 10, 9), close=200.0, name="茅台")
    )
    q = store.get_quote_raw("600600", date(2026, 10, 9))
    assert q is not None
    assert q.close == 200.0  # 被更新覆盖
