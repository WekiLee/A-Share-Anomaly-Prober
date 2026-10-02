"""人审流（T21）。

``ApprovalFlow``：将研判结论封装为 ``ApprovalRequest``（pending），必须经 ``decide`` 批准
才视为可用；**绝不直接下单**（仅产出模拟审批申请）。
"""

from __future__ import annotations

import structlog
from datetime import datetime
from typing import Any, Optional

from ashare_prober.research.models import ApprovalRequest

logger = structlog.get_logger(__name__)


class ApprovalError(ValueError):
    """人审流错误（请求不存在 / 已决定不可更改）。"""


class ApprovalFlow:
    """人审状态机：pending → approved / rejected。"""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    def create_request(self, report: Any) -> ApprovalRequest:
        """为研判报告创建人审申请（status=pending，落库）。"""
        request = ApprovalRequest(report_id=report.id, status="pending")
        if self.store is not None:
            self.store.save_approval(request)
        return request

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        """按 id 读取人审申请；不存在返回 None。"""
        if self.store is not None:
            return self.store.get_approval(request_id)
        return None

    def decide(
        self,
        request_id: str,
        approve: bool,
        note: str = "",
        approver: str = "",
    ) -> ApprovalRequest:
        """审批：pending → approved/rejected，写 decided_at/decision_note/approver 并落库。

        幂等约定：对已决定且**结果一致**的重复 decide 直接返回原申请；对已决定但**结果冲突**
        的重复 decide 抛 ``ApprovalError``（状态不可更改）。
        """
        request = self.get(request_id)
        if request is None:
            raise ApprovalError(f"人审申请不存在：{request_id}")

        target_status = "approved" if approve else "rejected"
        if request.status != "pending":
            if request.status == target_status:
                # 同结果重复审批，幂等返回。
                logger.info("重复审批（幂等）", request_id=request_id, status=target_status)
                return request
            raise ApprovalError(
                f"人审申请 {request_id} 已决定（{request.status}），不可更改"
            )

        request.status = target_status
        request.decided_at = datetime.now()
        request.decision_note = note
        request.approver = approver
        if self.store is not None:
            self.store.save_approval(request)
        logger.info(
            "人审决定",
            request_id=request.id,
            report_id=request.report_id,
            status=request.status,
            approver=approver,
        )
        return request
