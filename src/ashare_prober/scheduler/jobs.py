"""定时作业纯函数（T12）。

三个作业均为**纯函数 / best-effort**，不依赖调度器，可独立调用与单测：
- ``scan_watchlist``：逐标的跑异动引擎并落库；单标的失败隔离，不阻断其它。
- ``push_events``：把事件逐条推送到全部已配置通道；通道为空时安静返回 []。
- ``refresh_dashboard``：重建大盘看板并渲染为 ``outputs/index.html``（默认页）。

异常纪律：本模块函数**对外不抛异常**（除参数级编程错误），失败一律记日志并以
安全返回值表达（空列表 / False / failures 列表）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Optional

import structlog

from ashare_prober.anomaly.engine import AnomalyEngine
from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.push.models import PushResult

logger = structlog.get_logger(__name__)

#: 默认输出目录（与 ``cli.DEFAULT_OUTPUT_DIR`` 口径一致：项目根 ``outputs/``）。
DEFAULT_OUTPUT_DIR = Path("outputs")


def scan_watchlist(
    provider: Any,
    store: Any,
    symbols: Sequence[str],
    day: Optional[date] = None,
) -> tuple[list[AnomalyEvent], list[tuple[str, str]]]:
    """逐个标的跑异动扫描并落库。

    Args:
        provider: 数据门面（``Provider`` 或测试替身；``None`` 时引擎取数失败，
            各标的安静返回空事件）。
        store: 本地库（透传给引擎 ``save_event`` 落库；可为 ``None``）。
        symbols: 待扫描标的代码列表。
        day: 判定交易日；缺省取今天（透传给引擎）。

    Returns:
        ``(events, failures)`` 二元组：

        - ``events``：全部产出的事件（已由引擎落库）。
        - ``failures``：``(symbol, 错误摘要)`` 列表；单标的抛异常时记录并继续，
          绝不阻断其它标的。
    """
    engine = AnomalyEngine(provider, store=store)
    events: list[AnomalyEvent] = []
    failures: list[tuple[str, str]] = []
    for symbol in symbols:
        try:
            events.extend(engine.scan("stock", symbol, day))
        except Exception as exc:  # noqa: BLE001 - 单标的失败不阻断其它
            logger.warning(
                "扫描标的失败，已隔离", symbol=str(symbol), error=str(exc)
            )
            failures.append((str(symbol), f"{type(exc).__name__}: {exc}"))
    return events, failures


def push_events(
    events: Sequence[AnomalyEvent],
    settings: Any = None,
    store: Any = None,
) -> list[PushResult]:
    """把异动事件逐条推送到所有已配置通道（best-effort）。

    - 事件为空 → 直接返回 ``[]``（不构造推送管理器）。
    - ``store`` 非 None → 先用 ``resolve_runtime_settings`` 合并 DB 上的
      页面配置覆盖段（页面改完下一轮推送即生效；解析失败回退传入配置）。
    - 未配置任何通道（webhook 全空 / 总开关关闭）→ 安静返回 ``[]``。
    - 单事件推送失败（通道异常 / 管理器失控）→ 记日志并继续其它事件。

    Args:
        events: 待推送的异动事件序列。
        settings: 基础配置对象；``None`` 时推送管理器内部取 ``get_settings()``。
        store: 本地库；非 ``None`` 时按其 ``app_settings`` 覆盖段解析生效配置。

    Returns:
        逐通道推送结果列表（可能为空）。
    """
    if not events:
        return []

    # 页面自助配置接线：store 非 None 时合并 DB 覆盖段（失败回退，不阻断推送）。
    if store is not None:
        try:
            from ashare_prober.data.appsettings import resolve_runtime_settings

            settings = resolve_runtime_settings(store, settings)
        except Exception as exc:  # noqa: BLE001 - 配置解析失控时兜底
            logger.warning("运行时配置解析失败，回退基础配置推送", error=str(exc))

    # 惰性 import + 惰性构造：无推送配置时不产生任何副作用。
    try:
        from ashare_prober.push.manager import PushManager

        manager = (
            PushManager() if settings is None else PushManager(settings=settings)
        )
    except Exception as exc:  # noqa: BLE001 - 推送层构造失控时兜底
        logger.warning("推送管理器构造失败，跳过推送", error=str(exc))
        return []

    if not manager.channels:
        return []

    results: list[PushResult] = []
    for event in events:
        try:
            results.extend(manager.send_event(event))
        except Exception as exc:  # noqa: BLE001 - 单事件失败不影响其它
            logger.warning(
                "单事件推送失败，已隔离",
                event_id=str(getattr(event, "id", "")),
                error=str(exc),
            )
    return results


def refresh_dashboard(
    out_dir: Path | str | None = None,
    provider: Any = None,
    store: Any = None,
) -> bool:
    """重建大盘看板并渲染默认页 ``index.html``（best-effort）。

    Args:
        out_dir: 输出目录；缺省 ``DEFAULT_OUTPUT_DIR``（项目根 ``outputs/``）。
        provider: 数据门面；``None`` 时看板全部指标降级为空（仍可渲染）。
        store: 本地库；提供 ``list_events`` 时看板展示近期异动。

    Returns:
        渲染成功返回 ``True``；任何失败记日志并返回 ``False``，不抛异常。
    """
    out_path = Path(out_dir) if out_dir is not None else DEFAULT_OUTPUT_DIR
    try:
        from ashare_prober.dashboard.collector import MarketDashboardBuilder
        from ashare_prober.presentation.render import Render

        dashboard = MarketDashboardBuilder(provider=provider, store=store).build()
        out_path.mkdir(parents=True, exist_ok=True)
        Render().render_dashboard(dashboard, out_path, "index.html")
        return True
    except Exception as exc:  # noqa: BLE001 - 看板刷新失败不影响扫描/推送
        logger.warning(
            "刷新大盘看板失败",
            out_dir=str(out_path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
