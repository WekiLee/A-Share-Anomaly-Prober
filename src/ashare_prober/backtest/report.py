"""回测报告（T26）：自包含单文件 HTML。

- 禁止任何外链 CDN（样式/脚本全部内联）。
- 移动端友好：``max-width: 760px``，苹果风卡片。
- 红涨 ``#d70015`` / 跌绿 ``#008a3e`` 从 ``config`` 常量取（不硬编码色值）。
- 横向对比条用纯 CSS 实现。
"""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

from ashare_prober.backtest.models import BacktestResult, SignalRecord
from ashare_prober.config import DOWN_COLOR, NEUTRAL_COLOR, UP_COLOR

__all__ = ["render_backtest_html", "write_report"]

_DISCLAIMER = (
    "本工具仅辅助研判，不构成投资建议，不荐股、不自动交易。"
    "回测结果基于历史数据，不代表未来表现。"
)


def _esc(value: object) -> str:
    """HTML 转义。"""
    return html.escape(str(value), quote=True)


def _pct(value: float) -> str:
    """小数收益率 → 百分比字符串。"""
    return f"{value * 100:.2f}%"


def _bar_row(label: str, ratio: float, color: str) -> str:
    """一行横向 CSS 条形（ratio 为 0~1 占比）。"""
    width = max(0.0, min(1.0, ratio)) * 100.0
    return (
        f'<div class="bar-row"><span class="bar-label">{_esc(label)}</span>'
        f'<span class="bar-track"><span class="bar-fill" '
        f'style="width:{width:.1f}%;background:{color}"></span></span>'
        f'<span class="bar-val">{_pct(ratio)}</span></div>'
    )


def _direction_cell(signal: SignalRecord) -> str:
    """方向单元格：up 红 / down 绿。"""
    if signal.direction == "up":
        return f'<span class="up">涨 ▲</span>'
    if signal.direction == "down":
        return f'<span class="down">跌 ▼</span>'
    return _esc(signal.direction)


def _signal_rows(result: BacktestResult) -> str:
    """信号明细表行。"""
    if not result.signals:
        return (
            '<tr><td colspan="7" class="muted" style="text-align:center">'
            "回放窗口内无信号触发</td></tr>"
        )
    rows: list[str] = []
    for s in result.signals:
        t1 = _pct(s.forward["T+1"]) if "T+1" in s.forward else "—"
        t5 = _pct(s.forward["T+5"]) if "T+5" in s.forward else "—"
        t1_cls = "up" if s.forward.get("T+1", 0.0) > 0 else "down"
        t5_cls = "up" if s.forward.get("T+5", 0.0) > 0 else "down"
        censor = '<span class="badge">截尾</span>' if s.censored else ""
        rows.append(
            f"<tr><td>{s.day.strftime('%Y-%m-%d')}</td>"
            f"<td>{_esc(s.rule)}</td>"
            f"<td>{_direction_cell(s)}</td>"
            f"<td>{s.close:.2f}</td>"
            f'<td class="{t1_cls}">{t1}</td>'
            f'<td class="{t5_cls}">{t5}</td>'
            f"<td>{censor}</td></tr>"
        )
    return "".join(rows)


def _horizon_cards(result: BacktestResult) -> str:
    """各 horizon 的 胜率·均值·中位数 vs 基线胜率 对比卡。"""
    cards: list[str] = []
    for name, stats in result.per_horizon.items():
        if stats.n > 0:
            if stats.hit_rate > result.baseline_hit_rate:
                signal_color = UP_COLOR  # 高于基线：红（正向判别力）
            elif stats.hit_rate < result.baseline_hit_rate:
                signal_color = DOWN_COLOR  # 低于基线：绿（负向判别力）
            else:
                signal_color = NEUTRAL_COLOR
            stats_line = (
                f'<p class="muted">均值 {_pct(stats.avg)} · 中位数 {_pct(stats.median)}'
                f" · 最好 {_pct(stats.best)} · 最差 {_pct(stats.worst)}"
                f"（n={stats.n}）</p>"
            )
        else:
            signal_color = NEUTRAL_COLOR
            stats_line = '<p class="muted">无可计算样本（全部截尾或无信号）</p>'
        cards.append(
            f'<div class="card"><h2>{_esc(name)} 前向收益</h2>'
            + _bar_row("信号胜率", stats.hit_rate, signal_color)
            + _bar_row("基线胜率", result.baseline_hit_rate, NEUTRAL_COLOR)
            + stats_line
            + "</div>"
        )
    return "".join(cards)


def _rule_chips(result: BacktestResult) -> str:
    """分规则触发次数（chips）。"""
    if not result.per_rule:
        return '<p class="muted">无信号触发</p>'
    chips = "".join(
        f'<span class="chip">{_esc(rule)} × {count}</span>'
        for rule, count in result.per_rule.items()
    )
    return f'<div class="meta">{chips}</div>'


_CSS = f"""
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;background:#f2f3f5;color:#1c1e21;padding:16px;line-height:1.5}}
.wrap{{max-width:760px;margin:0 auto}}
.card{{background:#fff;border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
h1{{font-size:20px;margin-bottom:2px}}
h2{{font-size:15px;margin-bottom:10px}}
.meta{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}}
.chip{{background:#f2f3f5;border-radius:8px;padding:4px 10px;font-size:12px;color:#555}}
.bar-row{{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:12px}}
.bar-label{{width:72px;color:#666;flex:none}}
.bar-track{{flex:1;background:#ececec;border-radius:7px;height:14px;overflow:hidden;display:block}}
.bar-fill{{display:block;height:100%;border-radius:7px}}
.bar-val{{width:72px;text-align:right;flex:none;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{padding:6px 4px;text-align:right;border-bottom:1px solid #f0f0f0;white-space:nowrap}}
th{{color:#888;font-weight:500}}
th:first-child,td:first-child{{text-align:left}}
.up{{color:{UP_COLOR}}}
.down{{color:{DOWN_COLOR}}}
.badge{{font-size:10px;padding:1px 6px;border-radius:6px;background:#fdecea;color:#c0392b}}
.muted{{color:#888;font-size:12px}}
.gaps li{{font-size:12px;color:#a2701e;margin:4px 0 0 18px}}
.skipped li{{font-size:12px;color:#666;margin:4px 0 0 18px}}
.footer{{font-size:11px;color:#999;line-height:1.6;text-align:center;padding:6px 0 24px}}
"""


def render_backtest_html(result: BacktestResult) -> str:
    """渲染自包含单文件 HTML 回测报告（无任何外链 CDN）。"""
    range_text = (
        f"{result.start.strftime('%Y-%m-%d')} ~ {result.end.strftime('%Y-%m-%d')}"
        if result.start and result.end
        else "—"
    )
    gaps_html = (
        "<ul class='gaps'>" + "".join(f"<li>{_esc(g)}</li>" for g in result.gaps) + "</ul>"
        if result.gaps
        else ""
    )
    skipped_html = (
        "<ul class='skipped'>"
        + "".join(f"<li>{_esc(s)}</li>" for s in result.skipped_rules)
        + "</ul>"
        if result.skipped_rules
        else ""
    )
    censor_note = (
        f"（其中 {result.n_censored} 条尾部截尾，仅可计算部分 horizon）"
        if result.n_censored
        else ""
    )
    return (
        "<!DOCTYPE html>\n<html lang='zh-CN'>\n<head>\n"
        "<meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        f"<title>回测复核 · {_esc(result.symbol)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n<div class='wrap'>\n"
        # ---- 头部 ---- #
        f"<div class='card'><h1>回测复核 · {_esc(result.symbol)}</h1>"
        "<div class='meta'>"
        f"<span class='chip'>区间 {range_text}</span>"
        f"<span class='chip'>回放 {result.n_days} 个交易日</span>"
        f"<span class='chip'>信号 {result.n_signals} 条{censor_note}</span>"
        f"<span class='chip'>基线胜率 {_pct(result.baseline_hit_rate)}</span>"
        "</div>"
        + (f"<div style='margin-top:10px'><p class='muted'>数据缺口：</p>{gaps_html}</div>" if gaps_html else "")
        + "</div>"
        # ---- 各 horizon 对比 ---- #
        + _horizon_cards(result)
        # ---- 分规则触发 ---- #
        + f"<div class='card'><h2>分规则触发次数</h2>{_rule_chips(result)}</div>"
        # ---- 信号明细 ---- #
        + (
            "<div class='card'><h2>信号明细</h2><table>"
            "<tr><th>日期</th><th>规则</th><th>方向</th><th>收盘</th>"
            "<th>T+1</th><th>T+5</th><th>备注</th></tr>"
            + _signal_rows(result)
            + "</table></div>"
        )
        # ---- 未纳入回放的规则（诚实性披露）---- #
        + (
            f"<div class='card'><h2>未纳入回放的规则</h2>{skipped_html}"
            "<p class='muted'>以上规则依赖盘中 / 板块数据，日线回放无法覆盖，"
            "本报告不对其判别力下结论。</p></div>"
            if skipped_html
            else ""
        )
        # ---- 免责声明 ---- #
        + f"<p class='footer'>{_DISCLAIMER}</p>"
        + "</div>\n</body>\n</html>\n"
    )


def write_report(
    result: BacktestResult,
    out_path: str | Path,
    filename: str | None = None,
) -> Path:
    """渲染并写出 HTML 报告。

    Args:
        result: 回测结果。
        out_path: 目标目录；若以 ``.html`` 结尾则视为完整文件路径。
        filename: 文件名；缺省 ``BT-<symbol>-<YYYYMMDD>.html``
            （日期取 result.end，无则取今天）。
    Returns:
        实际写出的文件路径。
    """
    out = Path(out_path)
    if out.suffix.lower() == ".html":
        target = out
    else:
        day = result.end or date.today()
        name = filename or f"BT-{result.symbol}-{day.strftime('%Y%m%d')}.html"
        target = out / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_backtest_html(result), encoding="utf-8")
    return target
