"""人审流单测（T21）。

create→pending；decide(approve=True)→approved 且 decided_at 非空；decide(approve=False)→rejected；
store 往返一致；重复 decide 幂等（同结果）或报错（冲突结果）。
"""

from __future__ import annotations

import pytest
from datetime import datetime

from ashare_prober.data.store import Store
from ashare_prober.research.approval import ApprovalError, ApprovalFlow
from ashare_prober.research.models import ApprovalRequest, ResearchReport


def _report() -> ResearchReport:
    return ResearchReport(
        dossier_id="DSS-600600-20261009",
        bull_view="持有方观点",
        bear_view="旁观方观点",
        confidence=0.6,
        conclusion="综合结论",
    )


def test_create_request_is_pending(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    flow = ApprovalFlow(store=store)
    req = flow.create_request(_report())
    assert isinstance(req, ApprovalRequest)
    assert req.status == "pending"
    assert req.report_id


def test_decide_approve_sets_decided(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    flow = ApprovalFlow(store=store)
    req = flow.create_request(_report())
    decided = flow.decide(req.id, approve=True, note="同意", approver="alice")
    assert decided.status == "approved"
    assert decided.decided_at is not None
    assert decided.decision_note == "同意"
    assert decided.approver == "alice"


def test_decide_reject(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    flow = ApprovalFlow(store=store)
    req = flow.create_request(_report())
    decided = flow.decide(req.id, approve=False, approver="bob")
    assert decided.status == "rejected"
    assert decided.decided_at is not None


def test_store_roundtrip(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    flow = ApprovalFlow(store=store)
    req = flow.create_request(_report())
    decided = flow.decide(req.id, approve=True, note="ok", approver="alice")
    got = store.get_approval(req.id)
    assert got is not None
    assert got.id == req.id
    assert got.status == "approved"
    assert got.decision_note == "ok"
    # 报告也可落库往返（使用同一份报告对象以避免随机 id 错位）
    rpt = _report()
    store.save_report(rpt)
    got_rpt = store.get_report(rpt.id)
    assert got_rpt is not None
    assert got_rpt.id == rpt.id
    assert got_rpt.bull_view == "持有方观点"


def test_repeat_decide_idempotent_same_outcome(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    flow = ApprovalFlow(store=store)
    req = flow.create_request(_report())
    flow.decide(req.id, approve=True, approver="alice")
    again = flow.decide(req.id, approve=True, approver="alice")
    assert again.status == "approved"


def test_conflicting_decide_raises(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    flow = ApprovalFlow(store=store)
    req = flow.create_request(_report())
    flow.decide(req.id, approve=True, approver="alice")
    with pytest.raises(ApprovalError):
        flow.decide(req.id, approve=False, approver="bob")
