"""标的解析单测（T13）。

全部离线：通过注入假 loader 与临时目录 Cache，不访问网络、不依赖 akshare。
"""

from __future__ import annotations

import sys

import pytest

import ashare_prober.data.symbols as symbols
from ashare_prober.data.base import SourceError
from ashare_prober.data.cache import Cache
from ashare_prober.data.symbols import (
    SymbolInfo,
    SymbolNotFound,
    SymbolResolver,
    infer_market,
    load_from_akshare,
)

# --------------------------------------------------------------------------- #
# 假数据
# --------------------------------------------------------------------------- #
FAKE_ROWS: list[dict[str, str]] = [
    {"code": "600519", "name": "贵州茅台"},
    {"code": "000001", "name": "平安银行"},
    {"code": "300750", "name": "宁德时代"},
    {"code": "688981", "name": "中芯国际"},
    {"code": "830799", "name": "艾融软件"},
    {"code": "600887", "name": "伊利股份"},
    {"code": "601318", "name": "中国平安"},
    {"code": "600036", "name": "招商银行"},
    {"code": "920002", "name": "万达轴承"},
]


class _CountingLoader:
    """可计数的假标的表加载器（返回 dict 行，验证归一能力）。"""

    def __init__(self, rows: list[dict[str, str]] | None = None) -> None:
        self.rows: list[dict[str, str]] = list(rows if rows is not None else FAKE_ROWS)
        self.calls: int = 0

    def __call__(self) -> list[dict[str, str]]:
        self.calls += 1
        return [dict(row) for row in self.rows]


def _boom() -> list[dict[str, str]]:
    """必然失败的加载器（模拟网络/接口异常）。"""
    raise RuntimeError("network down")


@pytest.fixture
def cache(tmp_path):  # noqa: ANN001
    """临时目录缓存，用例结束后关闭连接（Windows 下避免文件占用）。"""
    handle = Cache(tmp_path / "symbols_cache.db")
    yield handle
    handle.close()


@pytest.fixture
def resolver(cache) -> SymbolResolver:  # noqa: ANN001
    """注入假数据与临时缓存的解析器。"""
    return SymbolResolver(cache=cache, loader=_CountingLoader())


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    ["sh600519", "SH600519", "600519.SH", "600519.sh", "  600519  ", "600519"],
)
def test_normalize_code_variants(resolver: SymbolResolver, raw: str) -> None:
    assert resolver.normalize(raw) == "600519"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("sz000001", "000001"), ("000001.SZ", "000001"), ("bj830799", "830799")],
)
def test_normalize_market_prefix_and_suffix(
    resolver: SymbolResolver, raw: str, expected: str
) -> None:
    assert resolver.normalize(raw) == expected


def test_normalize_fullwidth(resolver: SymbolResolver) -> None:
    """全角字符（ＳＨ６００５１９）应归一为半角代码。"""
    assert resolver.normalize("ＳＨ６００５１９") == "600519"


@pytest.mark.parametrize("raw", ["贵州茅台", "  茅台  ", "贵州 茅台"])
def test_normalize_chinese_unchanged(resolver: SymbolResolver, raw: str) -> None:
    """中文输入原样返回（去空白、不删字）。"""
    assert resolver.normalize(raw) == "".join(raw.split())


def test_normalize_empty(resolver: SymbolResolver) -> None:
    assert resolver.normalize("") == ""
    assert resolver.normalize("   ") == ""


# --------------------------------------------------------------------------- #
# market 推断 / prefixed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("600519", "sh"),
        ("688981", "sh"),
        ("000001", "sz"),
        ("300750", "sz"),
        ("830799", "bj"),
        ("920002", "bj"),
        ("510300", "sh"),  # ETF：5 开头归沪
    ],
)
def test_market_inference(code: str, market: str) -> None:
    assert infer_market(code) == market
    assert SymbolInfo(code=code, name="x").market == market


@pytest.mark.parametrize(
    ("code", "prefixed"),
    [("600519", "sh600519"), ("000001", "sz000001"), ("830799", "bj830799")],
)
def test_prefixed_property(code: str, prefixed: str) -> None:
    assert SymbolInfo(code=code, name="x").prefixed == prefixed


# --------------------------------------------------------------------------- #
# from_akshare_row
# --------------------------------------------------------------------------- #
def test_from_akshare_row_english_keys() -> None:
    item = SymbolInfo.from_akshare_row({"code": "600519", "name": "贵州茅台"})
    assert (item.code, item.name) == ("600519", "贵州茅台")


def test_from_akshare_row_chinese_keys() -> None:
    item = SymbolInfo.from_akshare_row({"代码": "000001", "名称": "平安银行"})
    assert (item.code, item.name) == ("000001", "平安银行")


def test_from_akshare_row_numeric_code_and_padding() -> None:
    """整数代码 / 不足 6 位代码应归一为 6 位纯数字。"""
    item = SymbolInfo.from_akshare_row({"code": 600519, "name": "贵州茅台"})
    assert item.code == "600519"
    padded = SymbolInfo.from_akshare_row({"代码": "1519", "名称": "测试"})
    assert padded.code == "001519"


def test_from_akshare_row_missing_fields() -> None:
    item = SymbolInfo.from_akshare_row({})
    assert item.code == ""
    assert item.name == ""


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #
def test_resolve_exact_code(resolver: SymbolResolver) -> None:
    assert resolver.resolve("600519").name == "贵州茅台"


def test_resolve_prefixed_input(resolver: SymbolResolver) -> None:
    """带前缀 / 后缀的输入应能解析。"""
    assert resolver.resolve("sh600519").code == "600519"
    assert resolver.resolve("600519.SH").code == "600519"


def test_resolve_name_exact(resolver: SymbolResolver) -> None:
    assert resolver.resolve("贵州茅台").code == "600519"


def test_resolve_name_fuzzy(resolver: SymbolResolver) -> None:
    """模糊名称（茅台）唯一命中时直接返回。"""
    assert resolver.resolve("茅台").code == "600519"


def test_resolve_code_prefix(resolver: SymbolResolver) -> None:
    """2~6 位数字前缀唯一命中时直接返回。"""
    assert resolver.resolve("6005").code == "600519"


def test_resolve_unknown_code_raises(resolver: SymbolResolver) -> None:
    with pytest.raises(SymbolNotFound):
        resolver.resolve("999999")


def test_resolve_fuzzy_multi_hit_raises_with_candidates(
    resolver: SymbolResolver,
) -> None:
    """多命中时抛 SymbolNotFound，并携带候选供消歧。"""
    with pytest.raises(SymbolNotFound) as excinfo:
        resolver.resolve("银行")
    assert len(excinfo.value.candidates) >= 2
    assert {item.code for item in excinfo.value.candidates} == {"000001", "600036"}


def test_resolve_code_prefix_multi_hit_raises(resolver: SymbolResolver) -> None:
    with pytest.raises(SymbolNotFound) as excinfo:
        resolver.resolve("600")
    assert len(excinfo.value.candidates) >= 2


def test_resolve_not_found_has_empty_candidates(resolver: SymbolResolver) -> None:
    """查不到时抛 SymbolNotFound，候选为空。"""
    with pytest.raises(SymbolNotFound) as excinfo:
        resolver.resolve("不存在的公司")
    assert excinfo.value.candidates == []


def test_resolve_not_found_message_contains_query(resolver: SymbolResolver) -> None:
    """异常消息须包含用户原始输入，便于 CLI 回显。"""
    with pytest.raises(SymbolNotFound) as excinfo:
        resolver.resolve("绝无此票")
    assert "绝无此票" in str(excinfo.value)


def test_resolve_empty_query_raises(resolver: SymbolResolver) -> None:
    with pytest.raises(SymbolNotFound):
        resolver.resolve("   ")


def test_resolve_matches_full_name_over_fragment(resolver: SymbolResolver) -> None:
    """「中国平安」精确优先于「平安银行」的包含匹配。"""
    assert resolver.resolve("中国平安").code == "601318"


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def test_search_returns_candidates(resolver: SymbolResolver) -> None:
    hits = resolver.search("银行")
    assert {item.code for item in hits} == {"000001", "600036"}


def test_search_exact_first(resolver: SymbolResolver) -> None:
    """精确匹配排在最前。"""
    hits = resolver.search("600519")
    assert hits and hits[0].code == "600519"


def test_search_limit(resolver: SymbolResolver) -> None:
    assert len(resolver.search("600", limit=1)) == 1


def test_search_never_raises() -> None:
    """标的表不可用 / 空查询时返回空列表，不抛异常。"""
    broken = SymbolResolver(cache=Cache(None), loader=_boom)
    assert broken.search("茅台") == []
    assert broken.search("") == []
    assert broken.search("   ") == []


def test_search_no_match_returns_empty(resolver: SymbolResolver) -> None:
    assert resolver.search("绝无此票") == []


# --------------------------------------------------------------------------- #
# resolve_many
# --------------------------------------------------------------------------- #
def test_resolve_many_partial_failure(resolver: SymbolResolver) -> None:
    """单个失败不阻断其它标的解析。"""
    resolved, failed = resolver.resolve_many(["600519", "绝无此票", "茅台"])
    assert [item.code for item in resolved] == ["600519", "600519"]
    assert failed == ["绝无此票"]


def test_resolve_many_empty_input(resolver: SymbolResolver) -> None:
    assert resolver.resolve_many([]) == ([], [])


# --------------------------------------------------------------------------- #
# 缓存行为
# --------------------------------------------------------------------------- #
def test_table_uses_cache_loader_called_once(cache) -> None:  # noqa: ANN001
    """首次回源，第二次（含新实例）命中缓存，loader 仅调用一次。"""
    loader = _CountingLoader()
    first = SymbolResolver(cache=cache, loader=loader)
    assert len(first.table()) == len(FAKE_ROWS)
    assert loader.calls == 1

    second = SymbolResolver(cache=cache, loader=loader)
    assert len(second.table()) == len(FAKE_ROWS)
    assert loader.calls == 1


def test_table_force_reload(cache) -> None:  # noqa: ANN001
    """force=True 跳过缓存强制回源。"""
    loader = _CountingLoader()
    resolver_obj = SymbolResolver(cache=cache, loader=loader)
    resolver_obj.table()
    resolver_obj.table(force=True)
    assert loader.calls == 2


def test_table_falls_back_to_cache_when_loader_fails(cache) -> None:  # noqa: ANN001
    """回源失败但有缓存时，沿用已缓存名单（不抛异常）。"""
    SymbolResolver(cache=cache, loader=_CountingLoader()).table()
    degraded = SymbolResolver(cache=cache, loader=_boom)
    assert len(degraded.table()) == len(FAKE_ROWS)


def test_table_raises_source_error_without_cache() -> None:
    """回源失败且无缓存时抛 SourceError（严禁编造数据）。"""
    resolver_obj = SymbolResolver(cache=Cache(None), loader=_boom)
    with pytest.raises(SourceError):
        resolver_obj.table()


def test_table_empty_loader_returns_empty_without_error(cache) -> None:  # noqa: ANN001
    """加载器返回空表时返回空列表（由 resolve 层报「未找到」）。"""
    resolver_obj = SymbolResolver(cache=cache, loader=lambda: [])
    assert resolver_obj.table() == []


# --------------------------------------------------------------------------- #
# 拼音首字母（软依赖）
# --------------------------------------------------------------------------- #
def test_pinyin_missing_is_silent(resolver: SymbolResolver, monkeypatch) -> None:  # noqa: ANN001
    """缺 pypinyin 时拼音策略静默跳过，绝不因软依赖缺失而报错。"""
    monkeypatch.setattr(symbols, "_PYPINYIN_UNAVAILABLE", False)
    monkeypatch.setitem(sys.modules, "pypinyin", None)

    assert symbols._pinyin_initials("贵州茅台") == ""
    with pytest.raises(SymbolNotFound):
        resolver.resolve("gzmt")


def test_pinyin_match_when_available(resolver: SymbolResolver, monkeypatch) -> None:  # noqa: ANN001
    """拼音首字母可用时（此处以桩函数模拟）能命中标的。"""
    table = {"贵州茅台": "gzmt", "平安银行": "payh", "招商银行": "zsyh", "中国平安": "zgpa"}
    monkeypatch.setattr(symbols, "_pinyin_initials", lambda name: table.get(name, ""))

    assert resolver.resolve("gzmt").code == "600519"
    hits = resolver.search("zsyh")
    assert [item.code for item in hits] == ["600036"]


def test_pinyin_multi_hit_raises_with_candidates(
    resolver: SymbolResolver, monkeypatch
) -> None:  # noqa: ANN001
    table = {"贵州茅台": "gz", "招商银行": "zs", "中国平安": "zg"}
    monkeypatch.setattr(symbols, "_pinyin_initials", lambda name: table.get(name, ""))
    with pytest.raises(SymbolNotFound) as excinfo:
        resolver.resolve("z")
    assert len(excinfo.value.candidates) == 2


# --------------------------------------------------------------------------- #
# akshare 默认加载器（惰性导入，离线校验异常路径）
# --------------------------------------------------------------------------- #
def test_load_from_akshare_raises_when_akshare_missing(monkeypatch) -> None:  # noqa: ANN001
    """akshare 不可导入时抛 SourceError（不污染模块导入期）。"""
    monkeypatch.setitem(sys.modules, "akshare", None)
    with pytest.raises(SourceError):
        load_from_akshare()


def test_module_exports() -> None:
    for name in (
        "SymbolInfo",
        "SymbolResolver",
        "SymbolNotFound",
        "load_from_akshare",
    ):
        assert name in symbols.__all__
