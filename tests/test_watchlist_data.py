"""用户自选名单数据层单测（全离线）。

覆盖：
- ``Store`` 新增 ``user_watchlist`` 表 CRUD（add/remove/set_active/list）；
- ``_norm_code`` 代码规范化（前缀/后缀/空格 + 非法输入）；
- ``resolve_watchlist`` 名单真源优先级（DB → 配置兜底 → 空）；
- ``AnomalyEngine.run_once(store=...)`` 名单注入与旧行为保持；
- ``SchedulerService`` 名单惰性解析（store 名单优先 / 显式 symbols 不被覆盖 /
  store 为 None 时维持配置兜底）。

**全部离线**：tmp_path 下的临时 SQLite，FakeProvider 不发任何网络请求。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from ashare_prober.anomaly.engine import AnomalyEngine
from ashare_prober.config import (
    PushSettings,
    ScheduleSettings,
    Settings,
    WatchlistSettings,
)
from ashare_prober.data.models import Quote
from ashare_prober.data.store import Store, _norm_code
from ashare_prober.data.watchlist import resolve_watchlist
from ashare_prober.scheduler.service import SchedulerService

DAY = date(2026, 10, 9)


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class CapturingProvider:
    """记录 get_quote 请求序列的假数据源（返回平稳行情，不产出事件）。"""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        self.requested.append(str(symbol))
        return Quote(
            symbol=str(symbol),
            name="平稳股",
            pre_close=100.0,
            close=100.5,
            amount=2e9,
            date=DAY,
        )

    def __getattr__(self, name: str):  # noqa: ANN204 - 其余取数方法一律降级
        def _missing(*args: Any, **kwargs: Any) -> None:
            return None

        return _missing


class CapturingScanFn:
    """记录 scan_fn 收到参数的假扫描函数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, list[str], Optional[date]]] = []

    def __call__(
        self,
        provider: Any,
        store: Any,
        symbols: Any,
        day: Optional[date],
    ) -> tuple[list[Any], list[tuple[str, str]]]:
        self.calls.append((provider, store, list(symbols), day))
        return [], []


def _fake_settings(symbols: Optional[list[str]] = None) -> SimpleNamespace:
    """带 watchlist.symbols 的假配置（避免触碰真实 get_settings）。"""
    return SimpleNamespace(watchlist=WatchlistSettings(symbols=symbols or []))


def _offline_settings(symbols: Optional[list[str]] = None) -> Settings:
    """离线测试配置：推送通道全空 + 推送/看板步骤关闭。"""
    return Settings(
        push=PushSettings(),
        watchlist=WatchlistSettings(symbols=symbols or []),
        schedule=ScheduleSettings(push=False, refresh_dashboard=False),
    )


def _patch_now_iso(monkeypatch: pytest.MonkeyPatch, start: str = "2026-01-01T09:00:00"):
    """把 store._now_iso 替换为自增秒的时间序列，保证 added_at 可排序。"""
    import ashare_prober.data.store as store_mod

    t = datetime.fromisoformat(start)
    counter = {"n": 0}

    def _fake() -> str:
        result = (t + timedelta(seconds=counter["n"])).isoformat()
        counter["n"] += 1
        return result

    monkeypatch.setattr(store_mod, "_now_iso", _fake)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "prober.db")


# --------------------------------------------------------------------------- #
# _norm_code
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sh600519", "600519"),
        ("600519.SH", "600519"),
        (" 600519 ", "600519"),
        ("SZ000001", "000001"),
        ("000001.sz", "000001"),
        ("bj430047", "430047"),
        ("600519", "600519"),
    ],
)
def test_norm_code_strips_market_affixes(raw: str, expected: str) -> None:
    """去交易所前缀/后缀/空格后应只剩 6 位数字。"""
    assert _norm_code(raw) == expected


@pytest.mark.parametrize("raw", ["贵州茅台", "60051", "6005191", "", "abc123", "sh6005", "60.0519"])
def test_norm_code_rejects_illegal(raw: str) -> None:
    """中文 / 位数不足 / 非数字等非法输入抛 ValueError。"""
    with pytest.raises(ValueError):
        _norm_code(raw)


# --------------------------------------------------------------------------- #
# Store CRUD
# --------------------------------------------------------------------------- #
def test_add_watchlist_new_returns_true_and_row(store: Store) -> None:
    assert store.add_watchlist("600519", name="贵州茅台") is True
    rows = store.list_watchlist()
    assert len(rows) == 1
    assert rows[0]["code"] == "600519"
    assert rows[0]["name"] == "贵州茅台"
    assert rows[0]["active"] is True
    assert rows[0]["added_at"]  # ISO 时间非空


def test_add_watchlist_duplicate_updates_and_returns_false(store: Store) -> None:
    store.add_watchlist("600519", name="旧名", active=True)
    assert store.add_watchlist("600519", name="新名", active=False) is False
    rows = store.list_watchlist()
    assert len(rows) == 1  # 不重复插入
    assert rows[0]["name"] == "新名"
    assert rows[0]["active"] is False


def test_add_watchlist_duplicate_preserves_existing_name(store: Store) -> None:
    """重复添加不带 name：原名称保留（不抹掉），返回值仍为 False。"""
    store.add_watchlist("sh600519", name="贵州茅台")
    assert store.add_watchlist("600519.SH") is False
    rows = store.list_watchlist()
    assert len(rows) == 1
    assert rows[0]["name"] == "贵州茅台"


def test_add_watchlist_duplicate_updates_nonempty_name(store: Store) -> None:
    """重复添加带非空新 name：名称更新为新值。"""
    store.add_watchlist("600519", name="旧名")
    assert store.add_watchlist("600519", name="新名") is False
    assert store.list_watchlist()[0]["name"] == "新名"


def test_add_watchlist_normalizes_code(store: Store) -> None:
    """sh600519 / 600519.SH 归一为同一行，重复插入返回 False。"""
    assert store.add_watchlist("sh600519") is True
    assert store.add_watchlist("600519.SH") is False
    assert len(store.list_watchlist()) == 1


def test_add_watchlist_illegal_code_raises(store: Store) -> None:
    with pytest.raises(ValueError):
        store.add_watchlist("茅台")


def test_remove_watchlist(store: Store) -> None:
    store.add_watchlist("600519")
    assert store.remove_watchlist("600519") is True
    assert store.list_watchlist() == []
    assert store.remove_watchlist("600519") is False  # 不存在返回 False


def test_set_watchlist_active(store: Store) -> None:
    store.add_watchlist("600519")
    assert store.set_watchlist_active("600519", False) is True
    assert store.list_watchlist()[0]["active"] is False
    assert store.set_watchlist_active("600519", True) is True
    assert store.list_watchlist()[0]["active"] is True
    assert store.set_watchlist_active("000001", True) is False  # 行不存在


def test_list_watchlist_order_desc_and_active_filter(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 added_at 降序（后加的在前）；active_only 排除已暂停。"""
    _patch_now_iso(monkeypatch)
    store.add_watchlist("600001", name="第一只")
    store.add_watchlist("600002", name="第二只")
    store.add_watchlist("600003", name="第三只")
    store.set_watchlist_active("600003", False)

    rows = store.list_watchlist()
    assert [r["code"] for r in rows] == ["600003", "600002", "600001"]

    active_rows = store.list_watchlist(active_only=True)
    assert [r["code"] for r in active_rows] == ["600002", "600001"]


# --------------------------------------------------------------------------- #
# resolve_watchlist
# --------------------------------------------------------------------------- #
def test_resolve_db_wins_over_config(store: Store) -> None:
    """DB 名单非空时为唯一真源，配置兜底被忽略。"""
    store.add_watchlist("600001")
    store.add_watchlist("600002")
    resolved = resolve_watchlist(store, _fake_settings(["999999"]))
    assert sorted(resolved) == ["600001", "600002"]


def test_resolve_falls_back_to_config_when_db_empty(store: Store) -> None:
    resolved = resolve_watchlist(store, _fake_settings(["600010", "600011"]))
    assert resolved == ["600010", "600011"]


def test_resolve_empty_when_both_empty(store: Store) -> None:
    assert resolve_watchlist(store, _fake_settings([])) == []


def test_resolve_excludes_paused_codes(store: Store) -> None:
    """active_only=True：已暂停的标的不参与解析。"""
    store.add_watchlist("600001")
    store.add_watchlist("600002")
    store.set_watchlist_active("600002", False)
    assert resolve_watchlist(store, _fake_settings(["999999"])) == ["600001"]


def test_resolve_store_none_uses_config() -> None:
    """store 为 None 时直接走配置兜底。"""
    assert resolve_watchlist(None, _fake_settings(["600519"])) == ["600519"]


def test_resolve_settings_none_uses_get_settings(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings 缺省时取 get_settings()（此处 monkeypatch 隔离真实配置）。"""
    import ashare_prober.config as config_mod

    monkeypatch.setattr(
        config_mod, "get_settings", lambda: _fake_settings(["600519"])
    )
    assert resolve_watchlist(store) == ["600519"]


# --------------------------------------------------------------------------- #
# engine.run_once(store=...)
# --------------------------------------------------------------------------- #
def test_engine_run_once_uses_store_watchlist(store: Store) -> None:
    """store 注入后仅扫描 DB 名单标的。"""
    provider = CapturingProvider()
    store.add_watchlist("600001")
    store.add_watchlist("600002")
    engine = AnomalyEngine(provider)
    events = engine.run_once(day=DAY, store=store)
    assert events == []
    # 注：排序语义由 test_list_watchlist_order_desc_and_active_filter 覆盖；
    # 此处同秒添加时间戳相同，仅断言扫描标的集合正确。
    assert sorted(provider.requested) == ["600001", "600002"]


def test_engine_run_once_store_none_keeps_legacy() -> None:
    """store=None 时保持旧行为：engine.watchlist 优先。"""
    provider = CapturingProvider()
    engine = AnomalyEngine(provider, watchlist=["000001"])
    engine.run_once(day=DAY, store=None)
    assert provider.requested == ["000001"]


def test_engine_run_once_store_empty_db_falls_back_to_settings(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """store 名单为空时降级配置兜底（通过 engine 的 store 路径）。"""
    import ashare_prober.config as config_mod

    monkeypatch.setattr(
        config_mod, "get_settings", lambda: _fake_settings(["600030"])
    )
    provider = CapturingProvider()
    engine = AnomalyEngine(provider)
    engine.run_once(day=DAY, store=store)
    assert provider.requested == ["600030"]


# --------------------------------------------------------------------------- #
# SchedulerService 名单惰性解析
# --------------------------------------------------------------------------- #
def test_scheduler_run_once_resolves_from_store(store: Store) -> None:
    """symbols=None + store 有名单 → run_once 实际扫描名单标的。"""
    store.add_watchlist("600001")
    store.add_watchlist("600002")
    scan_fn = CapturingScanFn()
    service = SchedulerService(
        provider=CapturingProvider(),
        store=store,
        settings=_offline_settings(),  # 配置 watchlist 为空，验证 DB 真源生效
        scan_fn=scan_fn,
    )
    summary = service.run_once(DAY)
    assert summary["events"] == 0
    assert len(scan_fn.calls) == 1
    _, _, symbols, day = scan_fn.calls[0]
    assert sorted(symbols) == ["600001", "600002"]
    assert day == DAY


def test_scheduler_run_once_lazy_resolves_newly_added(store: Store) -> None:
    """惰性解析：init 后新加的票，下一轮 run_once 即被覆盖。"""
    scan_fn = CapturingScanFn()
    service = SchedulerService(
        provider=CapturingProvider(),
        store=store,
        settings=_offline_settings(),
        scan_fn=scan_fn,
    )
    store.add_watchlist("600100")  # init 之后才加入
    service.run_once(DAY)
    assert scan_fn.calls[0][2] == ["600100"]


def test_scheduler_explicit_symbols_not_overridden(store: Store) -> None:
    """显式传入 symbols 时名单解析不生效（保持既有契约）。"""
    store.add_watchlist("600001")
    scan_fn = CapturingScanFn()
    service = SchedulerService(
        provider=CapturingProvider(),
        store=store,
        settings=_offline_settings(),
        scan_fn=scan_fn,
        symbols=["999999"],
    )
    service.run_once(DAY)
    assert scan_fn.calls[0][2] == ["999999"]


def test_scheduler_store_none_uses_config_symbols() -> None:
    """store 为 None 时维持旧行为：settings.watchlist.symbols。"""
    scan_fn = CapturingScanFn()
    service = SchedulerService(
        provider=CapturingProvider(),
        store=None,
        settings=_offline_settings(["600519"]),
        scan_fn=scan_fn,
    )
    service.run_once(DAY)
    assert scan_fn.calls[0][2] == ["600519"]


def test_scheduler_store_empty_db_falls_back_to_config(store: Store) -> None:
    """store 名单为空时降级配置兜底。"""
    scan_fn = CapturingScanFn()
    service = SchedulerService(
        provider=CapturingProvider(),
        store=store,
        settings=_offline_settings(["600030"]),
        scan_fn=scan_fn,
    )
    service.run_once(DAY)
    assert scan_fn.calls[0][2] == ["600030"]
