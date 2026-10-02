"""研判与审批数据模型（T20）。

- ``ResearchReport``：LLM 双视角研判报告（含 bull_view/bear_view/confidence/conclusion）。
- ``ApprovalRequest``：人审申请（pending → approved/rejected 状态机）。
- ``make_report_id`` / ``make_request_id``：ID 规范（RPT-<uuid> / REQ-<uuid>）。

字段严格对齐 ARCHITECTURE 第 3.1 节类图。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


def make_report_id() -> str:
    """生成研判报告 ID：``RPT-<uuid8>``。"""
    return f"RPT-{uuid.uuid4().hex[:12]}"


def make_request_id() -> str:
    """生成人审申请 ID：``REQ-<uuid>``。"""
    return f"REQ-{uuid.uuid4().hex}"


class ResearchReport(BaseModel):
    """LLM 双视角研判报告（字段严格照 ARCHITECTURE 第 3.1 节）。

    ``bull_view`` / ``bear_view`` / ``confidence`` / ``conclusion`` 为研判核心字段，
    由 LLM 产出、经 pydantic 校验（缺失/类型错将抛 ``JudgementError``）；其余字段由系统补全。
    """

    model_config = ConfigDict(extra="ignore")

    bull_view: str  # 持有方视角
    bear_view: str  # 旁观方视角
    confidence: float  # 置信度 0~1
    conclusion: str  # 综合结论

    id: str = Field(default_factory=make_report_id)
    dossier_id: str = ""  # 关联 EventDossier.id
    created_at: datetime = Field(default_factory=datetime.now)
    model: str = ""  # 实际调用模型名


class ApprovalRequest(BaseModel):
    """人审申请（状态机 pending → approved/rejected）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=make_request_id)
    report_id: str = ""  # 关联 ResearchReport.id
    status: str = "pending"  # pending / approved / rejected
    created_at: datetime = Field(default_factory=datetime.now)
    decided_at: Optional[datetime] = None
    decision_note: str = ""  # 审批意见
    approver: str = ""  # 审批人
