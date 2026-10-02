"""回测复核数据模型（T26）。

定义：
- ``SignalRecord``：单次信号触发记录（含前向收益与截尾标记）。
- ``HorizonStats``：单个 horizon（T+1/T+3/T+5/T+10）的收益统计。
- ``BacktestResult``：单标的回测汇总结果。

命名沿用共享知识口径：收益率均为小数（0.05 表示 +5%）；
horizon 键固定为 ``"T+1" / "T+3" / "T+5" / "T+10"``。
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# 固定 horizon 集合：键 → 前向交易日偏移 k
HORIZONS: dict[str, int] = {"T+1": 1, "T+3": 3, "T+5": 5, "T+10": 10}


class SignalRecord(BaseModel):
    """单次信号触发记录。

    Attributes:
        symbol: 标的代码。
        day: 触发交易日。
        rule: 信号类型（取自 ``AnomalyEvent.type``，如 ``volume_break_up``）。
        direction: 方向，``up`` / ``down``。
        close: 触发日收盘价。
        baseline_amount: 参与放量判定的近期均成交额（回放口径）。
        forward: 前向收益（小数），键为 ``T+1``/``T+3``/``T+5``/``T+10``；
            数据不足的 horizon 不写入该键（不冒充）。
        censored: 尾部数据不足、无法计算全部 horizon 时为 True。
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str
    day: date
    rule: str
    direction: str  # "up" / "down"
    close: float
    baseline_amount: float = 0.0
    forward: dict[str, float] = Field(default_factory=dict)
    censored: bool = False


class HorizonStats(BaseModel):
    """单个 horizon 的前向收益统计。

    ``hit_rate`` 为收益 > 0 的样本占比（小数）；``n`` 为该 horizon 可计算的
    信号数（尾部截尾信号不计入）。
    """

    model_config = ConfigDict(extra="ignore")

    n: int = 0
    hit_rate: float = 0.0
    avg: float = 0.0
    median: float = 0.0
    best: float = 0.0
    worst: float = 0.0


class BacktestResult(BaseModel):
    """单标的回测汇总结果。

    Attributes:
        symbol: 标的代码。
        start / end: 实际回放窗口（首/末回放日）；无回放日时为 None。
        n_days: 回放交易日数。
        n_signals: 信号总数。
        n_censored: 其中尾部截尾（无法计算全部 horizon）的信号数。
        baseline_hit_rate: 基线胜率——全部回放日中「次日收盘 > 当日收盘」占比
            （与信号组同口径的任意日对照）。
        per_horizon: 各 horizon 统计。
        per_rule: 各信号类型触发次数（键为 ``AnomalyEvent.type``）。
        skipped_rules: 未纳入回测的规则及原因（诚实性披露）。
        signals: 信号明细（按触发日升序）。
        gaps: 数据缺口说明。
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str
    start: Optional[date] = None
    end: Optional[date] = None
    n_days: int = 0
    n_signals: int = 0
    n_censored: int = 0
    baseline_hit_rate: float = 0.0
    per_horizon: dict[str, HorizonStats] = Field(default_factory=dict)
    per_rule: dict[str, int] = Field(default_factory=dict)
    skipped_rules: list[str] = Field(default_factory=list)
    signals: list[SignalRecord] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
