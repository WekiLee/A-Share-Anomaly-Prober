"""异动引擎单测（T11）。

注入假 Provider（仅实现 get_quote），验证：
- scan 对异动标的产出事件、对正常标的返回空；
- 单规则异常被隔离，不影响其他规则/标的；
- provider.get_quote 抛错时 scan 返回空；
- run_once 遍历 watchlist / 空 watchlist 直接返回 []；
- store 持久化（scan 落库后可由 store.get_event 读回）。
"""

from __future__ import annotations

from datetime import date

from ashare_prober.anomaly.engine import AnomalyEngine
from ashare_prober.anomaly.rules import BaseRule, VolumeBreakRule
from ashare_prober.data.models import Quote
from ashare_prober.data.store import Store


class FakeProvider:
    """按代码返回预设 Quote 的假数据源。"""

    def __init__(self, boom: bool = False) -> None:
        self.boom = boom

    def get_quote(self, symbol: str) -> Quote:
        if self.boom:
            raise RuntimeError("provider get_quote failed")
        if symbol == "600600":
            # 异动样本：放量破位
            return Quote(
                symbol="600600",
                name="异动股",
                pre_close=100.0,
                close=90.0,
                amount=2e9,
                date=date(2026, 10, 9),
            )
        # 正常样本：小幅波动，不触发
        return Quote(
            symbol=symbol,
            name="正常股",
            pre_close=100.0,
            close=100.3,
            amount=2e9,
            date=date(2026, 10, 9),
        )

    def get_money_flow(self, symbol: str):
        return None


class BadRule(BaseRule):
    """故意抛错的规则，用于验证异常隔离。"""

    rule_id = "bad"

    def evaluate(self, context):  # noqa: ANN001
        raise RuntimeError("rule boom")


def test_scan_produces_event_for_anomaly_symbol() -> None:
    engine = AnomalyEngine(provider=FakeProvider(), rules=[VolumeBreakRule()])
    events = engine.scan("stock", "600600", date(2026, 10, 9))
    assert len(events) == 1
    assert events[0].type in ("volume_break_up", "volume_break_down")
    assert events[0].status == "tentative"
    assert events[0].meta_status == "pending"


def test_scan_returns_empty_for_normal_symbol() -> None:
    engine = AnomalyEngine(provider=FakeProvider(), rules=[VolumeBreakRule()])
    events = engine.scan("stock", "000001", date(2026, 10, 9))
    assert events == []


def test_rule_exception_isolated() -> None:
    # BadRule 抛错不应阻断 VolumeBreakRule
    engine = AnomalyEngine(provider=FakeProvider(), rules=[VolumeBreakRule(), BadRule()])
    events = engine.scan("stock", "600600", date(2026, 10, 9))
    assert len(events) == 1  # 仅 VolumeBreakRule 产出


def test_scan_handles_provider_failure() -> None:
    engine = AnomalyEngine(provider=FakeProvider(boom=True), rules=[VolumeBreakRule()])
    events = engine.scan("stock", "600600", date(2026, 10, 9))
    assert events == []  # 取数失败 -> 空，不抛错


def test_run_once_with_watchlist() -> None:
    engine = AnomalyEngine(
        provider=FakeProvider(), rules=[VolumeBreakRule()], watchlist=["600600"]
    )
    events = engine.run_once(date(2026, 10, 9))
    assert len(events) == 1


def test_run_once_empty_watchlist_returns_empty() -> None:
    engine = AnomalyEngine(
        provider=FakeProvider(), rules=[VolumeBreakRule()], watchlist=[]
    )
    assert engine.run_once(date(2026, 10, 9)) == []


def test_scan_persists_to_store(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "prober.db")
    engine = AnomalyEngine(
        provider=FakeProvider(), rules=[VolumeBreakRule()], store=store
    )
    events = engine.scan("stock", "600600", date(2026, 10, 9))
    assert len(events) == 1
    # 落库后可由 store 读回
    got = store.get_event(events[0].id)
    assert got is not None
    assert got.id == events[0].id
    assert got.type == events[0].type
