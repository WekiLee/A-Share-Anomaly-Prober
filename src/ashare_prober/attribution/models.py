"""事件档案模型（T13）。

定义归因引擎的核心数据结构，字段严格对齐 ARCHITECTURE.md 第 3.1 节类图：
- ``EventDossier``：事件档案（前因/后果/证据/相似案例 + 版本留痕）。
- ``CauseBlock``：前因（新闻/公告/龙虎榜/解禁/联动/外围同期）。
- ``ConsequenceBlock``：后果（传导链/资金承接/历史相似）。
- ``TransmissionChain``：上下游传导链。
- ``Evidence``：证据（支持/反证 + 权重）。
- ``SimilarCase``：历史相似案例。
- ``make_dossier_id``：档案 ID 生成（复用 config 的 ``DSS-<code>-<date>`` 规范）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ashare_prober.config import make_dossier_id as _make_dossier_id  # 复用 ID 规范
from ashare_prober.data.models import (
    Announcement,
    DragonTiger,
    ExternalQuote,
    NewsItem,
)

# 类型别名：避免字段名 ``date`` 与类型名 ``date`` 在 pydantic 前向引用解析时冲突
#（见 from __future__ import annotations 下的字符串注解解析，data/models.py 同源处理）。
Date = date
DateTime = datetime


class TransmissionChain(BaseModel):
    """上下游传导链（前因如何传导至后果）。"""

    model_config = ConfigDict(extra="ignore")

    upstream: list[str] = Field(default_factory=list)  # 上游触发源
    midstream: list[str] = Field(default_factory=list)  # 中游传导环节
    downstream: list[str] = Field(default_factory=list)  # 下游影响标的/板块


class Evidence(BaseModel):
    """单条证据（支持/反证双栏中的一项）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""  # 证据编号（引擎落库前统一分配）
    category: str = ""  # 新闻/公告/龙虎榜/解禁/联动/外围
    content: str = ""
    supports: bool = True  # 支持（True）/ 反证（False）
    source: str = ""  # 数据来源
    weight: float = 0.6  # 初值权重（0~1）


class SimilarCase(BaseModel):
    """历史相似案例。"""

    model_config = ConfigDict(extra="ignore")

    date: Optional[Date] = None
    symbol: str = ""
    pattern: str = ""  # 形态/触发模式
    outcome: str = ""  # 后续表现
    # T16 扩展字段（带默认值，保证旧 dossier JSON 缺键时照常加载，向后兼容）：
    similarity: float = 0.0  # 相似度得分（0.0~1.0，由相似案例检索填充）
    reason: str = ""  # 相似原因说明（只含真实匹配的维度，不凑数）


class CauseBlock(BaseModel):
    """前因块：异动发生前的同期信息归集。"""

    model_config = ConfigDict(extra="ignore")

    news: list[NewsItem] = Field(default_factory=list)
    announcements: list[Announcement] = Field(default_factory=list)
    dragon_tiger: Optional[DragonTiger] = None
    unlock_schedule: list[dict] = Field(default_factory=list)  # 解禁日历
    sector_linkage: list[str] = Field(default_factory=list)  # 同板块联动描述
    external_same_time: list[ExternalQuote] = Field(default_factory=list)  # 外围同期


class ConsequenceBlock(BaseModel):
    """后果块：传导链与承接/历史参照。"""

    model_config = ConfigDict(extra="ignore")

    transmission_chain: TransmissionChain = Field(default_factory=TransmissionChain)
    capital_acceptance: str = ""  # 资金承接评价
    historical_similar: str = ""  # 历史相似参照


class EventDossier(BaseModel):
    """事件档案（归因核心载体，字段严格照 ARCHITECTURE 第 3.1 节）。"""

    model_config = ConfigDict(extra="ignore")

    id: str  # 形如 DSS-<code>-<date>
    anomaly_event_id: str = ""  # 关联 AnomalyEvent.id
    version: int = 1  # 版本留痕，默认 1
    created_at: DateTime = Field(default_factory=datetime.now)
    cause: CauseBlock = Field(default_factory=CauseBlock)
    consequence: ConsequenceBlock = Field(default_factory=ConsequenceBlock)
    evidences: list[Evidence] = Field(default_factory=list)
    similar_cases: list[SimilarCase] = Field(default_factory=list)


def make_dossier_id(code: str, day: date | datetime | str) -> str:
    """生成事件档案 ID：``DSS-<code>-<YYYYMMDD>``。"""
    return _make_dossier_id(code, day)
