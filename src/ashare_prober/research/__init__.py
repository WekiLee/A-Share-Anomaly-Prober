"""研判 + 人审（④ 研判层）。

对外暴露：研判模型、LLM 客户端、双视角 prompt、研判生成器与人审流。
"""

from __future__ import annotations

from ashare_prober.research.approval import ApprovalError, ApprovalFlow
from ashare_prober.research.judge import Judge, JudgementError
from ashare_prober.research.llm_client import (
    LLMClient,
    LLMError,
    OpenAICompatibleClient,
)
from ashare_prober.research.models import (
    ApprovalRequest,
    ResearchReport,
    make_report_id,
    make_request_id,
)
from ashare_prober.research.prompts import PromptSet

__all__ = [
    "ApprovalError",
    "ApprovalFlow",
    "Judge",
    "JudgementError",
    "LLMClient",
    "LLMError",
    "OpenAICompatibleClient",
    "ApprovalRequest",
    "ResearchReport",
    "make_report_id",
    "make_request_id",
    "PromptSet",
]
