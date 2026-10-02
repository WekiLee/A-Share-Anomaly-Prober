"""回测引擎（T26）：离线逐日回放线上同一套异动规则。

核心纪律：**必须调用 ``anomaly/rules.py`` 的真实规则类**（``RULES`` 注册表），
本模块不另写近似逻辑。``RuleContext.provider`` 注入 ``HistoricalProvider``
（回放窗口内的当日 bar），``baseline_amount`` 口径与前向引擎一致
——前 lookback 个交易日的均成交额。

诚实性纪律：需要盘中 / 板块数据的规则（板块资金流翻转、板块排名跳变、
涨跌停池、集合竞价）**跳过**并写入 ``skipped_rules``，严禁用日线假造。
"""

from __future__ import annotations

from datetime import date
from statistics import median
from typing import Any, Optional

import structlog

from ashare_prober.anomaly.models import RuleContext
from ashare_prober.anomaly.rules import RULES
from ashare_prober.backtest.data import fetch_daily
from ashare_prober.backtest.models import (
    HORIZONS,
    BacktestResult,
    HorizonStats,
    SignalRecord,
)
from ashare_prober.data.models import Quote

logger = structlog.get_logger(__name__)

__all__ = ["BacktestEngine", "HistoricalProvider", "run_backtest"]

# --------------------------------------------------------------------------- #
# 规则纳入范围：仅「日线即可完整回放」的规则真实参与评估；
# 其余如实列入 skipped_rules（键为 rule_id，与 RULES 注册表一致）。
# --------------------------------------------------------------------------- #
DAILY_REPLAYABLE_RULE_IDS: frozenset[str] = frozenset({"volume_break"})

SKIPPED_RULE_REASONS: dict[str, str] = {
    "sector_flow_flip": "需要盘中个股/板块资金流数据，日线回放无法提供",
    "sector_rank_jump": "需要盘中板块排名，日线回放无法提供",
    "limit_pool_hook": "需要涨跌停池数据，日线回放无法提供",
    "auction_hook": "需要集合竞价数据，日线回放无法提供",
}


class HistoricalProvider:
    """回测用最小数据门面：向规则提供「当前回放下标」的当日行情。

    只实现规则实际会调用的接口（对照 ``rules.py``）：
    - ``get_quote(symbol)``：返回当前回放日的 bar（单标的回放，symbol 仅作校验提示）。
    - ``get_money_flow(symbol)``：日线回放无资金流数据，恒返回 None
      （``SectorFlowFlipRule`` 收到 None 会安全返回不触发）。
    """

    def __init__(self, bars: list[Quote], index: int) -> None:
        """持有全量 bars 与当前回放下标。

        Args:
            bars: 按日期升序的全量日线。
            index: 当前回放日的下标（ HistoricalProvider 只暴露该日数据）。
        """
        self._bars = list(bars)
        self._index = max(0, min(index, len(bars) - 1))

    @property
    def current_bar(self) -> Quote:
        """当前回放日的行情 bar。"""
        return self._bars[self._index]

    def get_quote(self, symbol: str) -> Quote:
        """返回当前回放日行情（单标的回放；symbol 不匹配时仅告警不阻断）。"""
        if symbol != self.current_bar.symbol:
            logger.warning(
                "HistoricalProvider 单标的回放收到其他代码请求，返回当前 bar",
                requested=symbol,
                held=self.current_bar.symbol,
            )
        return self.current_bar

    def get_money_flow(self, symbol: str) -> None:
        """日线回放无资金流数据，恒返回 None（规则侧安全降级）。"""
        del symbol
        return None


class BacktestEngine:
    """单标的离线回测引擎：逐日回放真实规则，统计前向收益 vs 基线。"""

    def __init__(self, lookback: int = 6) -> None:
        """Args:
        lookback: 计算 ``baseline_amount`` 的历史窗口（前 lookback 日均成交额），
        与线上放量判定口径保持一致。
        """
        if lookback < 0:
            raise ValueError(f"lookback 须 >= 0，收到 {lookback}")
        self.lookback = lookback

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    def run(
        self,
        symbol: str,
        bars: Optional[list[Quote]] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> BacktestResult:
        """执行回测。

        Args:
            symbol: 标的代码。
            bars: 已就绪的日线（升序给定则**纯离线**）；None 时经
                ``fetch_daily`` 联网拉取（此时 start/end 必填）。
            start / end: 回放窗口（含端点）。给定 bars 时窗口之前的 bar
                仍作为 lookback 基线历史保留。
        Raises:
            ValueError: ``bars=None`` 且 start/end 缺失时。
        """
        if bars is None:
            if start is None or end is None:
                raise ValueError("bars=None 时必须提供 start 与 end（联网拉取区间）")
            bars = fetch_daily(symbol, start, end)
        return self._run_offline(symbol, list(bars), start, end)

    # ------------------------------------------------------------------ #
    # 离线回放核心
    # ------------------------------------------------------------------ #
    def _run_offline(
        self,
        symbol: str,
        bars: list[Quote],
        start: Optional[date],
        end: Optional[date],
    ) -> BacktestResult:
        result = BacktestResult(symbol=symbol)
        result.skipped_rules = self._skipped_rules()

        # 按 (date, 原顺序) 稳定升序；无日期的行不可回放，如实记 gap 后剔除。
        dated: list[Quote] = []
        undated = 0
        for bar in bars:
            if bar.date is None:
                undated += 1
                continue
            dated.append(bar)
        dated.sort(key=lambda q: q.date or date.min)
        if undated:
            result.gaps.append(f"{undated} 根行情行缺少日期，已剔除，不参与回放")
        bars = dated

        min_needed = self.lookback + 2  # lookback 基线 + 触发日 + 至少 1 个前向日
        if len(bars) < min_needed:
            result.gaps.append(
                f"样本不足：仅 {len(bars)} 根日线，回测至少需要 "
                f"lookback({self.lookback})+2={min_needed} 根；已跳过回测，不硬凑结论"
            )
            return result

        # 真实参与回放的规则（日线可完整触发的子集）。
        active_rules = [r for r in RULES if r.rule_id in DAILY_REPLAYABLE_RULE_IDS]

        replayed_days = 0
        first_day: Optional[date] = None
        last_day: Optional[date] = None
        base_total = 0
        base_up = 0
        n = len(bars)

        for t in range(self.lookback, n):
            bar = bars[t]
            day = bar.date
            if day is None:  # 防御：上方已过滤，理论不可达
                continue
            if start is not None and day < start:
                continue  # 窗口前仅作 lookback 基线历史，不回放
            if end is not None and day > end:
                break  # bars 已升序，后面全部越界

            replayed_days += 1
            if first_day is None:
                first_day = day
            last_day = day

            # 基线胜率口径：与信号组同窗口的「次日收盘 > 当日收盘」。
            if t + 1 < n:
                base_total += 1
                if bars[t + 1].close > bar.close:
                    base_up += 1

            # baseline_amount：前 lookback 日均成交额（与线上放量判定同口径）。
            window = bars[max(0, t - self.lookback) : t]
            baseline_amount = (
                sum(q.amount for q in window) / len(window) if window else None
            )

            provider = HistoricalProvider(bars, t)
            context = RuleContext(
                provider=provider,
                symbol=symbol,
                day=day,
                quote=bar,
                scope="stock",
                ref_name=bar.name or symbol,
                baseline_amount=baseline_amount,
            )

            for rule in active_rules:
                try:
                    rule_result = rule.evaluate(context)
                except Exception as exc:  # noqa: BLE001 - 单规则异常隔离
                    logger.warning(
                        "回测规则执行异常，已隔离",
                        rule_id=rule.rule_id,
                        symbol=symbol,
                        day=str(day),
                        error=str(exc),
                    )
                    continue
                if rule_result is None or rule_result.event is None:
                    continue

                direction = "up" if bar.close > bar.pre_close else "down"
                forward, censored = self._forward_returns(bars, t, bar.close)
                result.signals.append(
                    SignalRecord(
                        symbol=symbol,
                        day=day,
                        rule=rule_result.event.type,
                        direction=direction,
                        close=bar.close,
                        baseline_amount=float(baseline_amount or 0.0),
                        forward=forward,
                        censored=censored,
                    )
                )

        # ---- 汇总 ---- #
        result.n_days = replayed_days
        result.start = first_day
        result.end = last_day
        result.n_signals = len(result.signals)
        result.n_censored = sum(1 for s in result.signals if s.censored)
        result.baseline_hit_rate = (base_up / base_total) if base_total else 0.0
        result.per_horizon = self._per_horizon(result.signals)
        result.per_rule = self._per_rule(result.signals)

        if replayed_days == 0:
            result.gaps.append("回放窗口与数据无交集：未回放任何交易日")
        return result

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _forward_returns(
        bars: list[Quote], t: int, close: float
    ) -> tuple[dict[str, float], bool]:
        """计算 T+1/T+3/T+5/T+10 前向收益（小数）。

        尾部数据不足的 horizon **不写入** forward（不冒充），并返回
        ``censored=True``。
        """
        forward: dict[str, float] = {}
        censored = False
        if close <= 0:  # 收盘价异常，无法计算收益
            return forward, True
        n = len(bars)
        for name, k in HORIZONS.items():
            if t + k < n and bars[t + k].close > 0:
                forward[name] = bars[t + k].close / close - 1.0
            else:
                censored = True
        return forward, censored

    @staticmethod
    def _per_horizon(signals: list[SignalRecord]) -> dict[str, HorizonStats]:
        """按 horizon 汇总收益统计（仅统计可计算样本，截尾不冒充）。"""
        stats: dict[str, HorizonStats] = {}
        for name in HORIZONS:
            values = [s.forward[name] for s in signals if name in s.forward]
            if not values:
                stats[name] = HorizonStats(n=0)
                continue
            hits = sum(1 for v in values if v > 0)
            stats[name] = HorizonStats(
                n=len(values),
                hit_rate=hits / len(values),
                avg=sum(values) / len(values),
                median=float(median(values)),
                best=max(values),
                worst=min(values),
            )
        return stats

    @staticmethod
    def _per_rule(signals: list[SignalRecord]) -> dict[str, int]:
        """各信号类型触发次数（键为 AnomalyEvent.type）。"""
        counter: dict[str, int] = {}
        for s in signals:
            counter[s.rule] = counter.get(s.rule, 0) + 1
        return counter

    @staticmethod
    def _skipped_rules() -> list[str]:
        """按 RULES 注册表顺序列出未纳入回放的规则及原因（诚实性披露）。"""
        return [
            f"{rule.rule_id}：{SKIPPED_RULE_REASONS.get(rule.rule_id, '日线回放无法提供所需数据')}"
            for rule in RULES
            if rule.rule_id not in DAILY_REPLAYABLE_RULE_IDS
        ]


def run_backtest(
    symbol: str,
    start: Optional[date] = None,
    end: Optional[date] = None,
    bars: Optional[list[Quote]] = None,
) -> BacktestResult:
    """模块级便捷入口：单标的回测（默认 lookback=6）。

    给定 ``bars`` 则纯离线；否则按 ``[start, end]`` 联网拉取日线（两者必填）。
    """
    return BacktestEngine().run(symbol, bars=bars, start=start, end=end)
