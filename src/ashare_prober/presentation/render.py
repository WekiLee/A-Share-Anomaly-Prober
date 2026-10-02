"""展示层渲染器（T23）。

将 ``EventDossier`` + ``ResearchReport`` + ``ApprovalRequest`` 渲染为**自包含、可离线打开**
的单文件 HTML（内联 CSS/JS，禁止任何外链）。颜色遵循红涨绿跌共享约定（来自 ``config``）。

对外暴露 ``Render``：
- ``render_dossier(dossier, report=None, approval=None, out_path=None, event=None) -> str``
  返回完整 HTML 文本；若给定 ``out_path``（输出目录），则落盘 ``<out_path>/<dossier_id>.html``。
- ``render_card(dossier, event=None, approval=None) -> str``：紧凑卡片（``anomaly_card.html``）。
- ``render_dashboard(dashboard, out_path=None, filename="index.html") -> str``：
  **默认页**大盘总览看板（``market_dashboard.html``），自包含、可离线打开。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ashare_prober.attribution.models import EventDossier
from ashare_prober.config import DOWN_COLOR, NEUTRAL_COLOR, UP_COLOR
from ashare_prober.dashboard.models import MarketDashboard
from ashare_prober.research.models import ApprovalRequest, ResearchReport

# 模板目录：presentation/templates
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# 异动类型 → 中文标签
_TYPE_LABELS: dict[str, str] = {
    "volume_break_up": "放量上破",
    "volume_break_down": "放量破位",
    "sector_flow_flip": "板块资金流翻转",
    "sector_rank_jump": "板块排名跳变",
    "limit_pool": "涨跌停池",
    "auction": "集合竞价异动",
}

# 告警级别 → 中文标签
_LEVEL_LABELS: dict[str, str] = {
    "info": "提示",
    "watch": "关注",
    "alert": "警报",
}

# 审批/裁定状态 → 中文标签
_STATUS_LABELS: dict[str, str] = {
    "pending": "待审批",
    "approved": "已批准",
    "rejected": "已驳回",
    "tentative": "初判",
    "confirmed": "已确认",
    "rejected_event": "已否决",
}

# 异动事件状态 → 中文标签（用于摘要卡「状态」字段）
_EVENT_STATUS_LABELS: dict[str, str] = {
    "tentative": "初判",
    "confirmed": "已确认",
    "rejected": "已否决",
}


def _label(mapping: dict[str, str], key: str, default: str = "—") -> str:
    """从映射取标签；缺失时回退到 default。"""
    return mapping.get(key, default)


# --------------------------------------------------------------------------- #
# 看板格式化辅助（金额 / 百分比 / 方向 / 条形宽度）
# --------------------------------------------------------------------------- #
def _direction(value: float) -> str:
    """按数值正负返回方向：``up``（涨·红）/ ``down``（跌·绿）/ ``neutral``。"""
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "neutral"


def _fmt_pct(value: float, digits: int = 2) -> str:
    """格式化涨跌幅为带符号百分比，如 ``"+1.23%"`` / ``"-0.45%"``。"""
    return f"{float(value or 0.0):+.{digits}f}%"


def _fmt_amount(amount: float) -> str:
    """格式化成交额（元）为中文单位：万亿 / 亿 / 万。"""
    value = float(amount or 0.0)
    sign = "-" if value < 0 else ""
    abs_value = abs(value)
    if abs_value >= 1e12:
        return f"{sign}{abs_value / 1e12:.2f} 万亿"
    if abs_value >= 1e8:
        return f"{sign}{abs_value / 1e8:.2f} 亿"
    if abs_value >= 1e4:
        return f"{sign}{abs_value / 1e4:.2f} 万"
    return f"{sign}{abs_value:.2f}"


def _fmt_yi(amount: float, digits: int = 2) -> str:
    """格式化资金流（元）为「亿元」字符串，带符号。"""
    value = float(amount or 0.0)
    return f"{value / 1e8:+.{digits}f} 亿"


def _fmt_points(value: float) -> str:
    """格式化指数点位（千分位 + 两位小数）。"""
    return f"{float(value or 0.0):,.2f}"


def _fmt_time(value: object) -> str:
    """格式化时间为 ``YYYY-MM-DD HH:MM``；非时间对象原样转字符串。"""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _bar_width(value: float, max_abs: float, min_width: float = 4.0) -> float:
    """按绝对值占最大值的比例算条形宽度（%）；最大值为 0 时返回 min_width。"""
    abs_value = abs(float(value or 0.0))
    if max_abs <= 0:
        return min_width
    width = abs_value / max_abs * 100.0
    return max(min_width, min(100.0, width))


class Render:
    """HTML 渲染器：事件档案 → 自包含单文件 HTML。"""

    def __init__(self, template_dir: Optional[Path] = None) -> None:
        self.template_dir = Path(template_dir) if template_dir else _TEMPLATE_DIR
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def render_dossier(
        self,
        dossier: EventDossier,
        report: Optional[ResearchReport] = None,
        approval: Optional[ApprovalRequest] = None,
        out_path: Optional[Any] = None,
        event: Optional[Any] = None,
    ) -> str:
        """渲染完整事件档案页。

        Args:
            dossier: 事件档案（必填）。
            report: 研判报告（可空）。
            approval: 人审申请（可空）。
            out_path: 输出目录；若提供则写入 ``<out_path>/<dossier_id>.html``。
            event: 关联异动事件（可空；用于摘要卡的类型/级别/触发时间/状态）。

        Returns:
            完整 HTML 字符串。
        """
        ctx = self._build_context(dossier, report, approval, event)
        html = self.env.get_template("event_dossier.html").render(**ctx)
        if out_path is not None:
            out_dir = Path(out_path)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"{dossier.id}.html"
            out_file.write_text(html, encoding="utf-8")
        return html

    def render_card(
        self,
        dossier: EventDossier,
        event: Optional[Any] = None,
        approval: Optional[ApprovalRequest] = None,
    ) -> str:
        """渲染紧凑卡片（``anomaly_card.html``）。"""
        ctx = self._build_context(dossier, None, approval, event)
        return self.env.get_template("anomaly_card.html").render(**ctx)

    def render_dashboard(
        self,
        dashboard: MarketDashboard,
        out_path: Optional[Any] = None,
        filename: str = "index.html",
    ) -> str:
        """渲染**默认页**大盘总览看板（``market_dashboard.html``）。

        Args:
            dashboard: 大盘看板聚合体（含 ``gaps`` 数据缺口清单）。
            out_path: 输出目录；若提供则写入 ``<out_path>/<filename>``
                （默认 ``index.html``，即默认页）。
            filename: 输出文件名，默认 ``index.html``。

        Returns:
            完整 HTML 字符串（自包含、可离线打开）。
        """
        ctx = self._build_dashboard_context(dashboard)
        html = self.env.get_template("market_dashboard.html").render(**ctx)
        if out_path is not None:
            out_dir = Path(out_path)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / filename).write_text(html, encoding="utf-8")
        return html

    # ------------------------------------------------------------------ #
    # 上下文构造：大盘看板
    # ------------------------------------------------------------------ #
    def _build_dashboard_context(self, dashboard: MarketDashboard) -> dict[str, Any]:
        """把看板模型预格式化为模板可直接渲染的 dict（方向 / 文案 / 条形宽度）。"""
        indices = [
            {
                "code": it.code,
                "name": it.name or it.code,
                "last": it.last,
                "last_text": _fmt_points(it.last),
                "change": it.change,
                "change_text": f"{it.change:+.2f}",
                "pct_change": it.pct_change,
                "pct_text": _fmt_pct(it.pct_change),
                "amount": it.amount,
                "amount_text": _fmt_amount(it.amount),
                "direction": _direction(it.pct_change),
            }
            for it in dashboard.indices
        ]

        breadth = None
        if dashboard.breadth is not None:
            raw = dashboard.breadth
            total = max(0, int(raw.total))
            up_pct = raw.up_ratio if total else 0.0
            down_pct = (raw.down_count * 100.0 / total) if total else 0.0
            flat_pct = (raw.flat_count * 100.0 / total) if total else 0.0
            breadth = {
                "up_count": raw.up_count,
                "down_count": raw.down_count,
                "flat_count": raw.flat_count,
                "limit_up_count": raw.limit_up_count,
                "limit_down_count": raw.limit_down_count,
                "total": total,
                "up_ratio": raw.up_ratio,
                "up_ratio_text": f"{raw.up_ratio:.1f}%",
                "up_pct": f"{up_pct:.2f}",
                "down_pct": f"{down_pct:.2f}",
                "flat_pct": f"{flat_pct:.2f}",
            }

        sector_max = 0.0
        for item in list(dashboard.sectors_top) + list(dashboard.sectors_bottom):
            sector_max = max(sector_max, abs(float(item.main_net_inflow or 0.0)))

        def sector_ctx(item: Any) -> dict[str, Any]:
            return {
                "name": item.name,
                "pct_change": item.pct_change,
                "pct_text": _fmt_pct(item.pct_change),
                "inflow_text": _fmt_yi(item.main_net_inflow),
                "rank": abs(int(item.rank)),
                "bar_width": f"{_bar_width(item.main_net_inflow, sector_max):.2f}",
                "direction": _direction(item.main_net_inflow),
            }

        def limit_ctx(item: Any) -> dict[str, Any]:
            return {
                "code": item.stock_code,
                "name": item.name or item.stock_code,
                "price_text": _fmt_points(item.price),
                "pct_text": _fmt_pct(item.pct_change),
                "boards": int(item.consecutive_boards or 0),
                "boards_text": f"{int(item.consecutive_boards or 0)} 连板",
                "reason": item.reason or "—",
                "direction": _direction(item.pct_change),
            }

        external = [
            {
                "name": it.name,
                "category": it.category,
                "last_text": _fmt_points(it.last),
                "pct_text": _fmt_pct(it.pct_change),
                "direction": _direction(it.pct_change),
                "time_text": _fmt_time(it.timestamp),
            }
            for it in dashboard.external
        ]

        recent_events = [
            {
                "id": str(ev.get("id", "")),
                "code": str(ev.get("code", "")),
                "name": str(ev.get("name", "") or ev.get("code", "")),
                "type": str(ev.get("type", "")),
                "type_label": _label(
                    _TYPE_LABELS, str(ev.get("type", "")), default=str(ev.get("type", "")) or "—"
                ),
                "level": str(ev.get("level", "")),
                "level_label": _label(
                    _LEVEL_LABELS, str(ev.get("level", "")), default=str(ev.get("level", "")) or "—"
                ),
                "detected_text": _fmt_time(ev.get("detected_at")),
            }
            for ev in dashboard.recent_events
        ]

        # 监控名单（dict 归一：{code,name,active}）——纯新增 context 字段，
        # 供看板页「监控列表」卡片先渲染一版静态内容（file:// 打开也有内容）。
        watchlist = [
            {
                "code": str(w.get("code", "")),
                "name": str(w.get("name", "")),
                "active": bool(w.get("active", True)),
            }
            for w in dashboard.watchlist
            if isinstance(w, dict)
        ]

        meta = {
            "date_text": _fmt_time(dashboard.date),
            "updated_text": _fmt_time(dashboard.updated_at),
            "total_amount_text": _fmt_amount(dashboard.total_amount),
            "gaps": list(dashboard.gaps),
            "has_gaps": bool(dashboard.gaps),
        }

        return {
            "meta": meta,
            "indices": indices,
            "breadth": breadth,
            "sectors_top": [sector_ctx(it) for it in dashboard.sectors_top],
            "sectors_bottom": [sector_ctx(it) for it in dashboard.sectors_bottom],
            "limit_up": [limit_ctx(it) for it in dashboard.limit_up],
            "limit_down": [limit_ctx(it) for it in dashboard.limit_down],
            "external": external,
            "recent_events": recent_events,
            "watchlist": watchlist,
            "colors": {
                "up": UP_COLOR,
                "down": DOWN_COLOR,
                "neutral": NEUTRAL_COLOR,
            },
        }

    # ------------------------------------------------------------------ #
    # 上下文构造
    # ------------------------------------------------------------------ #
    def _build_context(
        self,
        dossier: EventDossier,
        report: Optional[ResearchReport],
        approval: Optional[ApprovalRequest],
        event: Optional[Any],
    ) -> dict[str, Any]:
        # 标的代码：优先从事件，否则从档案 id（DSS-<code>-<date>）解析。
        code = getattr(event, "ref_code", "") or ""
        if not code:
            parts = dossier.id.split("-")
            if len(parts) >= 2:
                code = parts[1]
        name = (getattr(event, "ref_name", "") or "") or code or "—"

        atype = getattr(event, "type", "") or ""
        level = getattr(event, "level", "") or ""
        event_status = getattr(event, "status", "") or ""
        detected_at = getattr(event, "detected_at", None) or dossier.created_at

        # 审批状态角标：有审批取审批状态，否则「待审批」。
        status = approval.status if approval else "pending"
        # 方向（红涨绿跌）：上破/上涨为 up（红），破位/下跌为 down（绿）。
        direction = (
            "up" if atype.endswith("_up")
            else ("down" if atype.endswith("_down") else "neutral")
        )

        summary = {
            "code": code,
            "name": name,
            "type": atype,
            "type_label": _label(_TYPE_LABELS, atype, default=atype or "—"),
            "level": level,
            "level_label": _label(_LEVEL_LABELS, level, default=level or "—"),
            "event_status": event_status,
            "event_status_label": _label(_EVENT_STATUS_LABELS, event_status, default="—"),
            "status": status,
            "status_label": _STATUS_LABELS.get(status, status),
            "direction": direction,
            "detected_at": detected_at,
            "dossier_id": dossier.id,
            "version": dossier.version,
            "colors": {
                "up": UP_COLOR,
                "down": DOWN_COLOR,
                "neutral": NEUTRAL_COLOR,
            },
        }
        return {
            "summary": summary,
            "dossier": dossier,
            "report": report,
            "approval": approval,
            "event": event,
        }
