"""大盘看板**聚合**模型。

本模块只保留聚合体 ``MarketDashboard``。其组成部分（指数行情 / 市场宽度 / 板块资金流）
都是**数据源产出的归一数据行**，已下沉到数据层 ``ashare_prober.data.models``
（见该模块「大盘类归一模型」小节）。此处 re-export 仅为保持对外 API 稳定——
``from ashare_prober.dashboard import IndexQuote`` 等既有写法继续可用。

**分层约束（重要）**：依赖必须单向为 ``dashboard → data``。数据层（尤其
``provider``）不得反向 import 本模块，否则会形成循环导入（历史上正因如此，
导致先 ``import ashare_prober.dashboard`` 时必崩）。

诚实性纪律：取不到的数据一律留空（``None`` / ``[]``），并在 ``gaps`` 中写明原因，
**不做任何数值编造**。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ashare_prober.data.models import (
    ExternalQuote,
    IndexQuote,
    LimitPoolItem,
    MarketBreadth,
    SectorFlow,
)

# 类型别名：避免字段名 ``date`` 与类型名 ``date`` 在注解解析时冲突
#（见 data/models.py 中同样的处理）。
Date = date
DateTime = datetime

# re-export：保持 ``from ashare_prober.dashboard.models import IndexQuote`` 等写法可用
__all__ = [
    "ExternalQuote",
    "IndexQuote",
    "LimitPoolItem",
    "MarketBreadth",
    "MarketDashboard",
    "SectorFlow",
    "TWO_MARKET_INDEX_CODES",
]

#: 两市合计成交额口径：上证指数 + 深证成指（见 ``MarketDashboard.total_amount``）。
TWO_MARKET_INDEX_CODES: tuple[str, ...] = ("000001", "399001")


class MarketDashboard(BaseModel):
    """大盘总览看板聚合体。

    ``gaps`` 是本项目诚实性纪律的载体：任何一项数据缺失，都必须在此写明原因，
    不允许以 0 / 占位值 / 估算值冒充实测数据。
    """

    model_config = ConfigDict(extra="ignore")

    date: Optional[Date] = None  # 数据日期（交易日）
    updated_at: datetime = Field(default_factory=datetime.now)  # 采集时间
    indices: list[IndexQuote] = Field(default_factory=list)
    breadth: Optional[MarketBreadth] = None
    sectors_top: list[SectorFlow] = Field(default_factory=list)  # 主力净流入前 N
    sectors_bottom: list[SectorFlow] = Field(default_factory=list)  # 净流出前 N
    limit_up: list[LimitPoolItem] = Field(default_factory=list)
    limit_down: list[LimitPoolItem] = Field(default_factory=list)
    external: list[ExternalQuote] = Field(default_factory=list)
    recent_events: list[dict] = Field(default_factory=list)  # 近期异动（dict 归一）
    watchlist: list[dict] = Field(default_factory=list)  # 监控名单（{code,name,active}）
    gaps: list[str] = Field(default_factory=list)  # 数据缺口说明

    @property
    def total_amount(self) -> float:
        """两市合计成交额（元）：上证指数(000001) + 深证成指(399001)。

        两者任一取不到时按 0 计入，故结果可能只含单市（属实反映，不做估算补齐）。
        """
        total = 0.0
        for item in self.indices:
            if item.code in TWO_MARKET_INDEX_CODES:
                total += float(item.amount or 0.0)
        return total


# 数值 / 字符串兜底转换工具（``_to_float`` / ``_to_str``）已随大盘归一模型
# 一并下沉至 ``ashare_prober.data.models``，本模块不再重复定义。
