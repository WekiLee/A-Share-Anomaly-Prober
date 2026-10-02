"""大盘看板采集器。

``MarketDashboardBuilder`` 按「单项失败不阻断其它项」的原则归集大盘数据：
指数行情 / 市场宽度 / 板块资金流 / 涨跌停池 / 外围行情 / 近期异动 / 监控名单。

降级纪律（本项目核心）：
- 每一项采集都包裹在 ``_safe`` 中；抛出异常 → 记 warning + 写入 ``gaps`` + 该项留空。
- **绝不编造数值**：取不到就是取不到，缺口必须如实呈现在看板页首。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable, Optional

from ashare_prober.dashboard.models import (
    IndexQuote,
    MarketBreadth,
    MarketDashboard,
    SectorFlow,
)
from ashare_prober.data.models import ExternalQuote, LimitPoolItem

logger = logging.getLogger(__name__)

#: 默认指数集合（代码 -> 名称）：覆盖沪深主板 / 创业板 / 科创板 / 权重 / 北交所。
DEFAULT_INDEX_CODES: dict[str, str] = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
    "000016": "上证50",
    "899050": "北证50",
}

#: 看板「近期异动」展示条数上限。
RECENT_EVENT_LIMIT = 10

#: 外围行情类别（港股指数 / 美股指数）。
EXTERNAL_CATEGORIES: tuple[str, ...] = ("港股指数", "美股指数")


class MarketDashboardBuilder:
    """大盘看板采集器：逐项采集 + 单项降级 + 缺口记录。"""

    def __init__(
        self,
        provider: Optional[Any] = None,
        store: Optional[Any] = None,
        index_codes: Optional[dict[str, str]] = None,
        top_n: int = 10,
    ) -> None:
        """初始化采集器。

        Args:
            provider: 数据门面（``Provider`` 或任何实现同名方法的替身）；``None`` 时
                全部指标降级为空并记一条总缺口（保证无数据源环境也可构造与渲染）。
            store: 本地库（可选；具备 ``list_events`` 时读取近期异动）。
            index_codes: 指数集合，默认 ``DEFAULT_INDEX_CODES``。
            top_n: 板块资金流 / 涨跌停明细的展示条数。
        """
        self.provider = provider
        self.store = store
        self.index_codes = dict(index_codes or DEFAULT_INDEX_CODES)
        self.top_n = int(top_n)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def build(self, day: date | None = None) -> MarketDashboard:
        """采集并组装大盘看板。

        Args:
            day: 交易日；``None`` 表示取当前日期（行情源各自返回最新快照）。

        Returns:
            ``MarketDashboard``；缺失项以空值呈现并写入 ``gaps``。
        """
        gaps: list[str] = []
        data_day = day if day is not None else datetime.now().date()

        if self.provider is None:
            gaps.append("数据源未配置（provider=None），全部指标降级为空")
            # 监控名单只依赖本地库（与 provider 无关），无数据源时也照常采集。
            watchlist = self._collect_watchlist(gaps)
            return MarketDashboard(date=data_day, watchlist=watchlist, gaps=gaps)

        indices = self._collect_indices(gaps)
        breadth = self._collect_breadth(gaps)
        sectors_top, sectors_bottom = self._collect_sectors(gaps)
        limit_up = self._collect_limit_pool(gaps, day=data_day, kind="up")
        limit_down = self._collect_limit_pool(gaps, day=data_day, kind="down")
        external = self._collect_external(gaps)
        recent_events = self._collect_recent_events(gaps)
        watchlist = self._collect_watchlist(gaps)

        return MarketDashboard(
            date=data_day,
            indices=indices,
            breadth=breadth,
            sectors_top=sectors_top,
            sectors_bottom=sectors_bottom,
            limit_up=limit_up,
            limit_down=limit_down,
            external=external,
            recent_events=recent_events,
            watchlist=watchlist,
            gaps=gaps,
        )

    # ------------------------------------------------------------------ #
    # 分项采集
    # ------------------------------------------------------------------ #
    def _collect_indices(self, gaps: list[str]) -> list[IndexQuote]:
        codes = list(self.index_codes.keys())
        raw = self._safe(
            "指数行情", lambda: self.provider.get_index_quotes(codes), [], gaps
        )
        items = [it for it in raw if isinstance(it, IndexQuote)]
        # 部分缺失（如源只返回 4 个指数）如实记录；全空时 _safe 已记过一次，不重复。
        if codes and 0 < len(items) < len(codes):
            got = {it.code for it in items}
            missing = [c for c in codes if c not in got]
            gaps.append(
                "指数行情未取全：缺失 "
                + "、".join(f"{c}({self.index_codes.get(c, '')})" for c in missing)
            )
        # 名称兜底：源未返回名称时用内置映射补全（仅补名称，不补数值）。
        for item in items:
            if not item.name and item.code in self.index_codes:
                item.name = self.index_codes[item.code]
        return items

    def _collect_breadth(self, gaps: list[str]) -> Optional[MarketBreadth]:
        value = self._safe(
            "市场宽度（涨跌家数）",
            lambda: self.provider.get_market_breadth(),
            None,
            gaps,
        )
        return value if isinstance(value, MarketBreadth) else None

    def _collect_sectors(
        self, gaps: list[str]
    ) -> tuple[list[SectorFlow], list[SectorFlow]]:
        raw = self._safe(
            "板块资金流",
            lambda: self.provider.get_sector_flow(self.top_n),
            [],
            gaps,
        )
        flows = [it for it in raw if isinstance(it, SectorFlow)]
        # 口径：净流入（>=0）归 TOP 榜，净流出（<0）归 BOTTOM 榜；
        # 若源已用 rank 正负标记，则以 rank 为准（正=流入榜，负=流出榜）。
        top: list[SectorFlow] = []
        bottom: list[SectorFlow] = []
        for item in flows:
            if item.rank < 0 or (item.rank == 0 and item.main_net_inflow < 0):
                bottom.append(item)
            else:
                top.append(item)
        top.sort(key=lambda s: s.main_net_inflow, reverse=True)
        bottom.sort(key=lambda s: s.main_net_inflow)
        return top[: self.top_n], bottom[: self.top_n]

    def _collect_limit_pool(
        self, gaps: list[str], day: date, kind: str
    ) -> list[LimitPoolItem]:
        label = "涨停池" if kind == "up" else "跌停池"
        raw = self._safe(
            label, lambda: self.provider.get_limit_pool(day, kind), [], gaps
        )
        items = [it for it in raw if isinstance(it, LimitPoolItem)]
        return items[: self.top_n]

    def _collect_external(self, gaps: list[str]) -> list[ExternalQuote]:
        result: list[ExternalQuote] = []
        for category in EXTERNAL_CATEGORIES:
            items = self._safe(
                f"外围行情（{category}）",
                lambda c=category: self.provider.get_external(c),
                [],
                gaps,
            )
            result.extend(it for it in items if isinstance(it, ExternalQuote))
        return result

    def _collect_recent_events(self, gaps: list[str]) -> list[dict]:
        """读取近期异动；本地库未提供 ``list_events`` 时留空并记缺口。"""
        if self.store is None:
            gaps.append("近期异动未获取：未配置本地库（store=None）")
            return []
        list_events = getattr(self.store, "list_events", None)
        if not callable(list_events):
            gaps.append("近期异动未获取：本地库未提供 list_events 接口")
            return []
        # 空列表是「今日确实无异动」的正常结果，不算数据缺口。
        raw = self._safe(
            "近期异动",
            lambda: list_events(limit=RECENT_EVENT_LIMIT),
            [],
            gaps,
            empty_is_gap=False,
        )
        # 成功返回空列表属于「今日确实无异动」，不是数据缺口，不写 gaps。
        return [_event_to_dict(item) for item in (raw or [])]

    def _collect_watchlist(self, gaps: list[str]) -> list[dict]:
        """采集监控名单（仅依赖本地库，与 provider 无关）。

        纪律：
        - ``store`` 为 None → 空名单 + 缺口「未连接本地库，监控列表不可用」。
        - ``store.list_watchlist()`` 抛异常 → 空名单 + 缺口说明（不阻断其它项）。
        - **业务性空名单**（用户还没加票）是正常态，**不计缺口**，页面显示引导文案。
        """
        if self.store is None:
            gaps.append("未连接本地库，监控列表不可用")
            return []
        list_watchlist = getattr(self.store, "list_watchlist", None)
        if not callable(list_watchlist):
            # 旧版本地库未实现 watchlist 契约：属正常能力边界（非数据缺口），
            # 静默降级为空名单，保证既有 store 实现的看板不误报缺口。
            logger.debug("本地库未提供 list_watchlist 接口，监控名单降级为空")
            return []
        # empty_is_gap=False：空名单是业务正常态，不算数据缺口。
        raw = self._safe(
            "监控列表", lambda: list_watchlist(), [], gaps, empty_is_gap=False
        )
        items: list[dict] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            items.append(
                {
                    "code": str(row.get("code", "")),
                    "name": str(row.get("name", "")),
                    "active": bool(row.get("active", True)),
                }
            )
        return items

    # ------------------------------------------------------------------ #
    # 安全包裹
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe(
        label: str,
        call: Callable[[], Any],
        default: Any,
        gaps: list[str],
        empty_is_gap: bool = True,
    ) -> Any:
        """执行采集；异常与空结果均降级为 ``default``，并写入缺口说明。

        Args:
            label: 数据项名称（用于日志与缺口文案）。
            call: 采集函数。
            default: 降级返回值。
            gaps: 缺口清单（原地追加）。
            empty_is_gap: 空结果是否视为数据缺口（「今日无异动」这类
                业务性空集应传 ``False``，避免把正常空态误报成缺口）。
        """
        try:
            value = call()
        except Exception as exc:  # noqa: BLE001 - 单源失败不阻断其它项
            logger.warning("大盘看板采集 %s 失败（已降级）：%s", label, exc)
            gaps.append(f"{label}获取失败：{exc}")
            return default
        if value is None:
            gaps.append(f"{label}未返回数据")
            return default
        if empty_is_gap and isinstance(value, (list, tuple)) and not value:
            gaps.append(f"{label}未返回数据")
            return default
        return value


def build_dashboard(
    day: date | None = None,
    provider: Optional[Any] = None,
    store: Optional[Any] = None,
) -> MarketDashboard:
    """便捷函数：一行构造看板。

    Args:
        day: 交易日；``None`` 取当前日期。
        provider: 数据门面；``None`` 时惰性构造默认门面（**不在 import 期构造**，
            避免无 akshare 环境下模块导入即崩溃）。
        store: 本地库（可选）。

    Returns:
        ``MarketDashboard``。
    """
    if provider is None:
        from ashare_prober.data.provider import build_default_provider

        provider = build_default_provider()
    return MarketDashboardBuilder(provider=provider, store=store).build(day=day)


def _event_to_dict(event: Any) -> dict:
    """将异动事件（模型或 dict）归一为看板用的轻量 dict。"""
    if isinstance(event, dict):
        return {
            "id": str(event.get("id", "")),
            "code": str(event.get("code") or event.get("ref_code") or ""),
            "name": str(event.get("name") or event.get("ref_name") or ""),
            "type": str(event.get("type", "")),
            "level": str(event.get("level", "")),
            "detected_at": event.get("detected_at"),
        }
    return {
        "id": str(getattr(event, "id", "") or ""),
        "code": str(getattr(event, "ref_code", "") or ""),
        "name": str(getattr(event, "ref_name", "") or ""),
        "type": str(getattr(event, "type", "") or ""),
        "level": str(getattr(event, "level", "") or ""),
        "detected_at": getattr(event, "detected_at", None),
    }
