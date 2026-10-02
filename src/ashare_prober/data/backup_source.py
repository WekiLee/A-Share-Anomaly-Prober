"""备用数据源（直连，T07）。

使用 ``httpx`` 直接调用腾讯 / 东方财富公开端点，作为 akshare 主源失败时的兜底。
本类与 ``AkShareSource`` 遵循同一 ``DataSource`` 协议，返回同构的归一模型。

实现范围：
- ``fetch_quote``：完整实现（腾讯为主、东财兜底），解析红涨绿跌无关的原始行情。
- 其余接口：主源失败时由 Provider 回退至此，按契约返回「空列表」或「SourceError」，
  结构保持正确，不阻断上层流程。

端点均为公开行情接口，无需鉴权；若将来加签名/限频，可在本文件集中调整。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Sequence

import httpx

from ashare_prober.data.base import DataSource, SourceError
from ashare_prober.data.models import (
    Announcement,
    DragonTiger,
    ExternalQuote,
    IndexQuote,
    LimitPoolItem,
    MarketBreadth,
    MoneyFlow,
    NewsItem,
    Quote,
    SectorFlow,
)

_TIMEOUT = 10.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}


class BackupSource:
    """腾讯 / 东财直连备源。"""

    name = "backup"

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _tencent_symbol(symbol: str) -> str:
        """将标的代码规整为腾讯格式（如 ``sh600600``）。"""
        s = symbol.lower().strip()
        if s[:2] in ("sh", "sz", "bj"):
            return s
        if s.startswith(("6", "9")):
            return "sh" + s
        if s.startswith(("0", "3")):
            return "sz" + s
        if s.startswith(("4", "8")):
            return "bj" + s
        return "sh" + s

    @staticmethod
    def _em_secid(symbol: str) -> str:
        """将标的代码规整为东财 secid（如 ``1.600600``）。"""
        s = symbol.lower().strip()
        code = re.sub(r"^[shszbj]+", "", s)
        if code.startswith(("6", "9")):
            return f"1.{code}"
        if code.startswith(("0", "3")):
            return f"0.{code}"
        if code.startswith(("4", "8")):
            return f"0.{code}"
        return f"1.{code}"

    # ------------------------------------------------------------------ #
    # 行情（完整实现）
    # ------------------------------------------------------------------ #
    def fetch_quote(self, symbol: str) -> Quote:
        # 腾讯为主，东财兜底；任一成功即返回归一 Quote。
        try:
            return self._fetch_quote_tencent(symbol)
        except SourceError:
            return self._fetch_quote_eastmoney(symbol)

    def _fetch_quote_tencent(self, symbol: str) -> Quote:
        ts = self._tencent_symbol(symbol)
        url = f"https://qt.gtimg.cn/q={ts}"
        try:
            resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            raise SourceError(
                f"腾讯行情请求失败: {exc}", source=self.name, cause=exc
            ) from exc
        return self._parse_tencent(resp.text, symbol)

    @staticmethod
    def _parse_tencent(content: str, symbol: str) -> Quote:
        match = re.search(r'="([^"]*)"', content)
        if not match:
            raise SourceError("腾讯行情返回格式异常（无行情串）", source="backup")
        fields = match.group(1).split("~")
        if len(fields) < 35:
            raise SourceError("腾讯行情字段不足", source="backup")

        def num(idx: int) -> float:
            try:
                return float(fields[idx])
            except (ValueError, IndexError):
                return 0.0

        # 字段口径见 DATA_SOURCES.md；成交量腾讯给「股」，转为「手」与 akshare 对齐。
        volume_shares = num(6)
        day = None
        if len(fields) > 30 and fields[30]:
            day = _parse_day(fields[30].split(" ")[0])

        return Quote(
            symbol=symbol,
            name=fields[1] if len(fields) > 1 else "",
            date=day,
            open=num(5),
            high=num(33),
            low=num(34),
            close=num(3),
            pre_close=num(4),
            volume=volume_shares / 100.0,
            amount=num(36),
            pct_change=num(32),
        )

    def _fetch_quote_eastmoney(self, symbol: str) -> Quote:
        secid = self._em_secid(symbol)
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": secid,
            "fields": "f43,f44,f45,f46,f57,f58,f60,f47,f48,f169,f170",
        }
        try:
            resp = httpx.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise SourceError(
                f"东财行情请求失败: {exc}", source=self.name, cause=exc
            ) from exc
        data = payload.get("data")
        if not data:
            raise SourceError("东财行情未返回数据", source="backup")

        def scale(value: Any, divisor: float = 1.0) -> float:
            if value is None:
                return 0.0
            try:
                return float(value) / divisor
            except (ValueError, TypeError):
                return 0.0

        return Quote(
            symbol=symbol,
            name=str(data.get("f58") or ""),
            date=datetime.now().date(),
            open=scale(data.get("f46"), 100),
            high=scale(data.get("f44"), 100),
            low=scale(data.get("f45"), 100),
            close=scale(data.get("f43"), 100),
            pre_close=scale(data.get("f60"), 100),
            volume=scale(data.get("f47"), 1),  # 东财成交量单位：手
            amount=scale(data.get("f48"), 1),  # 成交额单位：元
            pct_change=scale(data.get("f169"), 100),
        )

    # ------------------------------------------------------------------ #
    # 其余接口：结构正确，主源失败时兜底（空结果 / 显式不支持）
    # ------------------------------------------------------------------ #
    def fetch_news(self, symbol: str) -> list[NewsItem]:
        # 备源暂不实现新闻抓取，返回空列表（Provider 以「可用数据」继续）。
        return []

    def fetch_announcements(self, symbol: str, day: date) -> list[Announcement]:
        return []

    def fetch_dragon_tiger(
        self, symbol: str, start: date, end: date
    ) -> list[DragonTiger]:
        return []

    def fetch_money_flow(self, symbol: str) -> MoneyFlow:
        # 单值接口无法以「空列表」兜底；备源未实现时显式抛错，
        # 由 Provider 在「双源皆失败」时统一上报。
        raise SourceError(
            "backup source 未实现 fetch_money_flow", source=self.name
        )

    def fetch_external(self, category: str) -> list[ExternalQuote]:
        return []

    # ------------------------------------------------------------------ #
    # 大盘看板接口：备源未接入对应直连端点，显式抛错让 Provider 走降级
    # ------------------------------------------------------------------ #
    def fetch_index_quotes(self, codes: Sequence[str]) -> list[IndexQuote]:
        """备源未实现指数行情直连；抛错由 Provider 降级为空并记缺口。"""
        raise SourceError(
            "backup source 未实现 fetch_index_quotes", source=self.name
        )

    def fetch_market_breadth(self) -> MarketBreadth:
        """备源未实现全市场宽度统计（需拉全市场快照，成本高）；抛错降级。"""
        raise SourceError(
            "backup source 未实现 fetch_market_breadth", source=self.name
        )

    def fetch_sector_flow(self, top_n: int = 10) -> list[SectorFlow]:
        """备源未实现板块资金流；抛错降级。"""
        raise SourceError(
            "backup source 未实现 fetch_sector_flow", source=self.name
        )

    def fetch_limit_pool(self, day: date, kind: str = "up") -> list[LimitPoolItem]:
        """备源未实现涨跌停池；抛错降级。"""
        raise SourceError(
            f"backup source 未实现 fetch_limit_pool(kind={kind})",
            source=self.name,
        )


def _parse_day(text: str) -> date | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
