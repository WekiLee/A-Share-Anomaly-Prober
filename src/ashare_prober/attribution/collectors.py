"""证据归集器（T14）。

以「可插拔注册表」组织若干归集器，每条归集器统一实现
``collect(provider, event, **ctx) -> dict``：
- 返回 dict 中的 **CauseBlock 字段名**（news / announcements / dragon_tiger /
  unlock_schedule / sector_linkage / external_same_time）用于填充前因块；
- 可选保留键 ``"evidence"``（``Evidence`` 或 ``None``）用于同步产出一条证据。

功能归集器：新闻 / 公告 / 龙虎榜（MVP 必需）。
尽力归集器：解禁 / 同板块联动 / 外围同期（数据源不可得时返回空/占位，不阻断）。

每条归集结果同步产出一个 ``Evidence``（supports 初判由简单启发式：公告/新闻标题含
「中标/订单/增持」→ 支持；含「减持/解禁/预亏」→ 反证；无法判定 → None 不计入）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Any, Optional

from ashare_prober.attribution.models import Evidence
from ashare_prober.data.models import Announcement, DragonTiger, NewsItem


# --------------------------------------------------------------------------- #
# 启发式与默认值
# --------------------------------------------------------------------------- #
NEWS_MAX = 10  # 单标的归集新闻条数上限
DEFAULT_EVIDENCE_WEIGHT = 0.6

_POSITIVE_KEYWORDS = (
    "中标", "订单", "增持", "回购", "签约", "重大合同",
    "中标通知书", "扭亏", "利好", "项目中标",
)
_NEGATIVE_KEYWORDS = (
    "减持", "解禁", "预亏", "亏损", "处罚", "立案",
    "退市", "商誉减值", "业绩下滑", "问询", "警告",
)


def _event_day(event: Any) -> date:
    """从异动事件提取交易日（datetime → date）。"""
    da = getattr(event, "detected_at", None)
    if isinstance(da, datetime):
        return da.date()
    if isinstance(da, date):
        return da
    return date.today()


def _heuristic_supports(text: str) -> Optional[bool]:
    """基于关键词的简单初判：明显利好→True，明显利空→False，无法判定→None。"""
    if not text:
        return None
    has_pos = any(k in text for k in _POSITIVE_KEYWORDS)
    has_neg = any(k in text for k in _NEGATIVE_KEYWORDS)
    if has_pos and not has_neg:
        return True
    if has_neg and not has_pos:
        return False
    return None


def _build_evidence(
    category: str,
    content: str,
    supports: Optional[bool],
    source: str,
    weight: float = DEFAULT_EVIDENCE_WEIGHT,
) -> Optional[Evidence]:
    """构造证据；supports 为 None 时不计入（返回 None）。"""
    if supports is None:
        return None
    return Evidence(
        id="",
        category=category,
        content=content,
        supports=bool(supports),
        source=source,
        weight=float(weight),
    )


# --------------------------------------------------------------------------- #
# 归集器基类
# --------------------------------------------------------------------------- #
class Collector(ABC):
    """归集器基类：统一接口 + 元信息。"""

    collector_id: str = ""
    name: str = ""

    @abstractmethod
    def collect(self, provider: Any, event: Any, **ctx: Any) -> dict:
        """归集并返回 ``{cause_field: value, "evidence": Evidence|None}``。"""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 功能归集器（MVP 必需）
# --------------------------------------------------------------------------- #
class NewsCollector(Collector):
    """新闻归集：``provider.get_news(code)``，最多取近 NEWS_MAX 条。"""

    collector_id = "news"
    name = "新闻归集"

    def collect(self, provider: Any, event: Any, **ctx: Any) -> dict:
        items: list[NewsItem] = []
        try:
            items = list(provider.get_news(event.ref_code) or [])
        except Exception:
            items = []
        items = items[:NEWS_MAX]
        evidence: Optional[Evidence] = None
        if items:
            titles = "；".join(it.title for it in items if it.title)
            supports = _heuristic_supports(titles)
            source = next((it.source for it in items if it.source), "新闻")
            evidence = _build_evidence(
                "新闻",
                f"相关新闻 {len(items)} 条：{titles[:200]}",
                supports,
                source=source or "新闻",
            )
        return {"news": items, "evidence": evidence}


class AnnouncementCollector(Collector):
    """公告归集：``provider.get_announcements(code, day)``。"""

    collector_id = "announcement"
    name = "公告归集"

    def collect(self, provider: Any, event: Any, **ctx: Any) -> dict:
        items: list[Announcement] = []
        day = _event_day(event)
        try:
            items = list(provider.get_announcements(event.ref_code, day) or [])
        except Exception:
            items = []
        evidence: Optional[Evidence] = None
        if items:
            titles = "；".join(it.title for it in items if it.title)
            supports = _heuristic_supports(titles)
            evidence = _build_evidence(
                "公告",
                f"相关公告 {len(items)} 条：{titles[:200]}",
                supports,
                source="公告",
            )
        return {"announcements": items, "evidence": evidence}


class DragonTigerCollector(Collector):
    """龙虎榜归集：``provider.get_dragon_tiger(code, start, end)``，取最近一条。"""

    collector_id = "dragon_tiger"
    name = "龙虎榜归集"

    def collect(self, provider: Any, event: Any, **ctx: Any) -> dict:
        day = _event_day(event)
        start = day - timedelta(days=7)
        end = day
        items: list[DragonTiger] = []
        try:
            items = list(provider.get_dragon_tiger(event.ref_code, start, end) or [])
        except Exception:
            items = []
        dt: Optional[DragonTiger] = items[0] if items else None
        evidence: Optional[Evidence] = None
        if dt is not None:
            # 龙虎榜净买入为正 → 支持（资金主动买入），否则反证。
            supports = dt.net_buy > 0
            evidence = _build_evidence(
                "龙虎榜",
                f"上榜原因：{dt.reason or '—'}；净买入 {dt.net_buy:.0f}",
                supports,
                source="龙虎榜",
            )
        return {"dragon_tiger": dt, "evidence": evidence}


# --------------------------------------------------------------------------- #
# 尽力归集器（数据源不可得时返回空/占位，不阻断）
# --------------------------------------------------------------------------- #
class UnlockCollector(Collector):
    """解禁日历归集（尽力）：若 provider 提供 ``get_unlock`` 则调用，否则返回空。"""

    collector_id = "unlock"
    name = "解禁日历归集"

    def collect(self, provider: Any, event: Any, **ctx: Any) -> dict:
        get_unlock = getattr(provider, "get_unlock", None)
        schedule: list[dict] = []
        if callable(get_unlock):
            try:
                schedule = list(get_unlock(event.ref_code, _event_day(event)) or [])
            except Exception:
                schedule = []
        evidence: Optional[Evidence] = None
        if schedule:
            nxt = schedule[0]
            evidence = _build_evidence(
                "解禁",
                f"近期解禁：{nxt}",
                supports=False,  # 解禁通常构成供给压力
                source="解禁日历",
            )
        return {"unlock_schedule": schedule, "evidence": evidence}


class SectorLinkageCollector(Collector):
    """同板块联动归集（尽力）：若 provider 提供 ``get_sector_linkage`` 则调用。"""

    collector_id = "sector_linkage"
    name = "同板块联动归集"

    def collect(self, provider: Any, event: Any, **ctx: Any) -> dict:
        get_link = getattr(provider, "get_sector_linkage", None)
        linkage: list[str] = []
        if callable(get_link):
            try:
                linkage = list(get_link(event.ref_code, _event_day(event)) or [])
            except Exception:
                linkage = []
        # 联动本身中性，不产证据（supports 无法判定）。
        return {"sector_linkage": linkage}


class ExternalCollector(Collector):
    """外围同期归集：``provider.get_external(category)``（港股/美股指数等）。"""

    collector_id = "external"
    name = "外围同期归集"

    def collect(self, provider: Any, event: Any, **ctx: Any) -> dict:
        ext: list = []
        for category in ("港股指数", "美股指数"):
            try:
                ext.extend(list(provider.get_external(category) or []))
            except Exception:
                continue
        # 外围本身中性，不产证据。
        return {"external_same_time": ext}


# --------------------------------------------------------------------------- #
# 可插拔注册表：引擎默认加载全部归集器；新增只需在此追加。
# --------------------------------------------------------------------------- #
COLLECTORS: list[Collector] = [
    NewsCollector(),
    AnnouncementCollector(),
    DragonTigerCollector(),
    UnlockCollector(),
    SectorLinkageCollector(),
    ExternalCollector(),
]
