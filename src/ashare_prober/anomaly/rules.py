"""异动规则（T09）。

以「可插拔注册表」组织若干规则，每条规则统一实现 ``evaluate(context) -> RuleResult | None``：
- ``VolumeBreakRule``：放量破位 / 放量上破（仅需单只 Quote 即可完整触发）。
- ``SectorFlowFlipRule``：板块资金流翻转（主力净流入由正转负 / 由负转正）。
- ``SectorRankJumpRule``：板块排名较前一日跳变超阈值（数据不足时返回 None，不阻断）。
- ``LimitPoolHook`` / ``AuctionHook``：涨跌停池 / 集合竞价信号钩子（空壳，后续接 provider）。

所有规则仅产出「初判」事件（``status=tentative`` / ``meta_status=pending``），
最终裁决与次日确认由 ``meta_rules.MetaRule`` 负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ashare_prober.anomaly.models import (
    AnomalyEvent,
    Level,
    RuleContext,
    RuleResult,
    Scope,
    make_anomaly_id,
)

# --------------------------------------------------------------------------- #
# 规则阈值（集中可调；T11 引擎接入历史数据后放量判定更稳健）
# --------------------------------------------------------------------------- #
MOVE_PCT_THRESHOLD = 3.0  # 涨跌幅绝对值达到该比例才算「有意义的移动」
VOLUME_RATIO = 1.5  # 成交额相对近期均额放大约 1.5 倍视为「放量」
MIN_AMOUNT_ABS = 100_000_000.0  # 无近期基线时的绝对额兜底阈值（1 亿）
RANK_JUMP_THRESHOLD = 5  # 板块排名跳变超过该名次触发


class BaseRule(ABC):
    """规则基类：统一接口 + 元信息。"""

    rule_id: str = ""
    name: str = ""

    @abstractmethod
    def evaluate(self, context: RuleContext) -> Optional[RuleResult]:
        """对给定上下文评估是否触发异动；不触发返回 None。"""
        raise NotImplementedError


class VolumeBreakRule(BaseRule):
    """放量破位 / 放量上破。

    基于单只 ``Quote``：比较 ``close`` 与 ``pre_close`` 判定方向，并以成交额相对
    近期均值（或绝对额兜底）放大判定「放量」。该规则**仅需单只 Quote 即可完整触发**，
    引擎通过 ``provider.get_quote(symbol)`` 取数。
    """

    rule_id = "volume_break"
    name = "放量破位/上破"

    def evaluate(self, context: RuleContext) -> Optional[RuleResult]:
        quote = context.quote
        if quote is None:
            try:
                quote = context.provider.get_quote(context.symbol)
            except Exception:
                return None
        if quote is None or quote.pre_close <= 0:
            return None

        pct = (quote.close - quote.pre_close) / quote.pre_close * 100.0
        if abs(pct) < MOVE_PCT_THRESHOLD:
            return None  # 移动幅度不足，不触发

        # 「放量」判定：有近期基线用比率，否则用绝对额兜底。
        if context.baseline_amount is not None and context.baseline_amount > 0:
            amplified = quote.amount >= VOLUME_RATIO * context.baseline_amount
        else:
            amplified = quote.amount >= MIN_AMOUNT_ABS
        if not amplified:
            return None  # 无量不触发

        up = quote.close > quote.pre_close
        event = AnomalyEvent(
            id=make_anomaly_id(context.symbol, context.day),
            scope=context.scope or Scope.STOCK.value,
            ref_code=context.symbol,
            ref_name=context.ref_name or getattr(quote, "name", "") or context.symbol,
            type="volume_break_up" if up else "volume_break_down",
            level=Level.WATCH.value if up else Level.ALERT.value,
            rule_id=self.rule_id,
        )
        note = (
            f"{'放量上破' if up else '放量破位'}：涨跌幅 {pct:.2f}%，"
            f"成交额 {quote.amount:.0f}"
        )
        return RuleResult(event=event, note=note)


class SectorFlowFlipRule(BaseRule):
    """板块资金流翻转。

    通过 ``provider.get_money_flow(code)`` 取当日主力净流入，与 ``prev_main_net_inflow``
    （前一交易日，由引擎/归集器提供）对比，正负号翻转即触发，并给出板块联动提示。
    """

    rule_id = "sector_flow_flip"
    name = "板块资金流翻转"

    def evaluate(self, context: RuleContext) -> Optional[RuleResult]:
        prev = context.prev_main_net_inflow
        if prev is None:
            return None  # 缺少前一日数据，不阻断
        try:
            flow = context.provider.get_money_flow(context.symbol)
        except Exception:
            return None
        if flow is None or not hasattr(flow, "main_net_inflow"):
            return None

        cur = flow.main_net_inflow
        if prev == 0 or cur == 0:
            return None  # 两侧需均非零才有翻转意义
        if not ((prev > 0) != (cur > 0)):
            return None  # 未翻转

        turning_negative = cur < 0
        event = AnomalyEvent(
            id=make_anomaly_id(context.symbol, context.day),
            scope=context.scope or Scope.STOCK.value,
            ref_code=context.symbol,
            ref_name=f"{context.ref_name or context.symbol}（所属板块占位）",
            type="sector_flow_flip",
            level=Level.ALERT.value if turning_negative else Level.WATCH.value,
            rule_id=self.rule_id,
        )
        note = f"主力净流入由 {prev:.0f} 转为 {cur:.0f}（{'流出' if turning_negative else '流入'}）"
        return RuleResult(event=event, note=note)


class SectorRankJumpRule(BaseRule):
    """板块排名跳变。

    板块排名较前一交易日跳变超过阈值触发；数据不足（缺当前或前一排名）时返回 None，不阻断。
    """

    rule_id = "sector_rank_jump"
    name = "板块排名跳变"

    def evaluate(self, context: RuleContext) -> Optional[RuleResult]:
        cur = context.current_rank
        prev = context.prev_rank
        if cur is None or prev is None:
            return None  # 数据不足
        jump = abs(cur - prev)
        if jump < RANK_JUMP_THRESHOLD:
            return None
        event = AnomalyEvent(
            id=make_anomaly_id(context.symbol, context.day),
            scope=context.scope or Scope.SECTOR.value,
            ref_code=context.symbol,
            ref_name=context.ref_name or context.symbol,
            type="sector_rank_jump",
            level=Level.WATCH.value,
            rule_id=self.rule_id,
        )
        note = f"板块排名 {prev} → {cur}（跳变 {jump}）"
        return RuleResult(event=event, note=note)


class LimitPoolHook(BaseRule):
    """涨跌停池信号钩子（空壳）。

    后续阶段（涨跌停池 / T16）将接入 ``provider.get_dossier_inputs`` 中的涨跌停池数据；
    当前 evaluate 返回 None，保证注册表完整且不阻断。
    """

    rule_id = "limit_pool_hook"
    name = "涨跌停池钩子"

    def evaluate(self, context: RuleContext) -> Optional[RuleResult]:
        return None


class AuctionHook(BaseRule):
    """集合竞价信号钩子（空壳）。

    后续阶段将接入集合竞价量价数据；当前返回 None，不阻断。
    """

    rule_id = "auction_hook"
    name = "集合竞价钩子"

    def evaluate(self, context: RuleContext) -> Optional[RuleResult]:
        return None


# --------------------------------------------------------------------------- #
# 可插拔注册表：引擎默认加载全部规则；新增规则只需在此追加。
# --------------------------------------------------------------------------- #
RULES: list[BaseRule] = [
    VolumeBreakRule(),
    SectorFlowFlipRule(),
    SectorRankJumpRule(),
    LimitPoolHook(),
    AuctionHook(),
]
