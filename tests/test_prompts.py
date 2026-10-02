"""多视角 Prompt 单测（T19）。

验证模板渲染含 dossier 关键字段、system 含双视角指令与固定 JSON schema。
"""

from __future__ import annotations

from datetime import datetime

from ashare_prober.attribution.models import EventDossier
from ashare_prober.research.prompts import PromptSet


def _dossier() -> EventDossier:
    return EventDossier(
        id="DSS-600600-20261009",
        anomaly_event_id="EVT-600600-20261009-001",
        created_at=datetime(2026, 10, 9, 15, 30),
    )


def test_build_messages_contains_dossier_fields() -> None:
    ps = PromptSet()
    system, user = ps.build_messages(_dossier())
    # user 含序列化档案关键字段
    assert "DSS-600600-20261009" in user
    assert "cause" in user
    assert "evidences" in user
    assert "consequence" in user


def test_system_has_dual_view_instructions() -> None:
    ps = PromptSet()
    system, _ = ps.build_messages(_dossier())
    assert "持有方" in system and "旁观方" in system
    # 固定 schema 字段
    for key in ("bull_view", "bear_view", "confidence", "conclusion"):
        assert key in system


def test_custom_template_sentinel_replaced() -> None:
    ps = PromptSet(user_template="档案如下：__DOSSIER__ 结束")
    _, user = ps.build_messages(_dossier())
    assert "__DOSSIER__" not in user
    assert "DSS-600600-20261009" in user
