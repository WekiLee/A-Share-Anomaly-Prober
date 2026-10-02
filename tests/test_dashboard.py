"""大盘看板单测（全离线，不依赖网络 / akshare）。

覆盖：
- 模型：``MarketBreadth.up_ratio`` 除零保护、``MarketDashboard.total_amount`` 汇总口径。
- 采集器：正常路径 / 全量降级 / 部分失败 / 无 provider 四种情形。
- 门面：主备源双双失败时各 ``get_*`` 返回空值且不抛异常。
- 渲染：``Render.render_dashboard`` 产出 HTML 含红涨绿跌配色、落盘 ``index.html``、
  缺口非空时展示「数据缺口」。
"""

from __future__ import annotations

from datetime import date, datetime

from ashare_prober.config import DOWN_COLOR, UP_COLOR
from ashare_prober.data.base import SourceError
from ashare_prober.data.cache import Cache
from ashare_prober.data.models import ExternalQuote, LimitPoolItem
from ashare_prober.data.provider import Provider
from ashare_prober.dashboard.collector import (
    DEFAULT_INDEX_CODES,
    MarketDashboardBuilder,
)
from ashare_prober.dashboard.models import (
    IndexQuote,
    MarketBreadth,
    MarketDashboard,
    SectorFlow,
)
from ashare_prober.presentation.render import Render

DAY = date(2026, 10, 9)


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeStore:
    """最小化本地库替身；``events`` 为 ``list_events`` 的返回值。"""

    def __init__(self, events: list | None = None) -> None:
        self.events = events if events is not None else []

    def list_events(self, limit: int = 10) -> list:
        return list(self.events)[:limit]


class FakeProvider:
    """可整体失败或按项失败的假门面（结构对齐 ``Provider`` 的大盘接口）。"""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: dict[str, int] = {}

    def _maybe_fail(self, key: str) -> None:
        self.calls[key] = self.calls.get(key, 0) + 1
        if key in self.fail:
            raise RuntimeError(f"{key} boom")

    def get_index_quotes(self, codes) -> list[IndexQuote]:
        self._maybe_fail("index")
        return [
            IndexQuote(code=code, name=name, last=3000.0 + i, pct_change=0.5, amount=1e11)
            for i, (code, name) in enumerate(DEFAULT_INDEX_CODES.items())
            if code in set(codes)
        ]

    def get_market_breadth(self) -> MarketBreadth:
        self._maybe_fail("breadth")
        return MarketBreadth(
            up_count=2000, down_count=1500, flat_count=100,
            limit_up_count=40, limit_down_count=5, total=3600,
        )

    def get_sector_flow(self, top_n: int = 10) -> list[SectorFlow]:
        self._maybe_fail("sector")
        return [
            SectorFlow(name="半导体", pct_change=2.1, main_net_inflow=3.2e9, rank=1),
            SectorFlow(name="证券", pct_change=1.1, main_net_inflow=1.5e9, rank=2),
            SectorFlow(name="白酒", pct_change=-1.4, main_net_inflow=-2.0e9, rank=-1),
            SectorFlow(name="医药", pct_change=-0.8, main_net_inflow=-1.1e9, rank=-2),
        ]

    def get_limit_pool(self, day, kind: str = "up") -> list[LimitPoolItem]:
        self._maybe_fail(f"limit_{kind}")
        sign = 1.0 if kind == "up" else -1.0
        return [
            LimitPoolItem(
                stock_code="600600", name="测试A", price=10.0,
                pct_change=sign * 10.0, reason="题材驱动",
                consecutive_boards=2, date=day,
            )
        ]

    def get_external(self, category: str) -> list[ExternalQuote]:
        self._maybe_fail("external")
        return [
            ExternalQuote(
                name="恒生指数", category=category, last=18000.0,
                pct_change=0.8, timestamp=datetime(2026, 10, 9, 15, 0),
            )
        ]


class FakeSource:
    """假数据源：主备源均抛错，用于验证 Provider 的降级路径。"""

    name = "fake"

    def fetch_index_quotes(self, codes) -> list[IndexQuote]:
        raise SourceError("no index", source=self.name)

    def fetch_market_breadth(self) -> MarketBreadth:
        raise SourceError("no breadth", source=self.name)

    def fetch_sector_flow(self, top_n: int = 10) -> list[SectorFlow]:
        raise SourceError("no sector", source=self.name)

    def fetch_limit_pool(self, day, kind: str = "up") -> list[LimitPoolItem]:
        raise SourceError("no pool", source=self.name)


def _sample_event() -> dict:
    """近期异动样例（dict 形态，与采集器归一结果一致）。"""
    return {
        "id": "EVT-600600-20261009-001",
        "code": "600600",
        "name": "青岛啤酒",
        "type": "volume_break_up",
        "level": "alert",
        "detected_at": datetime(2026, 10, 9, 10, 30),
    }


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
def test_up_ratio_zero_total_returns_zero() -> None:
    breadth = MarketBreadth()
    assert breadth.total == 0
    assert breadth.up_ratio == 0.0


def test_up_ratio_computed_when_total_positive() -> None:
    breadth = MarketBreadth(up_count=25, down_count=75, total=100)
    assert breadth.up_ratio == 25.0


def test_total_amount_sums_shanghai_and_shenzhen() -> None:
    dashboard = MarketDashboard(
        date=DAY,
        indices=[
            IndexQuote(code="000001", name="上证指数", amount=1.0e11),
            IndexQuote(code="399001", name="深证成指", amount=2.0e11),
            IndexQuote(code="399006", name="创业板指", amount=5.0e10),  # 不计入两市合计
        ],
    )
    assert dashboard.total_amount == 3.0e11


def test_total_amount_zero_when_no_indices() -> None:
    assert MarketDashboard().total_amount == 0.0


# --------------------------------------------------------------------------- #
# 采集器：正常路径
# --------------------------------------------------------------------------- #
def test_builder_happy_path_fills_all_and_no_gaps() -> None:
    provider = FakeProvider()
    store = FakeStore(events=[_sample_event()])
    builder = MarketDashboardBuilder(provider=provider, store=store)

    dashboard = builder.build(day=DAY)

    assert len(dashboard.indices) == len(DEFAULT_INDEX_CODES)
    assert dashboard.breadth is not None
    assert dashboard.breadth.up_count == 2000
    assert [s.name for s in dashboard.sectors_top] == ["半导体", "证券"]
    assert [s.name for s in dashboard.sectors_bottom] == ["白酒", "医药"]
    assert len(dashboard.limit_up) == 1
    assert len(dashboard.limit_down) == 1
    assert len(dashboard.external) == 2  # 港股指数 + 美股指数
    assert len(dashboard.recent_events) == 1
    assert dashboard.gaps == []


def test_builder_uses_date_when_day_is_none() -> None:
    dashboard = MarketDashboardBuilder(provider=FakeProvider(), store=FakeStore()).build()
    assert dashboard.date is not None
    assert dashboard.gaps == []


# --------------------------------------------------------------------------- #
# 采集器：降级路径（核心纪律）
# --------------------------------------------------------------------------- #
def test_builder_degrades_when_every_item_fails() -> None:
    provider = FakeProvider(
        fail={"index", "breadth", "sector", "limit_up", "limit_down", "external"}
    )
    builder = MarketDashboardBuilder(provider=provider)

    dashboard = builder.build(day=DAY)  # 不应抛异常

    assert dashboard.indices == []
    assert dashboard.breadth is None
    assert dashboard.sectors_top == []
    assert dashboard.sectors_bottom == []
    assert dashboard.limit_up == []
    assert dashboard.limit_down == []
    assert dashboard.external == []
    assert dashboard.gaps, "全部失败时必须如实记录数据缺口"
    assert any("指数行情" in g for g in dashboard.gaps)
    assert any("板块资金流" in g for g in dashboard.gaps)
    assert any("涨停池" in g for g in dashboard.gaps)


def test_builder_partial_failure_keeps_other_items() -> None:
    provider = FakeProvider(fail={"sector"})
    store = FakeStore(events=[_sample_event()])
    builder = MarketDashboardBuilder(provider=provider, store=store)

    dashboard = builder.build(day=DAY)

    assert dashboard.sectors_top == []
    assert dashboard.sectors_bottom == []
    assert len(dashboard.indices) == len(DEFAULT_INDEX_CODES)  # 其它项不受影响
    assert dashboard.breadth is not None
    assert len(dashboard.limit_up) == 1
    assert any("板块资金流" in g for g in dashboard.gaps)


def test_builder_without_provider_never_crashes() -> None:
    dashboard = MarketDashboardBuilder(provider=None).build(day=DAY)

    assert isinstance(dashboard, MarketDashboard)
    assert dashboard.indices == []
    assert dashboard.recent_events == []
    assert dashboard.gaps, "无数据源时必须记录缺口"


def test_builder_without_store_records_missing_events() -> None:
    dashboard = MarketDashboardBuilder(provider=FakeProvider(), store=None).build(day=DAY)
    assert dashboard.recent_events == []
    assert any("近期异动" in g for g in dashboard.gaps)


# --------------------------------------------------------------------------- #
# Provider：主备源皆失败时降级为空
# --------------------------------------------------------------------------- #
def test_provider_dashboard_methods_degrade_to_empty(tmp_path) -> None:  # noqa: ANN001
    cache = Cache(tmp_path / "dash.cache.db")
    provider = Provider(FakeSource(), FakeSource(), cache, FakeStore())

    assert provider.get_index_quotes(["000001"]) == []
    assert provider.get_market_breadth() is None
    assert provider.get_sector_flow(5) == []
    assert provider.get_limit_pool(DAY, "up") == []


def test_builder_with_real_provider_degrades_without_raising(tmp_path) -> None:  # noqa: ANN001
    cache = Cache(tmp_path / "dash.cache.db")
    provider = Provider(FakeSource(), FakeSource(), cache, FakeStore())
    dashboard = MarketDashboardBuilder(provider=provider).build(day=DAY)

    assert dashboard.indices == []
    assert dashboard.gaps, "真实 Provider 全源失败时看板应记录缺口而非崩溃"


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def test_render_dashboard_contains_up_and_down_colors(tmp_path) -> None:  # noqa: ANN001
    dashboard = MarketDashboard(
        date=DAY,
        indices=[IndexQuote(code="000001", name="上证指数", last=3100.0,
                            pct_change=1.2, amount=4.5e11)],
        breadth=MarketBreadth(up_count=2000, down_count=1500, flat_count=100,
                              limit_up_count=40, limit_down_count=5, total=3600),
        sectors_top=[SectorFlow(name="半导体", pct_change=2.1, main_net_inflow=3.2e9, rank=1)],
        sectors_bottom=[SectorFlow(name="白酒", pct_change=-1.4, main_net_inflow=-2.0e9, rank=-1)],
        limit_up=[LimitPoolItem(stock_code="600600", name="测试A", price=10.0,
                                pct_change=10.0, reason="题材", consecutive_boards=2)],
        external=[ExternalQuote(name="恒生指数", category="港股指数", last=18000.0, pct_change=0.8)],
        recent_events=[_sample_event()],
    )

    html = Render().render_dashboard(dashboard, out_path=tmp_path)

    assert UP_COLOR in html
    assert DOWN_COLOR in html
    assert "上证指数" in html
    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == html


def test_render_dashboard_shows_gap_section_when_gaps_present() -> None:
    dashboard = MarketDashboard(date=DAY, gaps=["板块资金流获取失败：boom"])
    html = Render().render_dashboard(dashboard)

    assert "数据缺口" in html
    assert "板块资金流获取失败" in html


def test_render_dashboard_hides_gap_section_when_no_gaps() -> None:
    html = Render().render_dashboard(MarketDashboard(date=DAY))
    assert "数据缺口" not in html
    assert "今日暂无异动" in html


def test_render_dashboard_custom_filename(tmp_path) -> None:  # noqa: ANN001
    Render().render_dashboard(MarketDashboard(date=DAY), out_path=tmp_path, filename="dash.html")
    assert (tmp_path / "dash.html").exists()
    assert not (tmp_path / "index.html").exists()
