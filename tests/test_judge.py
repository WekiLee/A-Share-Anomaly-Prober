"""研判生成单测（T20）。

- 假 LLM 返回合法 JSON → 产出 ResearchReport 且字段完整。
- 假 LLM 返回非法 JSON（缺字段）→ 抛 JudgementError。
- LLM 调用本身抛错 → 抛 JudgementError。
"""

from __future__ import annotations

import pytest
from datetime import datetime

from ashare_prober.attribution.models import EventDossier
from ashare_prober.research.judge import JudgementError, Judge
from ashare_prober.research.llm_client import LLMClient
from ashare_prober.research.models import ResearchReport


class FakeLLMClient(LLMClient):
    model = "fake-model"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        return dict(self.payload)


class BoomLLMClient(LLMClient):
    model = "boom"

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        raise RuntimeError("llm down")


def _dossier() -> EventDossier:
    return EventDossier(
        id="DSS-600600-20261009",
        anomaly_event_id="EVT-600600-20261009-001",
        created_at=datetime(2026, 10, 9, 15, 30),
    )


def test_judge_generates_complete_report() -> None:
    payload = {
        "bull_view": "资金持续流入",
        "bear_view": "解禁压力",
        "confidence": 0.65,
        "conclusion": "短线偏强但需警惕回落",
    }
    judge = Judge(llm=FakeLLMClient(payload))
    report = judge.generate(_dossier())
    assert isinstance(report, ResearchReport)
    assert report.bull_view == "资金持续流入"
    assert report.bear_view == "解禁压力"
    assert report.confidence == 0.65
    assert report.conclusion == "短线偏强但需警惕回落"
    assert report.dossier_id == "DSS-600600-20261009"
    assert report.model == "fake-model"


def test_judge_raises_on_missing_fields() -> None:
    # 缺 confidence / conclusion → pydantic 校验失败 → JudgementError
    bad = {"bull_view": "x", "bear_view": "y"}
    judge = Judge(llm=FakeLLMClient(bad))
    with pytest.raises(JudgementError):
        judge.generate(_dossier())


def test_judge_raises_on_llm_error() -> None:
    judge = Judge(llm=BoomLLMClient())
    with pytest.raises(JudgementError):
        judge.generate(_dossier())
