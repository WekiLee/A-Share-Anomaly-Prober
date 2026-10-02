"""归因引擎（③ 归因引擎，核心差异点）。

对外暴露：事件档案模型、证据归集器注册表、档案模板与归因引擎主流程。
"""

from __future__ import annotations

from ashare_prober.attribution.collectors import (
    AnnouncementCollector,
    Collector,
    COLLECTORS,
    DragonTigerCollector,
    ExternalCollector,
    NewsCollector,
    SectorLinkageCollector,
    UnlockCollector,
)
from ashare_prober.attribution.engine import AttributionEngine
from ashare_prober.attribution.models import (
    CauseBlock,
    ConsequenceBlock,
    Evidence,
    EventDossier,
    SimilarCase,
    TransmissionChain,
    make_dossier_id,
)
from ashare_prober.attribution.templates import build_blank_dossier

__all__ = [
    "AttributionEngine",
    "Collector",
    "COLLECTORS",
    "NewsCollector",
    "AnnouncementCollector",
    "DragonTigerCollector",
    "UnlockCollector",
    "SectorLinkageCollector",
    "ExternalCollector",
    "EventDossier",
    "CauseBlock",
    "ConsequenceBlock",
    "Evidence",
    "SimilarCase",
    "TransmissionChain",
    "make_dossier_id",
    "build_blank_dossier",
]
