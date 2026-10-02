"""元规则（T10）：收盘裁决 + 次日确认。

目标：降低盘中假信号。盘中触发先标 ``status=tentative`` / ``meta_status=pending``，
待次日收盘同向确认后才 ``confirmed`` / ``passed``，否则 ``rejected`` / ``failed``。

方向判定复用事件 ``type`` 中的 ``_up`` / ``down`` 后缀（由规则在产出事件时写入），
使 ``confirm`` 无需回看当日行情即可判断「次日是否同向」。
"""

from __future__ import annotations

from typing import Any

from ashare_prober.anomaly.models import (
    AnomalyEvent,
    MetaStatus,
    Status,
)

# 次日同向确认所需的最小涨跌幅（%），用于过滤微小噪音。
CONFIRM_MOVE_PCT = 0.5


class MetaRule:
    """元规则：收盘裁决 + 次日确认。"""

    # ------------------------------------------------------------------ #
    # 收盘裁决：盘中初判 -> tentative / pending
    # ------------------------------------------------------------------ #
    def evaluate(self, event: AnomalyEvent) -> AnomalyEvent:
        """收盘裁决：将尚未终裁的事件标为 tentative / pending（幂等）。"""
        if event.status not in (Status.CONFIRMED.value, Status.REJECTED.value):
            event.status = Status.TENTATIVE.value
            event.meta_status = MetaStatus.PENDING.value
        return event

    # ------------------------------------------------------------------ #
    # 次日确认：依据次日行情方向裁决
    # ------------------------------------------------------------------ #
    def confirm(self, event: AnomalyEvent, next_day_quote: Any) -> AnomalyEvent:
        """以次日收盘方向确认事件。

        Args:
            event: 待确认事件（通常处于 tentative）。
            next_day_quote: 次日 Quote（需含 close / pre_close）。

        Returns:
            终审后的事件：同向且达阈值 -> confirmed/passed；否则 rejected/failed。
            已终裁事件直接原样返回。
        """
        # 已终裁（如之前确认过）不再改动。
        if event.status in (Status.CONFIRMED.value, Status.REJECTED.value):
            return event

        direction = self._expected_direction(event)
        if direction == 0:
            # 无法从 type 解析方向（如后续新增未带后缀的规则），保持 pending。
            return event

        pre = getattr(next_day_quote, "pre_close", None)
        close = getattr(next_day_quote, "close", None)
        if pre is None or close is None or pre <= 0:
            return event
        move_pct = (close - pre) / pre * 100.0
        next_direction = 1 if move_pct > 0 else (-1 if move_pct < 0 else 0)

        if direction == next_direction and abs(move_pct) >= CONFIRM_MOVE_PCT:
            event.status = Status.CONFIRMED.value
            event.meta_status = MetaStatus.PASSED.value
        else:
            event.status = Status.REJECTED.value
            event.meta_status = MetaStatus.FAILED.value
        return event

    @staticmethod
    def _expected_direction(event: AnomalyEvent) -> int:
        """从事件 type 解析预期方向：``_up`` -> +1，``_down`` -> -1，未知 -> 0。"""
        t = event.type.lower()
        if "up" in t:
            return 1
        if "down" in t:
            return -1
        return 0
