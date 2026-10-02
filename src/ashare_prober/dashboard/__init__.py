"""大盘看板包（① 默认页：大盘总览）。

对外暴露：
- 看板归一模型（``IndexQuote`` / ``MarketBreadth`` / ``SectorFlow`` / ``MarketDashboard``）
- 采集器（``MarketDashboardBuilder`` / ``build_dashboard``）

设计原则：
- **诚实性纪律**：任何一项数据取不到，一律留空并写入 ``MarketDashboard.gaps``，
  严禁编造数值填充；看板页首会显眼地展示「数据缺口」。
- **降级不阻断**：单项采集失败只记 warning + gap，绝不影响其它项。
"""

from __future__ import annotations

from ashare_prober.dashboard.collector import (
    DEFAULT_INDEX_CODES,
    MarketDashboardBuilder,
    build_dashboard,
)
from ashare_prober.dashboard.models import (
    IndexQuote,
    MarketBreadth,
    MarketDashboard,
    SectorFlow,
)

__all__ = [
    "DEFAULT_INDEX_CODES",
    "IndexQuote",
    "MarketBreadth",
    "MarketDashboard",
    "MarketDashboardBuilder",
    "SectorFlow",
    "build_dashboard",
]
