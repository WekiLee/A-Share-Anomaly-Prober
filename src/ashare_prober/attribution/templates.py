"""事件档案模板（T15）。

提供 ``build_blank_dossier(event)``：以 AnomalyEvent 为输入，生成一份「前因/后果」
空骨架的 ``EventDossier``，其中：
- ``cause`` / ``consequence`` 为空骨架（列表置空、嵌套对象就位）；
- ``evidences`` 初始为空（支持/反证双栏将在归集阶段按 Evidence.supports 填充）；
- ``consequence.transmission_chain`` 初为空列表；
- ``capital_acceptance`` / ``historical_similar`` 置占位串（待研判层/历史检索填充）。
"""

from __future__ import annotations

from datetime import datetime

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.attribution.models import (
    CauseBlock,
    ConsequenceBlock,
    EventDossier,
    TransmissionChain,
)
from ashare_prober.config import make_dossier_id


def build_blank_dossier(event: AnomalyEvent) -> EventDossier:
    """从异动事件构造空白事件档案骨架。

    Args:
        event: 已裁决/初判的异动事件（提供 ref_code / id / detected_at）。

    Returns:
        字段基本就位、内容待填充的 ``EventDossier``。
    """
    return EventDossier(
        id=make_dossier_id(event.ref_code, event.detected_at),
        anomaly_event_id=event.id,
        version=1,
        created_at=datetime.now(),
        cause=CauseBlock(),
        consequence=ConsequenceBlock(
            transmission_chain=TransmissionChain(),
            capital_acceptance="（待研判）",
            historical_similar="（暂无历史样本）",
        ),
        evidences=[],
        similar_cases=[],
    )
