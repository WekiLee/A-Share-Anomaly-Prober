"""CLI 单测（T25）。

注入假 Provider（返回触发 VolumeBreakRule 的 Quote），验证：
- ``run`` 产出 HTML 并打印 request id；
- ``approve --yes`` 置 approved 并重渲染。
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from ashare_prober.cli import app
from ashare_prober.data import Store as _RealStore
from ashare_prober.data.models import Quote

runner = CliRunner()


class FakeProvider:
    """仅实现必要接口，返回触发放量破位的样本 Quote。"""

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            name="异动股",
            pre_close=100.0,
            close=90.0,
            amount=2e9,
            date=None,
        )

    def get_news(self, symbol):
        return []

    def get_announcements(self, symbol, day):
        return []

    def get_dragon_tiger(self, symbol, start, end):
        return []

    def get_external(self, category):
        return []

    def get_money_flow(self, symbol):
        return None


def _patch(monkeypatch, tmp_path):
    """将 Provider 与落库重定向到临时环境，保证测试可隔离、可重复。"""
    monkeypatch.setattr(
        "ashare_prober.cli.build_default_provider", lambda: FakeProvider()
    )
    db_path = tmp_path / "prober.db"
    monkeypatch.setattr("ashare_prober.cli.Store", lambda: _RealStore(db_path))
    out_dir = tmp_path / "outputs"
    monkeypatch.setattr("ashare_prober.cli.DEFAULT_OUTPUT_DIR", out_dir)
    return db_path, out_dir


def test_run_produces_html_and_request_id(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    db_path, out_dir = _patch(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["run", "--symbol", "600600", "--date", "2026-10-09"]
    )
    assert result.exit_code == 0, result.output
    assert "请求 ID:" in result.output
    assert re.search(r"REQ-[0-9a-f]+", result.output) is not None
    assert "HTML 路径:" in result.output
    # HTML 文件确实写出
    assert list(out_dir.glob("*.html"))


def test_approve_yes_sets_approved_and_rerenders(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    db_path, out_dir = _patch(monkeypatch, tmp_path)
    run_res = runner.invoke(
        app, ["run", "--symbol", "600600", "--date", "2026-10-09"]
    )
    assert run_res.exit_code == 0, run_res.output
    match = re.search(r"REQ-[0-9a-f]+", run_res.output)
    assert match is not None
    req_id = match.group(0)

    appr_res = runner.invoke(
        app, ["approve", "--id", req_id, "--yes", "--approver", "tester"]
    )
    assert appr_res.exit_code == 0, appr_res.output
    assert "approved" in appr_res.output

    # 落地状态应为 approved
    store = _RealStore(db_path)
    got = store.get_approval(req_id)
    assert got is not None
    assert got.status == "approved"
