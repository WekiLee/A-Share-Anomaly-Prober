"""数据门面单测：使用假的 DataSource（不联网）验证三条路径。

覆盖：主源成功走主源 / 主源抛错回退备源 / 结果写缓存。
"""

from __future__ import annotations

from datetime import date, datetime

from ashare_prober.config import TTL
from ashare_prober.data.base import SourceError
from ashare_prober.data.cache import Cache
from ashare_prober.data.models import MoneyFlow, Quote
from ashare_prober.data.provider import Provider


class FakeStore:
    """记录落库行为的最小化 Store 替身。"""

    def __init__(self) -> None:
        self.saved_quotes: list[Quote] = []
        self.saved_news_calls = 0

    def save_quote(self, quote: Quote) -> None:
        self.saved_quotes.append(quote)

    def save_news(self, items) -> None:  # noqa: ANN001
        self.saved_news_calls += 1


class FakeSource:
    """可配置成败的假数据源（不联网，结构同 DataSource 协议）。"""

    name = "fake"

    def __init__(
        self,
        quote: Quote | None = None,
        fail_quote: bool = False,
        fail_money: bool = False,
    ) -> None:
        self._quote = quote or Quote(
            symbol="600600", name="测试", close=10.0, date=date(2026, 1, 1)
        )
        self.fail_quote = fail_quote
        self.fail_money = fail_money
        self.quote_calls = 0

    def fetch_quote(self, symbol: str) -> Quote:
        self.quote_calls += 1
        if self.fail_quote:
            raise SourceError("primary boom", source=self.name)
        return self._quote

    def fetch_news(self, symbol: str):
        return []

    def fetch_announcements(self, symbol: str, day: date):
        return []

    def fetch_dragon_tiger(self, symbol: str, start: date, end: date):
        return []

    def fetch_money_flow(self, symbol: str) -> MoneyFlow:
        if self.fail_money:
            raise SourceError("money boom", source=self.name)
        return MoneyFlow(code=symbol, main_net_inflow=100.0)

    def fetch_external(self, category: str):
        return []


def _provider(primary, backup, tmp_path):  # noqa: ANN001
    cache = Cache(tmp_path / "prov.cache.db")
    store = FakeStore()
    return Provider(primary, backup, cache, store), store


def test_primary_success_uses_primary_only(tmp_path) -> None:  # noqa: ANN001
    primary = FakeSource(fail_quote=False)
    backup = FakeSource(fail_quote=True)  # 若被错误调用会抛错
    prov, store = _provider(primary, backup, tmp_path)

    result = prov.get_quote("600600")

    assert result.close == 10.0
    assert primary.quote_calls == 1
    assert backup.quote_calls == 0  # 主源成功，不应回退
    assert len(store.saved_quotes) == 1


def test_primary_failure_falls_back_to_backup(tmp_path) -> None:  # noqa: ANN001
    primary = FakeSource(fail_quote=True)
    backup = FakeSource(fail_quote=False)
    prov, store = _provider(primary, backup, tmp_path)

    result = prov.get_quote("600600")

    assert result.close == 10.0
    assert primary.quote_calls == 1
    assert backup.quote_calls == 1  # 已回退
    assert len(store.saved_quotes) == 1


def test_result_written_to_cache(tmp_path) -> None:  # noqa: ANN001
    primary = FakeSource()
    backup = FakeSource(fail_quote=True)
    prov, _ = _provider(primary, backup, tmp_path)

    prov.get_quote("600600")

    # 直接从缓存读取应命中，且不再调用数据源
    cached = prov.cache.get("quote:600600", TTL.QUOTE)
    assert Quote.model_validate(cached).close == 10.0
    calls_after = primary.quote_calls
    prov.get_quote("600600")
    assert primary.quote_calls == calls_after  # 命中缓存，未再次回源


def test_get_money_flow_source_error_propagates(tmp_path) -> None:  # noqa: ANN001
    primary = FakeSource(fail_money=True)
    backup = FakeSource(fail_money=True)
    prov, _ = _provider(primary, backup, tmp_path)

    try:
        prov.get_money_flow("600600")
        raise AssertionError("expected SourceError when both sources fail")
    except SourceError:
        pass


def test_get_dossier_inputs_resilient_to_failures(tmp_path) -> None:  # noqa: ANN001
    # 主源/备源对新闻等返回空，资金流双源失败 -> 兜底为 None，不应抛错
    primary = FakeSource(fail_money=True)
    backup = FakeSource(fail_money=True)
    prov, _ = _provider(primary, backup, tmp_path)

    inputs = prov.get_dossier_inputs("600600", datetime(2026, 10, 9, 15, 0))
    assert inputs["symbol"] == "600600"
    assert inputs["quote"] is not None  # 行情成功
    assert inputs["money_flow"] is None  # 资金流双源失败 -> 兜底 None
    assert inputs["news"] == []  # 空列表兜底
