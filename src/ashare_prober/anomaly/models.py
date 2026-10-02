"""异动引擎数据模型（T09 / T11）。

定义：
- ``AnomalyEvent``：异动事件（字段严格对齐 ARCHITECTURE 第 3.1 节类图）。
- ``RuleResult``：单条规则的命中结果（事件或 None + 命中说明）。
- ``RuleContext``：传入规则的上下文（标的 / 当日 / Quote / 数据源 / 历史基线等）。
- ``Scope`` / ``Level`` / ``Status`` / ``MetaStatus``：枚举常量。
- ``make_anomaly_id``：事件 ID 生成（复用 config 的 ID 规范 ``EVT-<code>-<date>-<seq>``）。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ashare_prober.config import make_event_id  # 复用 ID 规范


class Scope(str, Enum):
    """异动作用域。"""

    INDEX = "index"
    SECTOR = "sector"
    STOCK = "stock"
    LIMIT_POOL = "limit_pool"


class Level(str, Enum):
    """告警级别。"""

    INFO = "info"
    WATCH = "watch"
    ALERT = "alert"


class Status(str, Enum):
    """事件裁定状态。"""

    TENTATIVE = "tentative"  # 盘中初判
    CONFIRMED = "confirmed"  # 次日确认通过
    REJECTED = "rejected"  # 次日确认失败


class MetaStatus(str, Enum):
    """元规则（收盘裁决 + 次日确认）状态。"""

    PENDING = "pending"  # 待次日确认
    PASSED = "passed"  # 确认通过
    FAILED = "failed"  # 确认失败


class AnomalyEvent(BaseModel):
    """异动事件（归一模型，字段严格照 ARCHITECTURE 第 3.1 节）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    scope: str = Scope.STOCK.value
    ref_code: str = ""
    ref_name: str = ""
    type: str = ""  # 如 "volume_break_up" / "sector_flow_flip"
    level: str = Level.INFO.value
    detected_at: datetime = Field(default_factory=datetime.now)
    status: str = Status.TENTATIVE.value
    rule_id: str = ""
    meta_status: str = MetaStatus.PENDING.value


class RuleResult(BaseModel):
    """规则命中结果。"""

    model_config = ConfigDict(extra="ignore")

    event: Optional[AnomalyEvent] = None
    note: str = ""  # 命中说明（仅诊断用，不写入 AnomalyEvent）


class RuleContext(BaseModel):
    """传给各规则的统一上下文。

    Attributes:
        provider: 数据门面（用于按需取数，如资金流 / 板块排名）。
        symbol: 标的代码。
        day: 判定交易日。
        quote: 已取到的当日行情（引擎预取；规则也可自行回源）。
        scope: 作用域。
        ref_name: 标的名称（通常取自 Quote.name）。
        baseline_amount: 近期均成交额，用于「放量」判定；缺失时回退绝对额阈值。
        prev_main_net_inflow: 前一交易日主力净流入，用于资金流翻转判定。
        current_rank / prev_rank: 板块排名及前一交易日排名，用于排名跳变判定。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: Any  # 数据门面（Provider 或测试替身）
    symbol: str
    day: date
    quote: Any = None  # Quote 或 None
    scope: str = Scope.STOCK.value
    ref_name: str = ""
    baseline_amount: Optional[float] = None
    prev_main_net_inflow: Optional[float] = None
    current_rank: Optional[int] = None
    prev_rank: Optional[int] = None


def make_anomaly_id(code: str, day: date | datetime | str, seq: int = 1) -> str:
    """生成异动事件 ID：``EVT-<code>-<YYYYMMDD>-<seq:03d>``。"""
    return make_event_id(code, day, seq)
