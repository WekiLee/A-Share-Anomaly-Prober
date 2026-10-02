"""渲染器单测（T23）。

构造假 dossier + report + approval，验证：
- 渲染结果包含红涨 / 绿跌配色（#d70015 / #008a3e）；
- 包含标的代码与 bull_view 文本；
- 不含任何外部 http(s) 外链（自包含、可离线打开）。
"""

from __future__ import annotations

from datetime import datetime

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.attribution.models import (
    CauseBlock,
    ConsequenceBlock,
    EventDossier,
    Evidence,
)
from ashare_prober.presentation import Render
from ashare_prober.research.models import ApprovalRequest, ResearchReport


def _event() -> AnomalyEvent:
    return AnomalyEvent(
        id="EVT-600600-20261009-001",
        ref_code="600600",
        ref_name="青岛啤酒",
        type="volume_break_down",
        level="alert",
        detected_at=datetime(2026, 10, 9, 15, 0),
    )


def _dossier() -> EventDossier:
    return EventDossier(
        id="DSS-600600-20261009",
        anomaly_event_id="EVT-600600-20261009-001",
        cause=CauseBlock(
            news=[],
            announcements=[],
            dragon_tiger=None,
            unlock_schedule=[{"date": "2026-10-15", "amount": 5_000_000, "ratio": 0.05}],
            sector_linkage=["啤酒板块当日 -2.1%"],
            external_same_time=[],
        ),
        consequence=ConsequenceBlock(
            capital_acceptance="机构净卖、承接偏弱",
            historical_similar="（暂无历史样本）",
        ),
        evidences=[
            Evidence(
                id="EVD-001", category="公告", content="下调盈利预测",
                supports=False, source="券商", weight=0.7,
            ),
            Evidence(
                id="EVD-002", category="新闻", content="旺季销量超预期",
                supports=True, source="东财", weight=0.6,
            ),
        ],
        similar_cases=[],
    )


def _report() -> ResearchReport:
    return ResearchReport(
        dossier_id="DSS-600600-20261009",
        bull_view="（LLM 未配置，跳过研判）持有方观点占位",
        bear_view="（LLM 未配置，跳过研判）旁观方观点占位",
        confidence=0.0,
        conclusion="请配置 LLM 后重新研判",
    )


def _approval() -> ApprovalRequest:
    return ApprovalRequest(report_id="RPT-x", status="pending")


def test_render_contains_colors_and_code_and_bull_view() -> None:
    dossier = _dossier()
    report = _report()
    approval = _approval()
    event = _event()
    html = Render().render_dossier(dossier, report, approval, event=event)
    # 红涨绿跌配色必须出现（CSS 变量定义）。
    assert "#d70015" in html
    assert "#008a3e" in html
    # 标的代码与研判文本应出现。
    assert "600600" in html
    assert "（LLM 未配置，跳过研判）持有方观点占位" in html


def test_render_is_self_contained_no_external_links() -> None:
    dossier = _dossier()
    html = Render().render_dossier(dossier, _report(), _approval())
    # 禁止任何外部 http(s) 外链。
    assert "http://" not in html
    assert "https://" not in html


def test_render_card_contains_code_and_colors() -> None:
    dossier = _dossier()
    card = Render().render_card(dossier, event=_event())
    assert "600600" in card
    assert "#d70015" in card
    assert "#008a3e" in card
