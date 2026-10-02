"""回测复核测试（T26）——全部离线（合成 bars 注入）。

合成数据设计原则：严格按 ``VolumeBreakRule`` 真实阈值构造
（涨跌幅绝对值 ≥ 3.0，成交额 ≥ 1.5 × 前 6 日均额），确保信号**确定触发**；
前向收益手工可算对照；尾部样本不足用短 bars 触发 censored。
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from ashare_prober.backtest import (
    BacktestEngine,
    BacktestResult,
    render_backtest_html,
    run_backtest,
    write_report,
)
from ashare_prober.backtest.data import _market_of
from ashare_prober.data.models import Quote

# --------------------------------------------------------------------------- #
# 合成 bars 工具
# --------------------------------------------------------------------------- #
DAY0 = date(2026, 1, 5)
QUIET_AMOUNT = 100_000_000.0  # 安静期成交额 1 亿
SPIKE_AMOUNT = 200_000_000.0  # 放量日成交额 2 亿（≥ 1.5 × 1 亿 基线，确定触发）


def _make_bars(
    closes: list[float], amounts: list[float], symbol: str = "600600"
) -> list[Quote]:
    """按收盘价/成交额序列构造日线 bars（pre_close 逐日回填，与 fetch_daily 同口径）。"""
    assert len(closes) == len(amounts)
    bars: list[Quote] = []
    pre = closes[0]
    for i, (close, amount) in enumerate(zip(closes, amounts)):
        pct = (close - pre) / pre * 100.0 if pre > 0 else 0.0
        bars.append(
            Quote(
                symbol=symbol,
                name="回测测试股",
                date=DAY0 + timedelta(days=i),
                open=pre,
                high=max(close, pre) * 1.01,
                low=min(close, pre) * 0.99,
                close=close,
                pre_close=pre,
                volume=amount / 100.0,
                amount=amount,
                pct_change=pct,
            )
        )
        pre = close
    return bars


def _up_design() -> list[Quote]:
    """连续缩量后放量上涨（17 根）：仅 t=6 触发 volume_break_up。

    t=6：close 10.4 / pre_close 10.0（+4.0% ≥ 3.0），amount 2e8 ≥ 1.5 × 1e8 基线。
    其后各日涨跌幅均 < 3%，不再触发。
    """
    closes = [10.0] * 6 + [10.4] + [10.5, 10.3, 10.4, 10.6, 10.9, 11.0, 11.1, 11.2, 11.3, 11.4]
    amounts = [QUIET_AMOUNT] * 6 + [SPIKE_AMOUNT] + [120_000_000.0] * 10
    return _make_bars(closes, amounts)


def _down_design() -> list[Quote]:
    """放量下跌（17 根）：仅 t=6 触发 volume_break_down（-5.0%）。"""
    closes = [10.0] * 6 + [9.5] + [9.4, 9.3, 9.2, 9.1, 9.0, 8.9, 8.8, 8.7, 8.6, 8.5]
    amounts = [QUIET_AMOUNT] * 6 + [SPIKE_AMOUNT] + [120_000_000.0] * 10
    return _make_bars(closes, amounts)


# --------------------------------------------------------------------------- #
# 信号识别
# --------------------------------------------------------------------------- #
def test_volume_break_up_signal_detected() -> None:
    """放量上涨序列 → 真实规则识别出 volume_break_up 信号。"""
    result = BacktestEngine(lookback=6).run("600600", bars=_up_design())
    assert result.n_signals == 1
    signal = result.signals[0]
    assert signal.rule == "volume_break_up"
    assert signal.direction == "up"
    assert signal.day == DAY0 + timedelta(days=6)
    assert signal.close == pytest.approx(10.4)
    # baseline_amount = 前 6 日均成交额（引擎同口径）
    assert signal.baseline_amount == pytest.approx(QUIET_AMOUNT)


def test_forward_returns_manual_reference() -> None:
    """前向收益手工对照：close[t+k]/close[t]-1。"""
    result = run_backtest("600600", bars=_up_design())
    forward = result.signals[0].forward
    assert forward["T+1"] == pytest.approx(10.5 / 10.4 - 1.0)
    assert forward["T+3"] == pytest.approx(10.4 / 10.4 - 1.0)  # bars[9]=10.4 → 0
    assert forward["T+5"] == pytest.approx(10.9 / 10.4 - 1.0)
    assert forward["T+10"] == pytest.approx(11.4 / 10.4 - 1.0)
    assert result.signals[0].censored is False
    assert result.n_censored == 0


def test_volume_break_down_signal() -> None:
    """放量下跌序列 → volume_break_down，前向收益为负。"""
    result = run_backtest("600600", bars=_down_design())
    assert result.n_signals == 1
    signal = result.signals[0]
    assert signal.rule == "volume_break_down"
    assert signal.direction == "down"
    assert signal.forward["T+1"] == pytest.approx(9.4 / 9.5 - 1.0)
    assert signal.forward["T+1"] < 0
    assert result.baseline_hit_rate == pytest.approx(0.0)  # 全程下跌，次日从未上涨


# --------------------------------------------------------------------------- #
# 截尾（诚实性：不冒充）
# --------------------------------------------------------------------------- #
def test_censored_tail_not_faked() -> None:
    """尾部样本不足 → censored=True，缺数据的 horizon 不写入 forward。"""
    closes = [10.0] * 6 + [10.4, 10.5, 10.6]  # 仅 9 根，t=6 触发后只剩 2 根
    amounts = [QUIET_AMOUNT] * 6 + [SPIKE_AMOUNT, 120_000_000.0, 120_000_000.0]
    result = run_backtest("600600", bars=_make_bars(closes, amounts))

    assert result.n_signals == 1
    assert result.n_censored == 1
    signal = result.signals[0]
    assert signal.censored is True
    # 仅 T+1 可算；T+3/T+5/T+10 数据不足，不得冒充
    assert set(signal.forward) == {"T+1"}
    assert signal.forward["T+1"] == pytest.approx(10.5 / 10.4 - 1.0)
    # 统计只计入可算样本
    assert result.per_horizon["T+1"].n == 1
    assert result.per_horizon["T+3"].n == 0
    assert result.per_horizon["T+10"].n == 0


# --------------------------------------------------------------------------- #
# 基线与汇总
# --------------------------------------------------------------------------- #
def test_baseline_hit_rate() -> None:
    """基线胜率 = 回放日中「次日收盘>当日收盘」占比（手工对照 9/10）。"""
    result = run_backtest("600600", bars=_up_design())
    # t=6..15 共 10 个有次日可对照的回放日，其中 9 日次日上涨（t=7 除外）
    assert result.n_days == 11  # t=6..16 全部回放
    assert result.baseline_hit_rate == pytest.approx(0.9)
    assert result.start == DAY0 + timedelta(days=6)
    assert result.end == DAY0 + timedelta(days=16)


def test_per_rule_counts() -> None:
    """分规则触发次数按信号类型统计。"""
    result = run_backtest("600600", bars=_up_design())
    assert result.per_rule == {"volume_break_up": 1}
    down = run_backtest("600600", bars=_down_design())
    assert down.per_rule == {"volume_break_down": 1}


def test_too_short_bars_empty_with_gap() -> None:
    """bars 少于 lookback+2 → 空结果 + gaps 说明，严禁硬凑。"""
    closes = [10.0] * 5
    amounts = [QUIET_AMOUNT] * 5
    result = run_backtest("600600", bars=_make_bars(closes, amounts))
    assert isinstance(result, BacktestResult)
    assert result.n_signals == 0
    assert result.n_days == 0
    assert result.signals == []
    assert result.gaps and "样本不足" in result.gaps[0]


def test_run_requires_dates_when_fetching() -> None:
    """bars=None 且缺 start/end → 明确报错（不静默联网）。"""
    with pytest.raises(ValueError):
        BacktestEngine().run("600600")


def test_skipped_rules_honest() -> None:
    """skipped_rules 非空且逐条写明「日线回放无法提供」类原因。"""
    result = run_backtest("600600", bars=_up_design())
    assert len(result.skipped_rules) == 4
    for entry in result.skipped_rules:
        assert "：" in entry
        assert "无法提供" in entry
    assert any(entry.startswith("sector_flow_flip") for entry in result.skipped_rules)
    assert any(entry.startswith("sector_rank_jump") for entry in result.skipped_rules)


def test_market_of_prefix_inference() -> None:
    """交易所前缀推断：60/68→sh，0/3→sz，4/8→bj；未知前缀报错。"""
    assert _market_of("600519") == "sh"
    assert _market_of("688981") == "sh"
    assert _market_of("sh600519") == "sh"
    assert _market_of("000001") == "sz"
    assert _market_of("300750") == "sz"
    assert _market_of("sz000001") == "sz"
    assert _market_of("430047") == "bj"
    assert _market_of("832566") == "bj"
    assert _market_of("900901") == "sh"  # B 股归 sh
    with pytest.raises(ValueError):
        _market_of("123456")  # 可转债等非股票前缀，不猜


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #
def test_html_report_colors_no_cdn_disclaimer(tmp_path) -> None:
    """报告：含红涨/跌绿配色、无外链 CDN、含免责声明、文件成功写出。"""
    result = run_backtest("600600", bars=_up_design())
    html = render_backtest_html(result)

    assert "#d70015" in html  # 红涨（取自 config.UP_COLOR）
    assert "#008a3e" in html  # 跌绿（取自 config.DOWN_COLOR）
    assert "不构成投资建议" in html
    assert "不代表未来表现" in html
    assert "http://" not in html and "https://" not in html  # 无任何外链
    assert "volume_break_up" in html
    assert "截尾" not in html or result.n_censored == 0  # 无截尾时不出现截尾标记

    path = write_report(result, tmp_path)
    assert path.exists()
    assert path.name.startswith("BT-600600-")
    assert path.name.endswith(".html")
    content = path.read_text(encoding="utf-8")
    assert "#d70015" in content and "不构成投资建议" in content


def test_html_report_censored_badge(tmp_path) -> None:
    """存在截尾信号时，报告中如实标注「截尾」。"""
    closes = [10.0] * 6 + [10.4, 10.5, 10.6]
    amounts = [QUIET_AMOUNT] * 6 + [SPIKE_AMOUNT, 120_000_000.0, 120_000_000.0]
    result = run_backtest("600600", bars=_make_bars(closes, amounts))
    assert result.n_censored == 1
    html = render_backtest_html(result)
    assert "截尾" in html


# --------------------------------------------------------------------------- #
# 网络冒烟（默认 skip，与 test_akshare_source_smoke 同守卫）
# --------------------------------------------------------------------------- #
@pytest.mark.network
def test_backtest_real_akshare_smoke() -> None:
    """真数据冒烟：600519 近 120 日跑通 run() 不抛（需 RUN_NETWORK_TESTS=1）。"""
    if os.environ.get("RUN_NETWORK_TESTS") != "1":
        pytest.skip("默认跳过网络冒烟测试；设置 RUN_NETWORK_TESTS=1 并联网后运行")

    end = date.today()
    start = end - timedelta(days=180)
    result = run_backtest("600519", start=start, end=end)
    assert result.symbol == "600519"
    assert result.n_days >= 0  # 不抛即通过；信号数不预设
