"""数据门面（T08）。

对外暴露统一接口，屏蔽主/备源差异，并实现「主源优先 → 备源兜底 → 写缓存/落库」：
- 先查 TTL 缓存；命中且新鲜直接返回。
- 未命中/过期：优先调主源（akshare），失败则回退备源（直连）。
- 任一源成功即落缓存 + 写本地库（行情/新闻），以「可用数据」继续，单源失败不阻断。

调用方（异动引擎 / 归因归集器）只依赖本门面，不直接接触 akshare 或具体备源。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

from ashare_prober.config import TTL
from ashare_prober.data.base import DataSource, SourceError
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
    SectorFlow,
)
from ashare_prober.data.store import Store

logger = logging.getLogger(__name__)


class Provider:
    """数据门面：源选择 + 缓存 + 落库 + 归一。"""

    def __init__(
        self,
        primary: DataSource,
        backup: DataSource,
        cache: Cache,
        store: Store,
    ) -> None:
        self.primary = primary
        self.backup = backup
        self.cache = cache
        self.store = store

    # ------------------------------------------------------------------ #
    # 通用：主源优先 / 备源兜底
    # ------------------------------------------------------------------ #
    def _fetch_with_fallback(
        self, call: Callable[[DataSource], Any], *, list_on_fail: bool = False
    ) -> Any:
        """先用主源，失败回退备源；双源皆败则返回空列表或抛 SourceError。

        Args:
            call: 接收数据源并返回数据的函数（如 ``lambda s: s.fetch_quote(sym)``）。
            list_on_fail: 双源皆败时是否返回 ``[]``（用于列表型接口）。
        """
        primary_exc: Exception | None = None
        try:
            return call(self.primary)
        except Exception as exc:  # noqa: BLE001 - 统一容错
            primary_exc = exc
            logger.warning(
                "主源(%s)失败，回退备源：%s",
                getattr(self.primary, "name", "?"),
                exc,
            )
        try:
            return call(self.backup)
        except Exception as backup_exc:  # noqa: BLE001
            if list_on_fail:
                return []
            raise SourceError(
                f"主源与备源均失败：primary={primary_exc} | backup={backup_exc}",
                source="provider",
                cause=backup_exc,
            ) from backup_exc

    # ------------------------------------------------------------------ #
    # 行情
    # ------------------------------------------------------------------ #
    def get_quote(self, symbol: str) -> Quote:
        key = f"quote:{symbol}"
        try:
            return Quote.model_validate(self.cache.get(key, TTL.QUOTE))
        except Exception:  # noqa: BLE001 - 缓存未命中/解析失败均视为未命中
            pass
        quote = self._fetch_with_fallback(lambda s: s.fetch_quote(symbol))
        self.cache.set(key, quote.model_dump(mode="json"), TTL.QUOTE)
        self.store.save_quote(quote)
        return quote

    # ------------------------------------------------------------------ #
    # 新闻
    # ------------------------------------------------------------------ #
    def get_news(self, symbol: str) -> list[NewsItem]:
        key = f"news:{symbol}"
        try:
            return [
                NewsItem.model_validate(d) for d in self.cache.get(key, TTL.NEWS)
            ]
        except Exception:  # noqa: BLE001
            pass
        items = self._fetch_with_fallback(
            lambda s: s.fetch_news(symbol), list_on_fail=True
        )
        self.cache.set(key, [it.model_dump(mode="json") for it in items], TTL.NEWS)
        self.store.save_news(items)
        return items

    # ------------------------------------------------------------------ #
    # 公告
    # ------------------------------------------------------------------ #
    def get_announcements(self, symbol: str, day: date) -> list[Announcement]:
        key = f"ann:{symbol}:{day.isoformat()}"
        try:
            return [
                Announcement.model_validate(d)
                for d in self.cache.get(key, TTL.FINANCE)
            ]
        except Exception:  # noqa: BLE001
            pass
        items = self._fetch_with_fallback(
            lambda s: s.fetch_announcements(symbol, day), list_on_fail=True
        )
        self.cache.set(key, [it.model_dump(mode="json") for it in items], TTL.FINANCE)
        return items

    # ------------------------------------------------------------------ #
    # 龙虎榜
    # ------------------------------------------------------------------ #
    def get_dragon_tiger(
        self, symbol: str, start: date, end: date
    ) -> list[DragonTiger]:
        key = f"dt:{symbol}:{start.isoformat()}:{end.isoformat()}"
        try:
            return [
                DragonTiger.model_validate(d)
                for d in self.cache.get(key, TTL.FINANCE)
            ]
        except Exception:  # noqa: BLE001
            pass
        items = self._fetch_with_fallback(
            lambda s: s.fetch_dragon_tiger(symbol, start, end), list_on_fail=True
        )
        self.cache.set(key, [it.model_dump(mode="json") for it in items], TTL.FINANCE)
        return items

    # ------------------------------------------------------------------ #
    # 资金流（单值；双源皆败才报错）
    # ------------------------------------------------------------------ #
    def get_money_flow(self, symbol: str) -> MoneyFlow:
        key = f"mf:{symbol}"
        try:
            return MoneyFlow.model_validate(self.cache.get(key, TTL.QUOTE))
        except Exception:  # noqa: BLE001
            pass
        flow = self._fetch_with_fallback(lambda s: s.fetch_money_flow(symbol))
        self.cache.set(key, flow.model_dump(mode="json"), TTL.QUOTE)
        return flow

    # ------------------------------------------------------------------ #
    # 外围行情
    # ------------------------------------------------------------------ #
    def get_external(self, category: str) -> list[ExternalQuote]:
        key = f"ext:{category}"
        try:
            return [
                ExternalQuote.model_validate(d)
                for d in self.cache.get(key, TTL.EXTERNAL)
            ]
        except Exception:  # noqa: BLE001
            pass
        items = self._fetch_with_fallback(
            lambda s: s.fetch_external(category), list_on_fail=True
        )
        self.cache.set(
            key, [it.model_dump(mode="json") for it in items], TTL.EXTERNAL
        )
        return items

    # ------------------------------------------------------------------ #
    # 大盘看板数据（指数 / 市场宽度 / 板块资金流 / 涨跌停池）
    # ------------------------------------------------------------------ #
    def get_index_quotes(self, codes: Sequence[str]) -> list[IndexQuote]:
        """获取指数行情列表（先缓存后回源；双源皆败返回空列表由上层记缺口）。"""
        key = "index:" + ",".join(str(c) for c in codes)
        try:
            return [
                IndexQuote.model_validate(d) for d in self.cache.get(key, TTL.QUOTE)
            ]
        except Exception:  # noqa: BLE001
            pass
        items = self._fetch_with_fallback(
            lambda s: s.fetch_index_quotes(codes), list_on_fail=True
        )
        self.cache.set(key, [it.model_dump(mode="json") for it in items], TTL.QUOTE)
        return items

    def get_market_breadth(self) -> Optional[MarketBreadth]:
        """获取市场宽度；双源皆败返回 ``None``（单值接口无法以空列表兜底）。"""
        key = "breadth:all"
        try:
            return MarketBreadth.model_validate(self.cache.get(key, TTL.QUOTE))
        except Exception:  # noqa: BLE001
            pass
        try:
            breadth = self._fetch_with_fallback(
                lambda s: s.fetch_market_breadth(), list_on_fail=False
            )
        except SourceError as exc:
            logger.warning("市场宽度获取失败（已降级为 None）：%s", exc)
            return None
        if not isinstance(breadth, MarketBreadth):
            return None
        self.cache.set(key, breadth.model_dump(mode="json"), TTL.QUOTE)
        return breadth

    def get_sector_flow(self, top_n: int = 10) -> list[SectorFlow]:
        """获取板块资金流（流入榜 + 流出榜，按 ``rank`` 正负区分）。"""
        key = f"sector_flow:{int(top_n)}"
        try:
            return [
                SectorFlow.model_validate(d) for d in self.cache.get(key, TTL.QUOTE)
            ]
        except Exception:  # noqa: BLE001
            pass
        items = self._fetch_with_fallback(
            lambda s: s.fetch_sector_flow(int(top_n)), list_on_fail=True
        )
        self.cache.set(key, [it.model_dump(mode="json") for it in items], TTL.QUOTE)
        return items

    def get_limit_pool(self, day: date, kind: str = "up") -> list[LimitPoolItem]:
        """获取涨跌停池明细（``kind`` 为 ``"up"`` / ``"down"``）。"""
        key = f"limit_pool:{kind}:{day.isoformat()}"
        try:
            return [
                LimitPoolItem.model_validate(d)
                for d in self.cache.get(key, TTL.QUOTE)
            ]
        except Exception:  # noqa: BLE001
            pass
        items = self._fetch_with_fallback(
            lambda s: s.fetch_limit_pool(day, kind), list_on_fail=True
        )
        self.cache.set(key, [it.model_dump(mode="json") for it in items], TTL.QUOTE)
        return items

    # ------------------------------------------------------------------ #
    # 归因归集输入（供 attribution 层使用）
    # ------------------------------------------------------------------ #
    def get_dossier_inputs(
        self, symbol: str, detected_at: Union[datetime, date]
    ) -> dict[str, Any]:
        """归集事件档案所需的同期数据；单源/单项失败不阻断，以 None/[] 兜底。

        Args:
            symbol: 标的代码。
            detected_at: 异动发现时间（datetime 或 date）。
        """
        day = detected_at.date() if isinstance(detected_at, datetime) else detected_at
        start = day - timedelta(days=7)
        end = day + timedelta(days=1)

        def safe(call: Callable[[], Any], default: Any) -> Any:
            try:
                return call()
            except Exception as exc:  # noqa: BLE001
                logger.warning("归集 %s 输入失败（已兜底）：%s", symbol, exc)
                return default

        external: list[ExternalQuote] = []
        external.extend(safe(lambda: self.get_external("港股指数"), []))
        external.extend(safe(lambda: self.get_external("美股指数"), []))

        return {
            "symbol": symbol,
            "detected_at": detected_at,
            "quote": safe(lambda: self.get_quote(symbol), None),
            "news": safe(lambda: self.get_news(symbol), []),
            "announcements": safe(lambda: self.get_announcements(symbol, day), []),
            "dragon_tiger": safe(
                lambda: self.get_dragon_tiger(symbol, start, end), []
            ),
            "money_flow": safe(lambda: self.get_money_flow(symbol), None),
            "external": external,
        }


# --------------------------------------------------------------------------- #
# 默认门面工厂（按需构造主/备源 + 缓存 + 落库）
# --------------------------------------------------------------------------- #
def build_default_provider(settings: Optional[Any] = None) -> Provider:
    """构造默认 Provider（主源 akshare + 备源直连 + 默认缓存/落库）。

    仅在调用时导入具体源模块，避免无 akshare 环境下模块 import 失败。
    """
    if settings is None:
        from ashare_prober.config import get_settings

        settings = get_settings()

    from ashare_prober.data.akshare_source import AkShareSource
    from ashare_prober.data.backup_source import BackupSource

    cache_path = Path(settings.db_path).with_name("prober.cache.db")
    return Provider(
        primary=AkShareSource(),
        backup=BackupSource(),
        cache=Cache(cache_path),
        store=Store(settings.db_path),
    )
