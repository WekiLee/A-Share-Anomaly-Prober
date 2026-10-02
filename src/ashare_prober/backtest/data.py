"""回测日线取数（T26）。

``fetch_daily(symbol, start, end)``：akshare 东财日线主源 + 新浪日线回退，
归一为 ``Quote`` 列表（按日期升序，含 ``pre_close`` 逐日回填）。

设计约束：
- akshare 函数内**惰性 import**（不联网不加载）。
- 不 import ``data.akshare_source``（避免跨模块耦合），交易所前缀推断在本模块
  自带一份 ``_market_of``。
- 两个源皆失败时抛 ``SourceError(source="backtest.data")``。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from ashare_prober.data.base import SourceError
from ashare_prober.data.models import Quote

__all__ = ["fetch_daily", "_market_of"]


def _market_of(symbol: str) -> str:
    """根据代码前缀推断交易所前缀（东财接口用纯代码，新浪接口需 ``sh/sz/bj`` 前缀）。

    规则：60/68（含 9/5 B股与基金）→ sh；0/3 → sz；4/8 → bj；
    未知前缀抛 ValueError（不猜）。
    """
    code = symbol.lower().lstrip("shszbj.")
    if code.startswith(("60", "68", "9", "5")):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    raise ValueError(f"无法识别的 A 股代码前缀: {symbol}")


def _to_float(value: Any, default: float = 0.0) -> float:
    """容错转 float（与 data.models 同风格，本地复制避免私有依赖）。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "--", "None", "nan", "NaN"):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _rows_from_hist(df: Any, symbol: str) -> list[Quote]:
    """东财 ``stock_zh_a_hist`` 中文列 DataFrame → Quote 列表（升序）。

    该接口无「昨收」列：``pre_close`` 用前一日收盘回填；首日按「涨跌幅」反推
    （close / (1 + pct/100)），推不出则置 0（规则对 pre_close<=0 不触发，安全）。
    """
    quotes: list[Quote] = []
    for _, row in df.iterrows():
        record: Mapping[str, Any] = row.to_dict()
        quote = Quote.from_akshare_row(record)
        quote.symbol = symbol  # 历史接口行内无代码列，统一回填
        if quote.date is None:
            continue  # 无日期行不可回放，丢弃
        quotes.append(quote)

    quotes.sort(key=lambda q: q.date or date.min)
    for i, quote in enumerate(quotes):
        if i > 0:
            quote.pre_close = quotes[i - 1].close
        elif quote.pct_change != 0.0 and quote.close > 0:
            quote.pre_close = quote.close / (1.0 + quote.pct_change / 100.0)
    return quotes


def _rows_from_daily(df: Any, symbol: str) -> list[Quote]:
    """新浪 ``stock_zh_a_daily`` 英文列 DataFrame → Quote 列表（升序）。

    列：date/open/high/low/close/volume（部分版本含 amount）。
    ``pct_change`` 用前收盘自行计算（首日无前收盘置 0.0）；
    ``pre_close`` 首日置 0.0、其后回填前一日收盘。
    """
    quotes: list[Quote] = []
    for _, row in df.iterrows():
        record: Mapping[str, Any] = row.to_dict()
        quote = Quote.from_akshare_row(record)
        quote.symbol = symbol  # 新浪日线行内无代码列，统一回填
        if quote.date is None:
            continue
        quotes.append(quote)

    quotes.sort(key=lambda q: q.date or date.min)
    for i, quote in enumerate(quotes):
        if i > 0:
            prev_close = quotes[i - 1].close
            quote.pre_close = prev_close
            quote.pct_change = (
                (quote.close - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
            )
        else:
            quote.pct_change = 0.0  # 首日无前收盘，如实置 0
    return quotes


def fetch_daily(symbol: str, start: date, end: date) -> list[Quote]:
    """拉取标的 ``[start, end]`` 区间日线（未复权），按日期升序返回。

    主源：东财 ``stock_zh_a_hist``（中文列）；失败或为空时回退
    新浪 ``stock_zh_a_daily``（英文列）。两者皆败抛 ``SourceError``。

    Raises:
        SourceError: 主源与回退源均失败（或均无数据）时。
        ValueError: 代码前缀无法识别时。
    """
    code = symbol.lower().lstrip("shszbj.")
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    quotes: list[Quote] = []
    primary_error: Exception | None = None

    # ---- 主源：东方财富 stock_zh_a_hist ---- #
    try:
        import akshare as ak  # type: ignore # noqa: PLC0415 - 惰性 import

        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_str,
            end_date=end_str,
            adjust="",
        )
        if df is not None and not df.empty:
            quotes = _rows_from_hist(df, symbol)
    except Exception as exc:  # noqa: BLE001 - 归一为 SourceError 前先尝试回退
        primary_error = exc

    if quotes:
        return quotes

    # ---- 回退：新浪 stock_zh_a_daily ---- #
    try:
        import akshare as ak  # type: ignore # noqa: PLC0415 - 惰性 import

        df = ak.stock_zh_a_daily(
            symbol=f"{_market_of(symbol)}{code}",
            start_date=start_str,
            end_date=end_str,
        )
        if df is not None and not df.empty:
            quotes = _rows_from_daily(df, symbol)
    except Exception as exc:  # noqa: BLE001 - 两源皆败，如实上抛
        raise SourceError(
            f"回测日线获取失败（东财/新浪均失败）: {symbol} "
            f"[{start_str}-{end_str}] 主源错误={primary_error} 回退错误={exc}",
            source="backtest.data",
            cause=exc,
        ) from exc

    if not quotes:
        raise SourceError(
            f"回测日线获取失败：东财/新浪均未返回数据: {symbol} [{start_str}-{end_str}]",
            source="backtest.data",
            cause=primary_error,
        )
    return quotes
