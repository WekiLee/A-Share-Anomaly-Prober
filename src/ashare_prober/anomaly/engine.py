"""异动引擎主循环（T11）。

职责：
- ``scan(scope, ref_code, date=None)``：取数 → 逐规则 evaluate → 初判（tentative/pending）
  → 持久化（store.save_event）。
- ``run_once(date=None)``：遍历 ``config.watchlist``（默认空 → 返回 [] 并记 info 日志），
  为后续全市场扫描留扩展点。
- 异常隔离：单规则 / 单标的抛错不影响其他，记 structlog 告警。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

import structlog

from ashare_prober.anomaly.meta_rules import MetaRule
from ashare_prober.anomaly.models import AnomalyEvent, RuleContext, Scope
from ashare_prober.anomaly.rules import RULES, BaseRule

logger = structlog.get_logger(__name__)


class AnomalyEngine:
    """异动引擎：编排规则评估与事件产出。"""

    def __init__(
        self,
        provider: Any,
        rules: Optional[list[BaseRule]] = None,
        meta: Optional[MetaRule] = None,
        store: Any = None,
        watchlist: Optional[list[str]] = None,
    ) -> None:
        self.provider = provider
        self.rules: list[BaseRule] = list(rules) if rules is not None else list(RULES)
        self.meta = meta or MetaRule()
        self.store = store
        self.watchlist = watchlist

    # ------------------------------------------------------------------ #
    # 单标的扫描
    # ------------------------------------------------------------------ #
    def scan(self, scope: str, ref_code: str, day: Optional[date] = None) -> list[AnomalyEvent]:
        """对单个标的跑全部规则，产出初判事件列表。

        Args:
            scope: 作用域（index/sector/stock/limit_pool）。
            ref_code: 标的代码。
            day: 判定交易日；缺省取今天。
        """
        day = day or date.today()
        try:
            quote = self.provider.get_quote(ref_code)
        except Exception as exc:  # noqa: BLE001 - 取数失败隔离
            logger.warning("scan 取行情失败，跳过标的", symbol=ref_code, error=str(exc))
            return []

        if quote is None:
            return []

        context = RuleContext(
            provider=self.provider,
            symbol=ref_code,
            day=day,
            quote=quote,
            scope=scope or Scope.STOCK.value,
            ref_name=getattr(quote, "name", "") or ref_code,
        )

        events: list[AnomalyEvent] = []
        for rule in self.rules:
            try:
                result = rule.evaluate(context)
            except Exception as exc:  # noqa: BLE001 - 单规则异常隔离
                logger.warning(
                    "规则执行异常，已隔离",
                    rule_id=rule.rule_id,
                    symbol=ref_code,
                    error=str(exc),
                )
                continue
            if result and result.event:
                event = result.event
                self.meta.evaluate(event)  # 收盘裁决：tentative/pending
                events.append(event)
                if self.store is not None:
                    try:
                        self.store.save_event(event)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "事件落库失败", event_id=event.id, error=str(exc)
                        )
        return events

    # ------------------------------------------------------------------ #
    # 全量扫描（MVP：仅 watchlist）
    # ------------------------------------------------------------------ #
    def run_once(
        self, day: Optional[date] = None, store: Any = None
    ) -> list[AnomalyEvent]:
        """遍历 watchlist 逐个 scan；watchlist 为空直接返回 []。

        Args:
            day: 判定交易日；缺省取今天。
            store: 本地库（可选）。非 ``None`` 时通过
                ``resolve_watchlist(store)`` 以 DB 名单为真源取标的
                （空名单降级配置兜底）；``None`` 时保持旧行为
                （``self.watchlist`` → ``settings.watchlist``）。

        扩展点：后续接入全市场标的枚举 / 板块池后，可在此扩展为全市场扫描。
        """
        if store is not None:
            from ashare_prober.data.watchlist import resolve_watchlist

            watchlist = resolve_watchlist(store)
        else:
            watchlist = self.watchlist
            if watchlist is None:
                from ashare_prober.config import get_settings

                watchlist = list(getattr(get_settings(), "watchlist", []) or [])

        if not watchlist:
            logger.info(
                "watchlist 为空，run_once 无标的可扫描，返回 []（扩展点：全市场扫描）"
            )
            return []

        all_events: list[AnomalyEvent] = []
        for code in watchlist:
            all_events.extend(self.scan("stock", code, day))
        return all_events
