"""回测复核（T26）。

用真实历史日线把线上同一套异动规则（``anomaly/rules.py`` 的规则类）回放一遍，
统计信号触发后的前向收益（T+1/T+3/T+5/T+10），与「任意日基线」对比，
回答「这套规则到底有没有判别力」。

模块：
- ``models``：SignalRecord / HorizonStats / BacktestResult
- ``data``：fetch_daily（akshare 东财主源 + 新浪回退）
- ``engine``：HistoricalProvider + BacktestEngine（离线回放核心）
- ``report``：自包含单文件 HTML 报告

诚实性纪律：需要盘中 / 板块数据的规则（板块资金流、板块排名、涨跌停池、
集合竞价）在日线回放中**跳过**并如实写进 ``skipped_rules``，严禁用日线数据假造。
"""

from ashare_prober.backtest.engine import (
    BacktestEngine,
    HistoricalProvider,
    run_backtest,
)
from ashare_prober.backtest.models import (
    BacktestResult,
    HorizonStats,
    SignalRecord,
)
from ashare_prober.backtest.report import render_backtest_html, write_report

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "HistoricalProvider",
    "HorizonStats",
    "SignalRecord",
    "render_backtest_html",
    "run_backtest",
    "write_report",
]
