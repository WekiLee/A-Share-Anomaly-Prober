"""数据层包（① 数据层）。

对外暴露归一模型、数据源抽象、缓存、落库与统一门面，便于上层（异动 / 归因 / 研判）
仅依赖 ``Provider`` 即可取得归一数据，屏蔽主/备源差异。
"""

from __future__ import annotations

from ashare_prober.data.base import (
    CacheMiss,
    DataSource,
    SourceError,
)
from ashare_prober.data.cache import Cache
from ashare_prober.data.models import (
    Announcement,
    DragonTiger,
    ExternalQuote,
    IndexQuote,
    LimitPoolItem,
    MarketBreadth,
    MoneyFlow,
    NewsItem,
    Quote,
    Seat,
    SectorFlow,
)
from ashare_prober.data.provider import Provider, build_default_provider
from ashare_prober.data.store import Store

__all__ = [
    "Announcement",
    "Cache",
    "CacheMiss",
    "DataSource",
    "DragonTiger",
    "ExternalQuote",
    "IndexQuote",
    "LimitPoolItem",
    "MarketBreadth",
    "MoneyFlow",
    "NewsItem",
    "Provider",
    "Quote",
    "Seat",
    "SectorFlow",
    "SourceError",
    "Store",
    "build_default_provider",
]
