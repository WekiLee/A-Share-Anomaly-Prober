"""异动规则引擎（② 异动规则引擎）。

对外暴露：事件模型、规则注册表、元规则与引擎主循环。
"""

from __future__ import annotations

from ashare_prober.anomaly.engine import AnomalyEngine
from ashare_prober.anomaly.meta_rules import MetaRule
from ashare_prober.anomaly.models import (
    AnomalyEvent,
    Level,
    MetaStatus,
    RuleContext,
    RuleResult,
    Scope,
    Status,
    make_anomaly_id,
)
from ashare_prober.anomaly.rules import (
    AuctionHook,
    BaseRule,
    LimitPoolHook,
    RULES,
    SectorFlowFlipRule,
    SectorRankJumpRule,
    VolumeBreakRule,
)

__all__ = [
    "AnomalyEngine",
    "MetaRule",
    "AnomalyEvent",
    "Level",
    "MetaStatus",
    "RuleContext",
    "RuleResult",
    "Scope",
    "Status",
    "make_anomaly_id",
    "BaseRule",
    "RULES",
    "LimitPoolHook",
    "AuctionHook",
    "SectorFlowFlipRule",
    "SectorRankJumpRule",
    "VolumeBreakRule",
]
