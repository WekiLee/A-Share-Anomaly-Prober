"""数据源抽象（T04）。

定义：
- ``SourceError``：数据源获取失败的统一异常（网络/接口/解析异常均归一为此）。
- ``CacheMiss``：缓存未命中或已过期时由 ``Cache.get`` 抛出。
- ``DataSource``：主/备源共同遵循的 Protocol（结构化接口契约）。

调用方（Provider）仅依赖此 Protocol，不直接耦合 akshare 函数名或具体备源实现。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ashare_prober.data.models import (
    Announcement,
    DragonTiger,
    ExternalQuote,
    MoneyFlow,
    NewsItem,
    Quote,
)


class SourceError(Exception):
    """数据源获取/解析失败的统一异常。

    Attributes:
        source: 出错的数据源标识（如 "akshare" / "backup"）。
        cause: 底层原始异常（若有）。
    """

    def __init__(
        self, message: str, *, source: str = "", cause: Exception | None = None
    ) -> None:
        super().__init__(message)
        self.source = source
        self.cause = cause

    def __str__(self) -> str:  # pragma: no cover - 便于日志可读
        if self.source:
            return f"[{self.source}] {super().__str__()}"
        return super().__str__()


class CacheMiss(Exception):
    """缓存未命中或已过期。

    由 ``Cache.get`` 在 key 不存在或已超过 TTL 时抛出，供 Provider 决定回源。
    """

    def __init__(self, key: str = "") -> None:
        super().__init__(f"cache miss: {key}" if key else "cache miss")
        self.key = key


@runtime_checkable
class DataSource(Protocol):
    """数据源接口契约（主源 akshare / 备源直连均遵循）。

    所有实现须保证：获取失败时抛 ``SourceError``，成功时返回归一后的 Pydantic 模型。
    """

    #: 数据源名称，用于日志与异常溯源。
    name: str

    def fetch_quote(self, symbol: str) -> Quote:
        """获取个股行情快照。"""
        ...

    def fetch_news(self, symbol: str) -> list[NewsItem]:
        """获取个股相关新闻列表。"""
        ...

    def fetch_announcements(self, symbol: str, day: date) -> list[Announcement]:
        """获取指定日期的个股公告列表。"""
        ...

    def fetch_dragon_tiger(
        self, symbol: str, start: date, end: date
    ) -> list[DragonTiger]:
        """获取日期区间内的龙虎榜明细。"""
        ...

    def fetch_money_flow(self, symbol: str) -> MoneyFlow:
        """获取个股资金流。"""
        ...

    def fetch_external(self, category: str) -> list[ExternalQuote]:
        """获取指定类别的外围行情（港股/美股/期货/汇率等）。"""
        ...


__all__ = ["SourceError", "CacheMiss", "DataSource"]
