"""元规则单测（T10）：收盘裁决 + 次日确认。"""

from __future__ import annotations

from datetime import date

from ashare_prober.anomaly.meta_rules import MetaRule
from ashare_prober.anomaly.models import AnomalyEvent, MetaStatus, Status


def _event(type_: str) -> AnomalyEvent:
    return AnomalyEvent(
        id="EVT-600600-20261009-001",
        scope="stock",
        ref_code="600600",
        ref_name="测试",
        type=type_,
        rule_id="volume_break",
    )


def test_evaluate_marks_tentative_pending() -> None:
    ev = _event("volume_break_up")
    MetaRule().evaluate(ev)
    assert ev.status == Status.TENTATIVE.value
    assert ev.meta_status == MetaStatus.PENDING.value


def test_confirm_same_direction_passes() -> None:
    # 上破事件，次日继续上涨 -> 确认通过
    ev = _event("volume_break_up")
    MetaRule().evaluate(ev)
    next_q = type("Q", (), {"pre_close": 90.0, "close": 95.0})()  # +5.5%
    MetaRule().confirm(ev, next_q)
    assert ev.status == Status.CONFIRMED.value
    assert ev.meta_status == MetaStatus.PASSED.value


def test_confirm_reverse_direction_rejected() -> None:
    # 上破事件，次日下跌 -> 确认失败
    ev = _event("volume_break_up")
    MetaRule().evaluate(ev)
    next_q = type("Q", (), {"pre_close": 90.0, "close": 85.0})()  # -5.5%
    MetaRule().confirm(ev, next_q)
    assert ev.status == Status.REJECTED.value
    assert ev.meta_status == MetaStatus.FAILED.value


def test_confirm_down_event_same_direction_passes() -> None:
    # 破位（down）事件，次日继续下跌 -> 确认通过
    ev = _event("volume_break_down")
    MetaRule().evaluate(ev)
    next_q = type("Q", (), {"pre_close": 90.0, "close": 85.0})()
    MetaRule().confirm(ev, next_q)
    assert ev.status == Status.CONFIRMED.value
    assert ev.meta_status == MetaStatus.PASSED.value


def test_confirm_already_decided_unchanged() -> None:
    ev = _event("volume_break_up")
    ev.status = Status.CONFIRMED.value
    ev.meta_status = MetaStatus.PASSED.value
    next_q = type("Q", (), {"pre_close": 90.0, "close": 85.0})()
    MetaRule().confirm(ev, next_q)  # 已终裁，不应改写
    assert ev.status == Status.CONFIRMED.value


def test_confirm_unknown_direction_keeps_pending() -> None:
    # type 无 _up/_down 后缀 -> 无法判定方向，保持 pending
    ev = _event("sector_rank_jump")
    MetaRule().evaluate(ev)
    next_q = type("Q", (), {"pre_close": 90.0, "close": 95.0})()
    MetaRule().confirm(ev, next_q)
    assert ev.status == Status.TENTATIVE.value
    assert ev.meta_status == MetaStatus.PENDING.value
