"""归因引擎主流程（T17）。

职责：
- ``build(event)``：触发 → 逐归集器 collect（try/except 隔离，单归集失败记告警、继续）
  → 汇总 evidences（确定性编号）→ 版本化（同 id 档案已存在则 +1，MVP 默认 1）
  → 持久化（store.save_dossier）。
- 异常隔离：单归集器抛错不影响整体，记 structlog 告警。
- T16 历史相似案例检索本批不实现，``similar_cases`` 保持空列表（不阻断）。
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.attribution.collectors import COLLECTORS, Collector
from ashare_prober.attribution.models import EventDossier
from ashare_prober.attribution.templates import build_blank_dossier

logger = structlog.get_logger(__name__)


class AttributionEngine:
    """归因引擎：编排证据归集与事件档案组装。"""

    def __init__(
        self,
        provider: Any,
        collectors: Optional[list[Collector]] = None,
        store: Any = None,
    ) -> None:
        self.provider = provider
        self.collectors: list[Collector] = (
            list(collectors) if collectors is not None else list(COLLECTORS)
        )
        self.store = store

    # ------------------------------------------------------------------ #
    # 构建事件档案
    # ------------------------------------------------------------------ #
    def build(self, event: AnomalyEvent) -> EventDossier:
        """对单个异动事件归集前因后果，组装并返回 ``EventDossier``。

        Args:
            event: 异动事件（已裁决/初判）。

        流程：空白模板 → 逐归集器填充 cause + 收集 evidence → 证据编号 →
        版本化 → 持久化。单归集器异常被隔离。
        """
        dossier = build_blank_dossier(event)
        evidences: list = []

        for collector in self.collectors:
            try:
                result = collector.collect(self.provider, event)
            except Exception as exc:  # noqa: BLE001 - 单归集器异常隔离
                logger.warning(
                    "归集器执行异常，已隔离",
                    collector_id=collector.collector_id,
                    event_id=event.id,
                    error=str(exc),
                )
                continue

            if not isinstance(result, dict):
                continue

            for key, value in result.items():
                if key == "evidence":
                    if value is not None:
                        evidences.append(value)
                elif value is not None:
                    # 仅将 CauseBlock 字段名映射写入前因块。
                    setattr(dossier.cause, key, value)

        # 证据确定性编号（便于落库与人审引用）。
        for idx, ev in enumerate(evidences, start=1):
            if not ev.id:
                ev.id = f"EVD-{idx:03d}"
        dossier.evidences = evidences

        # 版本化：若 store 已有同 id 档案则 +1（MVP：无则固定 1）。
        if self.store is not None:
            try:
                existing = self.store.get_dossier(dossier.id)
                if existing is not None:
                    dossier.version = existing.version + 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "查询既有档案版本失败", dossier_id=dossier.id, error=str(exc)
                )

        # 持久化。
        if self.store is not None:
            try:
                self.store.save_dossier(dossier)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "事件档案落库失败", dossier_id=dossier.id, error=str(exc)
                )

        return dossier
