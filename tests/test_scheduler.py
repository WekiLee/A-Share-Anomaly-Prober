"""定时调度单测（T12）。

**全部离线**：
- 扫描路径注入 FakeProvider / FakeStore / 假 ``scan_fn``，不发任何网络请求；
- 推送路径 monkeypatch 掉 ``PushManager`` 依赖（假通道 / 空 webhook 配置）；
- 看板路径 ``provider=None``（采集器降级为空看板，仍可渲染）；
- apscheduler 通过 ``sys.modules`` 注入 ``None`` 模拟未安装环境。

覆盖：jobs 三函数（含异常隔离 / day 透传）、``SchedulerService.run_once``
汇总与开关、``start()`` 缺依赖提示、``ScheduleSettings`` 时刻校验回退。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pytest

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.config import (
    PushSettings,
    ScheduleSettings,
    Settings,
    WatchlistSettings,
)
from ashare_prober.data.models import Quote
from ashare_prober.push.models import PushResult
from ashare_prober.scheduler import jobs as jobs_mod
from ashare_prober.scheduler import service as service_mod
from ashare_prober.scheduler.jobs import push_events, refresh_dashboard, scan_watchlist
from ashare_prober.scheduler.service import SchedulerService

DAY = date(2026, 10, 9)


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeProvider:
    """按代码返回预设 Quote 的假数据源（与 test_engine 口径一致）。"""

    def get_quote(self, symbol: str) -> Quote:
        if symbol == "600600":
            # 异动样本：放量破位
            return Quote(
                symbol="600600",
                name="异动股",
                pre_close=100.0,
                close=90.0,
                amount=2e9,
                date=DAY,
            )
        return Quote(
            symbol=symbol,
            name="正常股",
            pre_close=100.0,
            close=100.3,
            amount=2e9,
            date=DAY,
        )

    def get_money_flow(self, symbol: str) -> None:
        return None

    def __getattr__(self, name: str):  # noqa: ANN204 - 兜底：其余取数方法一律降级
        def _missing(*args: Any, **kwargs: Any) -> None:
            return None

        return _missing


class FakeStore:
    """记录 save_event 调用的假本地库。"""

    def __init__(self) -> None:
        self.saved: list[AnomalyEvent] = []

    def save_event(self, event: AnomalyEvent) -> None:
        self.saved.append(event)

    def list_events(self, limit: int = 10, since: Any = None) -> list[dict]:
        return []


def _make_event(code: str = "600600", seq: int = 1) -> AnomalyEvent:
    return AnomalyEvent(
        id=f"EVT-{code}-20261009-{seq:03d}",
        ref_code=code,
        ref_name="假标的",
        type="volume_break_down",
        level="alert",
        detected_at=datetime(2026, 10, 9, 15, 5, 0),
    )


def _settings(
    *,
    symbols: Optional[list[str]] = None,
    push: bool = True,
    refresh: bool = True,
) -> Settings:
    """构造离线测试配置：推送通道全空 + 显式开关。"""
    return Settings(
        push=PushSettings(),  # webhook 全空：PushManager 不装配通道
        watchlist=WatchlistSettings(symbols=symbols or []),
        schedule=ScheduleSettings(push=push, refresh_dashboard=refresh),
    )


# --------------------------------------------------------------------------- #
# scan_watchlist
# --------------------------------------------------------------------------- #
def test_scan_watchlist_produces_and_saves_events() -> None:
    """正常路径：真实引擎 + FakeProvider，异动标的产出事件并落库。"""
    store = FakeStore()

    events, failures = scan_watchlist(FakeProvider(), store, ["600600", "000001"], DAY)

    assert failures == []
    assert len(events) == 1
    assert events[0].ref_code == "600600"
    assert events[0].status == "tentative"
    assert [e.id for e in store.saved] == [events[0].id]


def test_scan_watchlist_isolates_symbol_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """某 symbol 抛异常时其余照常，failures 记录该 symbol。"""

    class BoomEngine:
        """对 ``BAD`` 抛错的假引擎（验证 scan_watchlist 自身的隔离逻辑）。"""

        calls: list[str] = []

        def __init__(self, provider: Any, store: Any = None) -> None:
            self.store = store

        def scan(self, scope: str, ref_code: str, day: Optional[date] = None):
            BoomEngine.calls.append(ref_code)
            if ref_code == "BAD":
                raise RuntimeError("scan boom")
            event = _make_event(ref_code)
            if self.store is not None:
                self.store.save_event(event)
            return [event]

    monkeypatch.setattr(jobs_mod, "AnomalyEngine", BoomEngine)
    store = FakeStore()

    events, failures = scan_watchlist(
        FakeProvider(), store, ["000001", "BAD", "600600"], DAY
    )

    assert BoomEngine.calls == ["000001", "BAD", "600600"]  # BAD 不阻断后续
    assert [e.ref_code for e in events] == ["000001", "600600"]
    assert len(failures) == 1
    assert failures[0][0] == "BAD"
    assert "scan boom" in failures[0][1]
    assert len(store.saved) == 2


def test_scan_watchlist_passes_day_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """day 参数逐标的透传给引擎。"""
    seen: list[Optional[date]] = []

    class RecordingEngine:
        def __init__(self, provider: Any, store: Any = None) -> None:
            pass

        def scan(self, scope: str, ref_code: str, day: Optional[date] = None):
            seen.append(day)
            return []

    monkeypatch.setattr(jobs_mod, "AnomalyEngine", RecordingEngine)

    events, failures = scan_watchlist(FakeProvider(), None, ["A", "B"], DAY)

    assert events == [] and failures == []
    assert seen == [DAY, DAY]


# --------------------------------------------------------------------------- #
# push_events
# --------------------------------------------------------------------------- #
def test_push_events_no_channels_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置任何通道时安静返回 [] 且不抛异常。"""
    from ashare_prober.push import manager as manager_mod

    monkeypatch.setattr(
        manager_mod, "get_settings", lambda: Settings(push=PushSettings())
    )

    result = push_events([_make_event()])

    assert result == []


def test_push_events_empty_input_skips_manager() -> None:
    """事件为空时不构造推送管理器，直接返回 []。"""
    assert push_events([]) == []


def test_push_events_calls_send_event_per_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有通道时逐事件调用 send_event，并聚合逐通道结果。"""
    from ashare_prober.push import manager as manager_mod

    sent: list[AnomalyEvent] = []

    class FakeManager:
        channels = [object()]  # 非空即可进入逐事件循环

        def send_event(self, event: AnomalyEvent, extra: str = "") -> list[PushResult]:
            sent.append(event)
            return [PushResult(channel="fake", ok=True, message="ok")]

    monkeypatch.setattr(manager_mod, "PushManager", FakeManager)

    events = [_make_event("600600", 1), _make_event("000001", 2)]
    results = push_events(events)

    assert sent == events  # 每个事件恰好推送一次
    assert len(results) == 2
    assert all(r.ok for r in results)


def test_push_events_isolates_single_event_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单事件推送抛异常时，其余事件照常推送。"""
    from ashare_prober.push import manager as manager_mod

    pushed: list[str] = []

    class FlakyManager:
        channels = [object()]

        def send_event(self, event: AnomalyEvent, extra: str = "") -> list[PushResult]:
            if event.ref_code == "BAD":
                raise RuntimeError("send boom")
            pushed.append(event.ref_code)
            return [PushResult(channel="fake", ok=True)]

    monkeypatch.setattr(manager_mod, "PushManager", FlakyManager)

    results = push_events([_make_event("BAD"), _make_event("600600", 2)])

    assert pushed == ["600600"]
    assert len(results) == 1


# --------------------------------------------------------------------------- #
# refresh_dashboard
# --------------------------------------------------------------------------- #
def test_refresh_dashboard_writes_index_html(tmp_path: Path) -> None:
    """正常路径（provider=None 降级空看板）：写出 index.html 并返回 True。"""
    out_dir = tmp_path / "outputs"

    ok = refresh_dashboard(out_dir, provider=None, store=None)

    assert ok is True
    html = out_dir / "index.html"
    assert html.exists()
    assert html.read_text(encoding="utf-8").strip() != ""


def test_refresh_dashboard_swallows_builder_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """采集器抛异常时返回 False 且不抛。"""
    from ashare_prober.dashboard import collector as collector_mod

    class BoomBuilder:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def build(self, day: Any = None) -> Any:
            raise RuntimeError("builder boom")

    monkeypatch.setattr(collector_mod, "MarketDashboardBuilder", BoomBuilder)

    ok = refresh_dashboard(tmp_path, provider=None, store=None)

    assert ok is False


# --------------------------------------------------------------------------- #
# SchedulerService.run_once
# --------------------------------------------------------------------------- #
def test_run_once_summary_with_injected_scan_fn() -> None:
    """注入假扫描函数：汇总 dict 正确（开关全关，聚焦 scan 步骤）。"""
    failures = [("BAD", "RuntimeError: scan boom")]

    def fake_scan(provider: Any, store: Any, symbols: list[str], day: Any):
        assert symbols == ["600600", "000001"]
        assert day == DAY
        return [_make_event("600600", 1), _make_event("600600", 2)], failures

    service = SchedulerService(
        settings=_settings(symbols=["600600", "000001"], push=False, refresh=False),
        scan_fn=fake_scan,
    )

    summary = service.run_once(DAY)

    assert summary["events"] == 2
    assert summary["pushed"] == 0
    assert summary["failures"] == failures
    assert summary["dashboard"] is False


def test_run_once_day_forwarded_to_scan_fn() -> None:
    seen: dict[str, Any] = {}

    def fake_scan(provider: Any, store: Any, symbols: list[str], day: Any):
        seen["day"] = day
        return [], []

    service = SchedulerService(
        settings=_settings(push=False, refresh=False), scan_fn=fake_scan
    )

    service.run_once(DAY)

    assert seen["day"] == DAY


def test_run_once_disabled_steps_not_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """push / refresh 开关关闭时对应步骤不执行。"""
    calls: list[str] = []
    monkeypatch.setattr(
        service_mod, "push_events", lambda events: calls.append("push") or []
    )
    monkeypatch.setattr(
        service_mod, "refresh_dashboard", lambda *a, **kw: calls.append("refresh") or True
    )

    def fake_scan(provider: Any, store: Any, symbols: list[str], day: Any):
        return [_make_event()], []

    service = SchedulerService(
        settings=_settings(symbols=["600600"], push=False, refresh=False),
        scan_fn=fake_scan,
    )

    summary = service.run_once(DAY)

    assert calls == []
    assert summary["pushed"] == 0
    assert summary["dashboard"] is False


def test_run_once_enabled_steps_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """push / refresh 开关开启时步骤执行且结果计入汇总。"""
    push_arg: list[Any] = []
    monkeypatch.setattr(
        service_mod,
        "push_events",
        lambda events: push_arg.extend(events)
        or [PushResult(channel="fake", ok=True), PushResult(channel="fake", ok=False)],
    )
    monkeypatch.setattr(
        service_mod, "refresh_dashboard", lambda *a, **kw: True
    )

    def fake_scan(provider: Any, store: Any, symbols: list[str], day: Any):
        return [_make_event()], []

    service = SchedulerService(
        settings=_settings(symbols=["600600"], push=True, refresh=True),
        scan_fn=fake_scan,
    )

    summary = service.run_once(DAY)

    assert len(push_arg) == 1  # 事件透传给推送
    assert summary["pushed"] == 1  # 仅统计 ok 的通道次
    assert summary["dashboard"] is True


def test_run_once_symbols_default_from_settings_watchlist() -> None:
    """symbols 未显式给出时，缺省取 settings.watchlist.symbols。"""
    seen: dict[str, Any] = {}

    def fake_scan(provider: Any, store: Any, symbols: list[str], day: Any):
        seen["symbols"] = symbols
        return [], []

    service = SchedulerService(
        settings=_settings(symbols=["600519", "300750"]),
        scan_fn=fake_scan,
    )

    assert service.symbols == ["600519", "300750"]
    service.run_once()
    assert seen["symbols"] == ["600519", "300750"]


def test_run_once_writes_dashboard_html(tmp_path: Path) -> None:
    """端到端（离线）：注入事件 + 关推送 + 开看板 → outputs/index.html 落盘。"""

    def fake_scan(provider: Any, store: Any, symbols: list[str], day: Any):
        return [_make_event()], []

    service = SchedulerService(
        settings=_settings(symbols=["600600"], push=False, refresh=True),
        scan_fn=fake_scan,
        out_dir=tmp_path / "outputs",
    )

    summary = service.run_once(DAY)

    assert summary["dashboard"] is True
    assert (tmp_path / "outputs" / "index.html").exists()


# --------------------------------------------------------------------------- #
# SchedulerService.start / stop（apscheduler 惰性依赖）
# --------------------------------------------------------------------------- #
def test_start_raises_runtime_error_without_apscheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未安装 apscheduler 时 start() 抛 RuntimeError，信息含安装提示。"""
    monkeypatch.setitem(sys.modules, "apscheduler", None)
    monkeypatch.setitem(sys.modules, "apscheduler.schedulers", None)
    monkeypatch.setitem(sys.modules, "apscheduler.triggers", None)

    service = SchedulerService(settings=_settings(), scan_fn=lambda *a: ([], []))

    with pytest.raises(RuntimeError, match="pip install apscheduler"):
        service.start()

    assert service._scheduler is None  # 未启动成功，无需 stop


def test_stop_without_start_is_noop() -> None:
    """未启动时 stop() 为空操作，不抛异常。"""
    service = SchedulerService(settings=_settings(), scan_fn=lambda *a: ([], []))
    service.stop()  # 不应抛
    assert service._scheduler is None


# --------------------------------------------------------------------------- #
# 配置：WatchlistSettings / ScheduleSettings
# --------------------------------------------------------------------------- #
def test_watchlist_settings_from_dict_defaults_and_cleaning() -> None:
    assert WatchlistSettings.from_dict({}).symbols == []
    # 非法 symbols（非列表）→ 安全回退空列表
    assert WatchlistSettings.from_dict({"symbols": "600519"}).symbols == []
    # 空白项被清洗
    cleaned = WatchlistSettings.from_dict({"symbols": ["600519", "  ", ""]})
    assert cleaned.symbols == ["600519"]


def test_watchlist_settings_is_iterable() -> None:
    """可像 list 一样迭代 / 取长度（兼容 AnomalyEngine.run_once 历史读取方式）。"""
    watchlist = WatchlistSettings(symbols=["600519", "000001"])
    assert list(watchlist) == ["600519", "000001"]
    assert len(watchlist) == 2


def test_schedule_settings_invalid_scan_time_falls_back() -> None:
    """scan_time 非法（格式错 / 越界）时回退默认 15:05，不抛异常。"""
    for bad in ("abc", "9:5", "25:00", "15:60", "", "15:05:00"):
        settings = ScheduleSettings.from_dict({"scan_time": bad})
        assert settings.scan_time == "15:05", f"{bad!r} 应回退默认值"


def test_schedule_settings_valid_time_normalized() -> None:
    settings = ScheduleSettings.from_dict({"scan_time": "9:05"})
    assert settings.scan_time == "09:05"
    settings = ScheduleSettings.from_dict({"scan_time": "23:59"})
    assert settings.scan_time == "23:59"


def test_schedule_settings_am_scan_time_handling() -> None:
    """am_scan_time：留空 / 缺省 → 空（不做早盘扫描）；非法 → 回退空。"""
    assert ScheduleSettings.from_dict({}).am_scan_time == ""
    assert ScheduleSettings.from_dict({"am_scan_time": ""}).am_scan_time == ""
    assert ScheduleSettings.from_dict({"am_scan_time": "09:25"}).am_scan_time == "09:25"
    assert ScheduleSettings.from_dict({"am_scan_time": "99:99"}).am_scan_time == ""


def test_schedule_settings_bool_flags_default_true() -> None:
    settings = ScheduleSettings.from_dict({})
    assert settings.enabled is True
    assert settings.push is True
    assert settings.refresh_dashboard is True
    settings = ScheduleSettings.from_dict({"enabled": False, "push": False})
    assert settings.enabled is False
    assert settings.push is False


def test_settings_load_merges_watchlist_and_schedule(tmp_path: Path) -> None:
    """Settings.load 合并读取 [watchlist] / [schedule] 段（含缺字段兼容）。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "\n".join(
            [
                "[watchlist]",
                'symbols = ["600519", "300750"]',
                "",
                "[schedule]",
                'scan_time = "15:10"',
                "push = false",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings.load(cfg)

    assert settings.watchlist.symbols == ["600519", "300750"]
    assert settings.schedule.scan_time == "15:10"
    assert settings.schedule.push is False
    assert settings.schedule.refresh_dashboard is True  # 缺字段 → 默认值
    assert settings.schedule.am_scan_time == ""


def test_settings_load_missing_sections_uses_defaults(tmp_path: Path) -> None:
    """配置缺 [watchlist] / [schedule] 段时全部取安全默认值。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[llm]\nmodel = "test-model"\n', encoding="utf-8")

    settings = Settings.load(cfg)

    assert settings.watchlist.symbols == []
    assert settings.schedule.enabled is True
    assert settings.schedule.scan_time == "15:05"
