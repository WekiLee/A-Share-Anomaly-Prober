"""监控名单 API / 看板监控列表卡片测试（全离线）。

覆盖：
- ``viewer``：``start_viewer_thread`` 起真服务（127.0.0.1、port=0 系统分配），
  逐个验证 watchlist API 的状态码与响应形状；静态文件路由不受影响。
- ``collector``：``_collect_watchlist`` 的降级纪律（store 缺失 / 异常 / 空名单不计 gap）。
- 模板：渲染结果含「监控列表」卡片、API 路径与静态模式提示文案。

数据层 ``Store`` 的 watchlist 方法由并行开发提供：本文件主流程使用**契约忠实**的
替身存储（FakeWatchStore）；另附真实 ``Store`` 落库集成用例，在契约方法缺失时
自动 skip（并行代码合入后即生效），保证任何阶段 0 failed。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

import pytest

from ashare_prober.dashboard.collector import MarketDashboardBuilder
from ashare_prober.dashboard.models import MarketDashboard
from ashare_prober.data.base import SourceError
from ashare_prober.data.symbols import SymbolInfo, SymbolNotFound
from ashare_prober.presentation.render import Render
from ashare_prober.presentation.viewer import start_viewer_thread


# --------------------------------------------------------------------------- #
# 测试替身：解析器 / 本地库（契约忠实）
# --------------------------------------------------------------------------- #
class FakeResolver:
    """替身解析器：``resolve`` 按固定映射返回 ``SymbolInfo`` 或抛异常。"""

    def resolve(self, query: str) -> SymbolInfo:
        token = str(query or "").strip()
        if token in ("茅台", "600519", "gzmt"):
            return SymbolInfo(code="600519", name="贵州茅台")
        if token == "银行":  # 歧义：多候选
            raise SymbolNotFound(
                "未找到匹配标的: 银行；你是否要找：…",
                candidates=[
                    SymbolInfo(code="601988", name="中国银行"),
                    SymbolInfo(code="600036", name="招商银行"),
                    SymbolInfo(code="601166", name="兴业银行"),
                ],
            )
        if token == "歧义十条":  # 候选超过 8 条，验证截断
            raise SymbolNotFound(
                "未找到匹配标的: 歧义十条",
                candidates=[
                    SymbolInfo(code=f"60010{i}", name=f"候选{i}")
                    for i in range(10)
                ],
            )
        if token == "不存在":  # 查无候选
            raise SymbolNotFound("未找到匹配标的: 不存在")
        if token == "sourcefail":  # 标的表不可用
            raise SourceError("标的表加载失败", source="symbols")
        if token == "非法":  # 解析成功但代码非法（触发落库 ValueError）
            return SymbolInfo(code="abc", name="非法标的")
        # 兜底：6 位数字按原代码，其余给固定代码
        code = token if (token.isdigit() and len(token) == 6) else "600000"
        return SymbolInfo(code=code, name="默认标的")

    def search(self, query: str, limit: int = 10) -> list[SymbolInfo]:
        return []


class FakeWatchStore:
    """契约忠实的监控名单替身存储（与 data 层契约签名一致）。"""

    _FIXED_ADDED_AT = "2024-01-01 09:30:00"

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def add_watchlist(self, code: str, name: str = "", active: bool = True) -> bool:
        code = str(code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            raise ValueError(f"非法代码: {code}")
        with self._lock:
            is_new = code not in self._rows
            self._rows[code] = {
                "code": code,
                "name": str(name or ""),
                "added_at": self._FIXED_ADDED_AT,
                "active": bool(active),
            }
        return is_new

    def remove_watchlist(self, code: str) -> bool:
        code = str(code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            raise ValueError(f"非法代码: {code}")
        with self._lock:
            return self._rows.pop(code, None) is not None

    def set_watchlist_active(self, code: str, active: bool) -> bool:
        code = str(code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            raise ValueError(f"非法代码: {code}")
        with self._lock:
            row = self._rows.get(code)
            if row is None:
                return False
            row["active"] = bool(active)
            return True

    def list_watchlist(self, active_only: bool = False) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows.values()]
        return [r for r in rows if (r["active"] or not active_only)]


# --------------------------------------------------------------------------- #
# HTTP 助手
# --------------------------------------------------------------------------- #
def _get(base: str, path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post(base: str, path: str, payload: Optional[dict]) -> tuple[int, Any]:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    return _post_raw(req)


def _post_raw(req: urllib.request.Request) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_bad_json(base: str, path: str) -> tuple[int, Any]:
    req = urllib.request.Request(
        base + path, data=b"{not-json", headers={"Content-Type": "application/json"}, method="POST"
    )
    return _post_raw(req)


# --------------------------------------------------------------------------- #
# 服务 fixture
# --------------------------------------------------------------------------- #
@pytest.fixture()
def viewer(tmp_path):
    """起真服务（port=0 系统分配端口），返回命名空间（base/store/resolver）。"""
    store = FakeWatchStore()
    resolver = FakeResolver()
    httpd, thread = start_viewer_thread(tmp_path, port=0, store=store, resolver=resolver)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield type("ViewerCtx", (), {"base": base, "store": store, "resolver": resolver})()
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# API 用例
# --------------------------------------------------------------------------- #
class TestWatchlistAPI:
    """watchlist HTTP API：状态码与响应形状。"""

    def test_get_initial_empty(self, viewer) -> None:
        status, body = _get(viewer.base, "/api/watchlist")
        assert status == 200
        assert body == {"ok": True, "items": []}

    def test_add_resolves_and_persists(self, viewer) -> None:
        status, body = _post(viewer.base, "/api/watchlist/add", {"query": "茅台"})
        assert status == 200
        assert body == {"ok": True, "code": "600519", "name": "贵州茅台"}
        rows = viewer.store.list_watchlist()
        assert len(rows) == 1
        assert rows[0]["code"] == "600519"
        assert rows[0]["name"] == "贵州茅台"
        assert rows[0]["active"] is True

    def test_add_duplicate_returns_ok(self, viewer) -> None:
        assert _post(viewer.base, "/api/watchlist/add", {"query": "600519"})[0] == 200
        status, body = _post(viewer.base, "/api/watchlist/add", {"query": "gzmt"})
        assert status == 200
        assert body["ok"] is True
        # 更新而非新增：仍只有一行
        assert len(viewer.store.list_watchlist()) == 1

    def test_add_ambiguous_returns_409_with_candidates(self, viewer) -> None:
        status, body = _post(viewer.base, "/api/watchlist/add", {"query": "银行"})
        assert status == 409
        assert body["ok"] is False
        assert body["error"] == "ambiguous"
        codes = [c["code"] for c in body["candidates"]]
        assert "601988" in codes and "600036" in codes
        # 落库绝不发生
        assert viewer.store.list_watchlist() == []

    def test_add_candidates_capped_at_8(self, viewer) -> None:
        _, body = _post(viewer.base, "/api/watchlist/add", {"query": "歧义十条"})
        assert 1 <= len(body["candidates"]) <= 8

    def test_add_not_found_returns_404(self, viewer) -> None:
        status, body = _post(viewer.base, "/api/watchlist/add", {"query": "不存在"})
        assert status == 404
        assert body["ok"] is False
        assert "candidates" not in body

    def test_add_resolver_source_error_returns_404(self, viewer) -> None:
        status, body = _post(viewer.base, "/api/watchlist/add", {"query": "sourcefail"})
        assert status == 404
        assert body["ok"] is False

    def test_add_store_value_error_returns_400(self, viewer) -> None:
        status, body = _post(viewer.base, "/api/watchlist/add", {"query": "非法"})
        assert status == 400
        assert body["ok"] is False
        assert viewer.store.list_watchlist() == []

    def test_add_empty_query_returns_400(self, viewer) -> None:
        status, _ = _post(viewer.base, "/api/watchlist/add", {"query": ""})
        assert status == 400

    def test_add_bad_json_returns_400(self, viewer) -> None:
        status, body = _post_bad_json(viewer.base, "/api/watchlist/add")
        assert status == 400
        assert body["ok"] is False

    def test_remove_then_get(self, viewer) -> None:
        _post(viewer.base, "/api/watchlist/add", {"query": "600519"})
        _post(viewer.base, "/api/watchlist/add", {"query": "000001"})
        status, body = _post(viewer.base, "/api/watchlist/remove", {"code": "000001"})
        assert status == 200 and body == {"ok": True}
        _, listing = _get(viewer.base, "/api/watchlist")
        codes = [it["code"] for it in listing["items"]]
        assert codes == ["600519"]
        # 再删一次 → 404
        status, body = _post(viewer.base, "/api/watchlist/remove", {"code": "000001"})
        assert status == 404 and body["ok"] is False

    def test_remove_invalid_code_returns_400(self, viewer) -> None:
        status, body = _post(viewer.base, "/api/watchlist/remove", {"code": "abc"})
        assert status == 400
        assert body["ok"] is False

    def test_active_toggle_visible_in_get(self, viewer) -> None:
        _post(viewer.base, "/api/watchlist/add", {"query": "600519"})
        status, body = _post(
            viewer.base, "/api/watchlist/active", {"code": "600519", "active": False}
        )
        assert status == 200 and body == {"ok": True}
        _, listing = _get(viewer.base, "/api/watchlist")
        assert listing["items"][0]["active"] is False
        # 恢复
        status, _ = _post(
            viewer.base, "/api/watchlist/active", {"code": "600519", "active": True}
        )
        assert status == 200
        _, listing = _get(viewer.base, "/api/watchlist")
        assert listing["items"][0]["active"] is True
        # 不存在的代码 → 404
        status, _ = _post(
            viewer.base, "/api/watchlist/active", {"code": "999999", "active": False}
        )
        assert status == 404

    def test_active_non_bool_returns_400(self, viewer) -> None:
        _post(viewer.base, "/api/watchlist/add", {"query": "600519"})
        status, _ = _post(
            viewer.base, "/api/watchlist/active", {"code": "600519", "active": "yes"}
        )
        assert status == 400

    def test_unknown_api_post_returns_404_json(self, viewer) -> None:
        status, body = _post(viewer.base, "/api/other", {"x": 1})
        assert status == 404
        assert body["ok"] is False

    def test_static_files_still_served(self, viewer, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<html>hello-prober</html>", encoding="utf-8")
        # 静态路径返回目录内容（非 JSON，不走 _get 助手）
        with urllib.request.urlopen(viewer.base + "/", timeout=5) as resp:
            assert resp.status == 200
            assert b"hello-prober" in resp.read()
        # 不存在的静态路径 → 404（不 500）
        try:
            with urllib.request.urlopen(viewer.base + "/nope.html", timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        assert status == 404

    def test_json_content_type(self, viewer) -> None:
        req = urllib.request.Request(viewer.base + "/api/watchlist")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.headers.get("Content-Type") == "application/json; charset=utf-8"


# --------------------------------------------------------------------------- #
# 真实 Store 集成（数据层并行开发：契约方法缺失时自动 skip）
# --------------------------------------------------------------------------- #
class TestRealStoreIntegration:
    """用真实 tmp ``Store`` 验证落库（契约方法就绪后自动生效）。"""

    def test_add_persists_to_real_store(self, tmp_path) -> None:
        from ashare_prober.data.store import Store

        if not hasattr(Store, "add_watchlist"):
            pytest.skip("数据层 watchlist 契约尚未落地（并行开发中）")
        store = Store(tmp_path / "watch.db")
        httpd, thread = start_viewer_thread(
            tmp_path / "out", port=0, store=store, resolver=FakeResolver()
        )
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            status, body = _post(base, "/api/watchlist/add", {"query": "茅台"})
            assert status == 200
            rows = store.list_watchlist()
            assert any(r["code"] == "600519" and r["name"] == "贵州茅台" for r in rows)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# collector：_collect_watchlist 降级纪律
# --------------------------------------------------------------------------- #
class _NullProvider:
    """全部返回空的替身数据门面（保证 build() 各项走完）。"""

    def get_index_quotes(self, codes):  # noqa: ANN001, ANN202 - 测试替身
        return []

    def get_market_breadth(self):  # noqa: ANN201
        return None

    def get_sector_flow(self, top_n):  # noqa: ANN001, ANN202
        return []

    def get_limit_pool(self, day, kind):  # noqa: ANN001, ANN202
        return []

    def get_external(self, category):  # noqa: ANN001, ANN202
        return []


class TestCollectorWatchlist:
    """看板采集器的监控名单采集与缺口纪律。"""

    def test_store_none_watchlist_empty_and_gap(self) -> None:
        dash = MarketDashboardBuilder(provider=_NullProvider(), store=None).build()
        assert dash.watchlist == []
        assert any("监控列表" in g for g in dash.gaps)

    def test_store_none_with_provider_none_also_records_gap(self) -> None:
        dash = MarketDashboardBuilder(provider=None, store=None).build()
        assert dash.watchlist == []
        assert any("监控列表" in g for g in dash.gaps)

    def test_watchlist_filled_no_gap(self) -> None:
        store = FakeWatchStore()
        store.add_watchlist("600519", "贵州茅台")
        store.add_watchlist("000001", "平安银行", active=False)
        dash = MarketDashboardBuilder(provider=_NullProvider(), store=store).build()
        assert {w["code"] for w in dash.watchlist} == {"600519", "000001"}
        by_code = {w["code"]: w for w in dash.watchlist}
        assert by_code["600519"]["active"] is True
        assert by_code["000001"]["active"] is False
        assert by_code["600519"]["name"] == "贵州茅台"
        assert not any("监控列表" in g for g in dash.gaps)

    def test_empty_watchlist_is_not_gap(self) -> None:
        dash = MarketDashboardBuilder(provider=_NullProvider(), store=FakeWatchStore()).build()
        assert dash.watchlist == []
        assert not any("监控列表" in g for g in dash.gaps)

    def test_store_error_records_gap(self) -> None:
        class BadStore:
            def list_watchlist(self) -> list[dict]:
                raise RuntimeError("db locked")

        dash = MarketDashboardBuilder(provider=_NullProvider(), store=BadStore()).build()
        assert dash.watchlist == []
        assert any("监控列表" in g for g in dash.gaps)

    def test_marketdashboard_watchlist_field_default(self) -> None:
        dash = MarketDashboard()
        assert dash.watchlist == []


# --------------------------------------------------------------------------- #
# 模板：监控列表卡片
# --------------------------------------------------------------------------- #
class TestDashboardTemplate:
    """看板模板渲染结果包含监控列表卡片要素。"""

    def _render(self, watchlist: list[dict]) -> str:
        return Render().render_dashboard(MarketDashboard(watchlist=watchlist))

    def test_contains_card_and_api_path(self) -> None:
        html = self._render([{"code": "600519", "name": "贵州茅台", "active": True}])
        assert "监控列表" in html
        assert "/api/watchlist" in html
        assert "静态模式" in html
        assert "600519" in html
        assert "贵州茅台" in html

    def test_static_row_renders_active_and_paused(self) -> None:
        html = self._render(
            [
                {"code": "600519", "name": "贵州茅台", "active": True},
                {"code": "000001", "name": "平安银行", "active": False},
            ]
        )
        assert "监控中" in html
        assert "已暂停" in html

    def test_empty_watchlist_shows_hint(self) -> None:
        html = self._render([])
        assert "名单为空" in html
