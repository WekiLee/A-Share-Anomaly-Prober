"""归因引擎主流程单测（T17）。

注入假 Provider + 假 AnomalyEvent，验证 build() 返回 EventDossier，
cause.news/announcements/dragon_tiger 已填充、evidences 非空，
且 save_dossier → get_dossier 往返一致（不联网）。
"""

from __future__ import annotations

from datetime import datetime

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.attribution.engine import AttributionEngine
from ashare_prober.data.models import Announcement, DragonTiger, NewsItem
from ashare_prober.data.store import Store


class FakeProvider:
    """返回预设数据的假数据源。"""

    def get_news(self, symbol):
        return [
            NewsItem(id="N1", title="公司中标重大订单", source="东财"),
            NewsItem(id="N2", title="公司回购股份", source="巨潮"),
        ]

    def get_announcements(self, symbol, day):
        return [Announcement(id="A1", title="签订重大合同", stock_code=symbol)]

    def get_dragon_tiger(self, symbol, start, end):
        return [DragonTiger(stock_code=symbol, reason="日涨幅偏离值达7%", net_buy=9_000_000)]

    def get_external(self, category):
        return []


def _event() -> AnomalyEvent:
    return AnomalyEvent(
        id="EVT-600600-20261009-001",
        ref_code="600600",
        detected_at=datetime(2026, 10, 9, 15, 30),
    )


def test_build_fills_cause_and_evidences(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    engine = AttributionEngine(provider=FakeProvider(), store=store)
    dossier = engine.build(_event())

    assert dossier.id == "DSS-600600-20261009"
    assert dossier.anomaly_event_id == "EVT-600600-20261009-001"
    assert len(dossier.cause.news) == 2
    assert len(dossier.cause.announcements) == 1
    assert dossier.cause.dragon_tiger is not None
    assert dossier.cause.dragon_tiger.net_buy == 9_000_000
    # 新闻标题含利好关键词 → 一条支持证据；龙虎榜净买为正 → 一条支持证据。
    assert len(dossier.evidences) >= 1
    assert all(ev.id.startswith("EVD-") for ev in dossier.evidences)


def test_build_persist_roundtrip(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    engine = AttributionEngine(provider=FakeProvider(), store=store)
    dossier = engine.build(_event())

    got = store.get_dossier(dossier.id)
    assert got is not None
    assert got.id == dossier.id
    assert got.anomaly_event_id == dossier.anomaly_event_id
    assert len(got.cause.news) == 2
    assert got.cause.dragon_tiger is not None
    assert got.cause.dragon_tiger.net_buy == 9_000_000
    assert len(got.evidences) == len(dossier.evidences)


def test_build_version_increments_on_rebuild(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    engine = AttributionEngine(provider=FakeProvider(), store=store)
    first = engine.build(_event())
    assert first.version == 1
    second = engine.build(_event())
    assert second.version == 2


def test_build_without_store_no_persist() -> None:
    engine = AttributionEngine(provider=FakeProvider(), store=None)
    dossier = engine.build(_event())
    assert dossier.cause.news  # 仍填充
    assert dossier.version == 1
