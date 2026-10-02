"""异动规则单测（T09）。

用假 Quote / 假 Provider 验证：
- VolumeBreakRule：放量破位 / 放量上破 / 无量不触发 / 小幅不触发。
- SectorFlowFlipRule：资金流翻转产出、未翻转或数据不足返回 None。
- 注册表 RULES 可插拔（含空壳钩子）。
"""

from __future__ import annotations

from datetime import date

from ashare_prober.anomaly.models import RuleContext, Scope
from ashare_prober.anomaly.rules import (
    RULES,
    SectorFlowFlipRule,
    SectorRankJumpRule,
    VolumeBreakRule,
)
from ashare_prober.data.models import MoneyFlow, Quote


class FakeProvider:
    """仅实现规则所需接口的假数据源。"""

    def __init__(self, quote: Quote | None = None, flow: MoneyFlow | None = None) -> None:
        self._quote = quote
        self._flow = flow

    def get_quote(self, symbol: str) -> Quote | None:
        return self._quote

    def get_money_flow(self, symbol: str) -> MoneyFlow | None:
        return self._flow


def _ctx(quote: Quote, **kw) -> RuleContext:
    return RuleContext(
        provider=FakeProvider(quote=quote),
        symbol="600600",
        day=date(2026, 10, 9),
        quote=quote,
        **kw,
    )


# --------------------------------------------------------------------------- #
# VolumeBreakRule
# --------------------------------------------------------------------------- #
def test_volume_break_down() -> None:
    q = Quote(symbol="600600", name="测试", pre_close=100.0, close=90.0, amount=2e9)
    rule = VolumeBreakRule()
    res = rule.evaluate(_ctx(q, baseline_amount=1e9))
    assert res is not None and res.event is not None
    assert res.event.type == "volume_break_down"
    assert res.event.level == "alert"  # 破位偏空


def test_volume_break_up() -> None:
    q = Quote(symbol="600600", name="测试", pre_close=100.0, close=110.0, amount=2e9)
    rule = VolumeBreakRule()
    res = rule.evaluate(_ctx(q, baseline_amount=1e9))
    assert res is not None and res.event is not None
    assert res.event.type == "volume_break_up"
    assert res.event.level == "watch"  # 上破偏多


def test_volume_break_no_volume() -> None:
    # 移动明显，但成交额未达放量阈值 -> 不触发
    q = Quote(symbol="600600", name="测试", pre_close=100.0, close=90.0, amount=1.2e9)
    rule = VolumeBreakRule()
    res = rule.evaluate(_ctx(q, baseline_amount=1e9))  # 1.2/1.0=1.2 < 1.5
    assert res is None


def test_volume_break_small_move() -> None:
    # 成交额大，但涨跌幅不足 -> 不触发
    q = Quote(symbol="600600", name="测试", pre_close=100.0, close=100.5, amount=2e9)
    rule = VolumeBreakRule()
    res = rule.evaluate(_ctx(q, baseline_amount=1e9))
    assert res is None


def test_volume_break_falls_back_to_abs_threshold() -> None:
    # 无近期基线时，退回到绝对额阈值触发
    q = Quote(symbol="600600", name="测试", pre_close=100.0, close=90.0, amount=5e8)
    rule = VolumeBreakRule()
    res = rule.evaluate(_ctx(q))  # baseline=None -> MIN_AMOUNT_ABS
    assert res is not None and res.event is not None


def test_volume_break_missing_quote_falls_back_to_provider() -> None:
    # context.quote 为 None 时回退到 provider.get_quote
    q = Quote(symbol="600600", name="测试", pre_close=100.0, close=110.0, amount=2e9)
    ctx = RuleContext(
        provider=FakeProvider(quote=q),
        symbol="600600",
        day=date(2026, 10, 9),
        quote=None,
        baseline_amount=1e9,
    )
    res = VolumeBreakRule().evaluate(ctx)
    assert res is not None and res.event is not None


# --------------------------------------------------------------------------- #
# SectorFlowFlipRule
# --------------------------------------------------------------------------- #
def test_sector_flow_flip_triggers() -> None:
    q = Quote(symbol="600600", name="测试")
    flip_flow = MoneyFlow(code="600600", main_net_inflow=-5e7)
    ctx = _ctx(q, prev_main_net_inflow=1e8)  # 前期净流入为正，当日转负
    ctx.provider = FakeProvider(quote=q, flow=flip_flow)
    res = SectorFlowFlipRule().evaluate(ctx)
    assert res is not None and res.event is not None
    assert res.event.type == "sector_flow_flip"
    assert res.event.level == "alert"  # 转流出偏空


def test_sector_flow_flip_no_flip() -> None:
    q = Quote(symbol="600600", name="测试")
    flow = MoneyFlow(code="600600", main_net_inflow=8e7)  # 仍为正，未翻转
    ctx = _ctx(q, prev_main_net_inflow=1e8)
    ctx.provider = FakeProvider(quote=q, flow=flow)
    assert SectorFlowFlipRule().evaluate(ctx) is None


def test_sector_flow_flip_no_prev_data() -> None:
    q = Quote(symbol="600600", name="测试")
    ctx = _ctx(q, prev_main_net_inflow=None)
    ctx.provider = FakeProvider(quote=q, flow=MoneyFlow(code="600600"))
    assert SectorFlowFlipRule().evaluate(ctx) is None  # 数据不足，不阻断


# --------------------------------------------------------------------------- #
# SectorRankJumpRule / 注册表
# --------------------------------------------------------------------------- #
def test_sector_rank_jump_triggers() -> None:
    q = Quote(symbol="600600", name="测试")
    ctx = _ctx(q, current_rank=20, prev_rank=5)  # 跳变 15
    res = SectorRankJumpRule().evaluate(ctx)
    assert res is not None and res.event is not None
    assert res.event.type == "sector_rank_jump"


def test_sector_rank_jump_no_data() -> None:
    q = Quote(symbol="600600", name="测试")
    ctx = _ctx(q, current_rank=20, prev_rank=None)
    assert SectorRankJumpRule().evaluate(ctx) is None


def test_rules_registry_collects_rules() -> None:
    assert len(RULES) >= 3
    assert any(r.rule_id == "volume_break" for r in RULES)
    assert any(r.rule_id == "sector_flow_flip" for r in RULES)
    assert any(r.rule_id == "sector_rank_jump" for r in RULES)
