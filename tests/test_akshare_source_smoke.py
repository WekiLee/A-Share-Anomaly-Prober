"""akshare 主源网络冒烟测试。

默认 **skip**（不联网 / 未安装 akshare 时不失败）。需要真实验证时：
    设置环境变量 RUN_NETWORK_TESTS=1 并联网，再运行：
    pytest -m network tests/test_akshare_source_smoke.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.network


def _akshare_available() -> bool:
    try:
        import akshare  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.network
def test_akshare_fetch_quote_smoke() -> None:
    if not _akshare_available():
        pytest.skip("akshare 未安装，跳过冒烟测试")
    if os.environ.get("RUN_NETWORK_TESTS") != "1":
        pytest.skip("默认跳过网络冒烟测试；设置 RUN_NETWORK_TESTS=1 并联网后运行")

    from ashare_prober.data.akshare_source import AkShareSource

    source = AkShareSource()
    quote = source.fetch_quote("600600")
    assert quote.symbol == "600600"
    assert isinstance(quote.close, float)
