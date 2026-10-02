"""研判生成（T20）。

``Judge``：序列化事件档案 → 组装双视角 prompt → 调用 LLM → 用 pydantic 校验 JSON →
产出 ``ResearchReport``。LLM 返回字段缺失/类型错误时抛 ``JudgementError``（附原始响应）。
"""

from __future__ import annotations

import json
from typing import Optional

from ashare_prober.attribution.models import EventDossier
from ashare_prober.research.llm_client import LLMClient
from ashare_prober.research.models import ResearchReport
from ashare_prober.research.prompts import PromptSet


class JudgementError(ValueError):
    """研判失败（LLM 调用异常 / 返回 JSON 校验失败）。"""


class Judge:
    """双视角研判生成器。"""

    def __init__(
        self,
        llm: LLMClient,
        prompts: Optional[PromptSet] = None,
    ) -> None:
        self.llm = llm
        self.prompts = prompts or PromptSet()

    def generate(self, dossier: EventDossier) -> ResearchReport:
        """对给定事件档案生成研判报告。

        Args:
            dossier: 事件档案。

        Raises:
            JudgementError: LLM 调用失败或返回 JSON 不符合 ResearchReport schema。
        """
        system, user = self.prompts.build_messages(dossier)
        try:
            data = self.llm.chat_json(system, user)
        except Exception as exc:  # noqa: BLE001
            raise JudgementError(f"LLM 调用失败：{exc}") from exc

        if not isinstance(data, dict):
            raise JudgementError(
                f"LLM 返回非 JSON 对象：{json.dumps(data, ensure_ascii=False)[:500]}"
            )

        try:
            report = ResearchReport.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            raise JudgementError(
                f"研判结果 JSON 校验失败：{exc}；"
                f"原始响应：{json.dumps(data, ensure_ascii=False)[:500]}"
            ) from exc

        # 补充档案关联与模型名（LLM 不应自行生成这些字段）。
        report.dossier_id = dossier.id
        report.model = getattr(self.llm, "model", "") or ""
        return report
