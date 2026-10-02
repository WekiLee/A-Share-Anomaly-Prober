"""事件档案模型单测（T13）。

验证各模型可实例化，且 JSON 往返（model_dump(mode="json") → model_validate_json）正确。
"""

from __future__ import annotations

from datetime import date, datetime

from ashare_prober.attribution.models import (
    CauseBlock,
    ConsequenceBlock,
    Evidence,
    EventDossier,
    SimilarCase,
    TransmissionChain,
    make_dossier_id,
)
from ashare_prober.data.models import (
    Announcement,
    DragonTiger,
    ExternalQuote,
    NewsItem,
    Seat,
)


def _sample_dossier() -> EventDossier:
    news = [
        NewsItem(
            id="N1",
            title="中标大单",
            source="东财",
            published_at=datetime(2026, 10, 9, 9, 0),
        )
    ]
    ann = [Announcement(id="A1", title="签订重大合同", stock_code="600600")]
    dt = DragonTiger(
        stock_code="600600",
        date=date(2026, 10, 9),
        reason="日涨幅偏离值达7%",
        buy_seats=[Seat(name="机构", amount=8_000_000, side="buy")],
        sell_seats=[Seat(name="游资", amount=3_000_000, side="sell")],
        net_buy=5_000_000,
    )
    ext = [ExternalQuote(name="恒生科技", category="港股指数", pct_change=1.1)]
    cause = CauseBlock(
        news=news,
        announcements=ann,
        dragon_tiger=dt,
        unlock_schedule=[{"date": "2026-10-15", "amount": 5_000_000, "ratio": 0.05}],
        sector_linkage=["半导体板块当日 +3.2%"],
        external_same_time=ext,
    )
    consequence = ConsequenceBlock(
        transmission_chain=TransmissionChain(
            upstream=["政策利好"], midstream=["板块共振"], downstream=["个股放量"]
        ),
        capital_acceptance="机构净买、游资接力",
        historical_similar="2025-xx 相似形态后续 3 日 +12%",
    )
    ev = Evidence(
        id="EVD-001",
        category="公告",
        content="签订大额订单",
        supports=True,
        source="巨潮",
        weight=0.8,
    )
    case = SimilarCase(
        date=date(2025, 8, 12), symbol="600xxx", pattern="放量突破", outcome="3日+12%"
    )
    return EventDossier(
        id=make_dossier_id("600600", date(2026, 10, 9)),
        anomaly_event_id="EVT-600600-20261009-001",
        version=1,
        created_at=datetime(2026, 10, 9, 15, 30),
        cause=cause,
        consequence=consequence,
        evidences=[ev],
        similar_cases=[case],
    )


def test_make_dossier_id_format() -> None:
    assert make_dossier_id("600600", date(2026, 10, 9)) == "DSS-600600-20261009"
    assert (
        make_dossier_id("600600", datetime(2026, 10, 9, 15, 30)) == "DSS-600600-20261009"
    )


def test_dossier_instantiate() -> None:
    d = _sample_dossier()
    assert d.id == "DSS-600600-20261009"
    assert d.cause.dragon_tiger is not None
    assert d.cause.dragon_tiger.net_buy == 5_000_000
    assert len(d.cause.news) == 1
    assert len(d.evidences) == 1
    assert d.evidences[0].supports is True


def test_dossier_json_roundtrip() -> None:
    d = _sample_dossier()
    payload = d.model_dump(mode="json")
    text = d.model_dump_json()
    back = EventDossier.model_validate_json(text)
    assert back.id == d.id
    assert back.anomaly_event_id == d.anomaly_event_id
    assert back.version == d.version
    assert back.cause.dragon_tiger is not None
    assert back.cause.dragon_tiger.net_buy == 5_000_000
    assert len(back.cause.news) == 1
    assert len(back.cause.external_same_time) == 1
    assert back.cause.external_same_time[0].pct_change == 1.1
    assert back.consequence.transmission_chain.midstream == ["板块共振"]
    assert len(back.evidences) == 1
    assert back.evidences[0].weight == 0.8
    assert len(back.similar_cases) == 1
    assert back.similar_cases[0].symbol == "600xxx"
    # 确保 round-trip 后重新序列化一致（结构性稳定）。
    assert EventDossier.model_validate(payload).id == d.id


def test_evidence_supports_bool() -> None:
    ev = Evidence(category="解禁", content="解禁", supports=False)
    assert ev.supports is False
    assert ev.weight == 0.6
