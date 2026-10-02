"""``fetch_external`` 美股分支候选名兼容单测（全离线，monkeypatch 不触网）。

背景：实测 akshare 1.18.84 没有 ``stock_us_index_spot_em`` 等设计书口径函数，
美股指数此前恒定降级为空。现按候选名依次探测，最后以 ``index_global_spot_em``
（全球指数，含美股三大指数）兜底并按名称过滤。
"""

from __future__ import annotations

import pandas as pd
import pytest

from ashare_prober.data.akshare_source import AkShareSource, _fetch_us_index_df
from ashare_prober.data.base import SourceError

# 模拟 index_global_spot_em 返回：含美股三大指数 + 其它市场指数（应被过滤）。
_GLOBAL_DF = pd.DataFrame(
    {
        "名称": ["道琼斯", "纳斯达克", "标普500", "澳大利亚标普200", "恒生指数"],
        "最新价": [42000.0, 18000.0, 5700.0, 22000.0, 38000.0],
        "涨跌幅": [0.5, -0.3, 0.2, 1.1, -0.6],
    }
)


def test_us_index_falls_back_to_global_when_candidates_missing(monkeypatch) -> None:  # noqa: ANN001
    """首选名不存在（1.18.84 实况）→ 回退 index_global_spot_em 并过滤美股指数。"""
    import akshare as ak

    for name in (
        "stock_us_index_spot_em",
        "stock_us_spot_em",
        "stock_us_index_spot_sina",
    ):
        monkeypatch.delattr(ak, name, raising=False)  # 模拟不存在

    calls: list[str] = []

    def fake_global() -> pd.DataFrame:
        calls.append("index_global_spot_em")
        return _GLOBAL_DF.copy()

    monkeypatch.setattr(ak, "index_global_spot_em", fake_global, raising=False)

    df = _fetch_us_index_df(ak)

    assert calls == ["index_global_spot_em"]
    # 只保留美股三大指数；「澳大利亚标普200」（含「标普」但非美股）与恒生指数被过滤掉
    assert list(df["名称"]) == ["道琼斯", "纳斯达克", "标普500"]


def test_fetch_external_us_index_returns_normalized_quotes(monkeypatch) -> None:  # noqa: ANN001
    """走完 ``fetch_external`` 全链路：归一为 ExternalQuote 且 category 正确。"""
    import akshare as ak

    for name in (
        "stock_us_index_spot_em",
        "stock_us_spot_em",
        "stock_us_index_spot_sina",
    ):
        monkeypatch.delattr(ak, name, raising=False)
    monkeypatch.setattr(
        ak, "index_global_spot_em", lambda: _GLOBAL_DF.copy(), raising=False
    )

    items = AkShareSource().fetch_external("美股指数")

    names = {it.name for it in items}
    assert names == {"道琼斯", "纳斯达克", "标普500"}
    assert all(it.category == "美股指数" for it in items)
    assert all(it.last > 0 for it in items)


def test_us_index_prefers_first_existing_candidate(monkeypatch) -> None:  # noqa: ANN001
    """候选接口存在且成功 → 直接使用，不触兜底。"""
    import akshare as ak

    calls: list[str] = []

    def fake_primary() -> pd.DataFrame:
        calls.append("stock_us_index_spot_em")
        return pd.DataFrame({"名称": ["道琼斯"], "最新价": [42000.0], "涨跌幅": [0.5]})

    def fake_global() -> pd.DataFrame:  # pragma: no cover - 不应被调用
        calls.append("index_global_spot_em")
        raise AssertionError("首选候选成功时不应触发兜底")

    monkeypatch.setattr(
        ak, "stock_us_index_spot_em", fake_primary, raising=False
    )
    monkeypatch.setattr(ak, "index_global_spot_em", fake_global, raising=False)

    df = _fetch_us_index_df(ak)

    assert calls == ["stock_us_index_spot_em"]
    assert list(df["名称"]) == ["道琼斯"]


def test_us_index_raises_source_error_when_all_fail(monkeypatch) -> None:  # noqa: ANN001
    """候选存在但全部调用失败 → 归一为 SourceError（供 Provider 走降级）。"""
    import akshare as ak

    def boom() -> pd.DataFrame:
        raise RuntimeError("network down")

    for name in (
        "stock_us_index_spot_em",
        "stock_us_spot_em",
        "stock_us_index_spot_sina",
        "index_global_spot_em",
    ):
        monkeypatch.setattr(ak, name, boom, raising=False)

    with pytest.raises(SourceError):
        _fetch_us_index_df(ak)
