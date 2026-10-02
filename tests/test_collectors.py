"""证据归集器单测（T14）。

注入假 Provider 返回预设 news/announcements/dragon_tiger，
验证 NewsCollector / AnnouncementCollector / DragonTigerCollector 产出正确，
且 Evidence supports 初判在已知标题下正确。
"""

from __future__ import annotations

from datetime import date, datetime

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.attribution.collectors import (
    AnnouncementCollector,
    Collector,
    DragonTigerCollector,
    NewsCollector,
    _heuristic_supports,
)
from ashare_prober.attribution.models import Evidence
from ashare_prober.data.models import Announcement, DragonTiger, NewsItem


class FakeProvider:
    """返回预设数据的假数据源。"""

    def __init__(self, boom: bool = False) -> None:
        self.boom = boom

    def get_news(self, symbol):
        if self.boom:
            raise RuntimeError("news boom")
        return [
            NewsItem(id="N1", title="公司中标重大订单", source="东财"),
            NewsItem(id="N2", title="股东计划减持预亏", source="巨潮"),
            NewsItem(id="N3", title="公司召开临时股东大会", source="中证"),
        ]

    def get_announcements(self, symbol, day):
        if self.boom:
            raise RuntimeError("ann boom")
        return [
            Announcement(id="A1", title="签订重大合同", stock_code=symbol),
            Announcement(id="A2", title="股东减持计划", stock_code=symbol),
        ]

    def get_dragon_tiger(self, symbol, start, end):
        if self.boom:
            raise RuntimeError("dt boom")
        return [
            DragonTiger(stock_code=symbol, reason="日涨幅偏离值达7%", net_buy=12_000_000),
            DragonTiger(stock_code=symbol, reason="日换手率达20%", net_buy=-3_000_000),
        ]

    def get_external(self, category):
        return []


def _event() -> AnomalyEvent:
    return AnomalyEvent(
        id="EVT-600600-20261009-001",
        ref_code="600600",
        detected_at=datetime(2026, 10, 9, 15, 30),
    )


def test_heuristic_supports() -> None:
    assert _heuristic_supports("公司中标重大订单") is True
    assert _heuristic_supports("股东减持预亏") is False
    assert _heuristic_supports("公司召开临时股东大会") is None
    assert _heuristic_supports("") is None


def test_news_collector_fills_cause() -> None:
    result = NewsCollector().collect(FakeProvider(), _event())
    assert "news" in result
    assert len(result["news"]) == 3
    # 标题混合了利好与利空关键词 → 无法判定 → evidence 为 None。
    assert result["evidence"] is None


def test_news_collector_positive_evidence() -> None:
    provider = FakeProvider()
    provider.get_news = lambda symbol: [
        NewsItem(id="N1", title="公司中标大单", source="东财")
    ]
    result = NewsCollector().collect(provider, _event())
    ev = result["evidence"]
    assert isinstance(ev, Evidence)
    assert ev.supports is True
    assert ev.category == "新闻"


def test_news_collector_negative_evidence() -> None:
    provider = FakeProvider()
    provider.get_news = lambda symbol: [
        NewsItem(id="N1", title="股东减持预亏", source="巨潮")
    ]
    result = NewsCollector().collect(provider, _event())
    ev = result["evidence"]
    assert isinstance(ev, Evidence)
    assert ev.supports is False


def test_news_collector_best_effort_on_boom() -> None:
    result = NewsCollector().collect(FakeProvider(boom=True), _event())
    assert result["news"] == []
    assert result["evidence"] is None


def test_announcement_collector_fills_cause() -> None:
    result = AnnouncementCollector().collect(FakeProvider(), _event())
    assert len(result["announcements"]) == 2
    # 标题同时含利好(合同)与利空(减持) → 无法判定。
    assert result["evidence"] is None


def test_dragon_tiger_collector_fills_cause_and_evidence() -> None:
    result = DragonTigerCollector().collect(FakeProvider(), _event())
    dt = result["dragon_tiger"]
    assert dt is not None
    assert dt.reason == "日涨幅偏离值达7%"
    ev = result["evidence"]
    assert isinstance(ev, Evidence)
    assert ev.supports is True  # 净买入为正
    assert ev.category == "龙虎榜"


def test_dragon_tiger_negative_evidence() -> None:
    provider = FakeProvider()
    provider.get_dragon_tiger = lambda s, a, b: [
        DragonTiger(stock_code=s, reason="上榜", net_buy=-5_000_000)
    ]
    result = DragonTigerCollector().collect(provider, _event())
    assert result["evidence"].supports is False
