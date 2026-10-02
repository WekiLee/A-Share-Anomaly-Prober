"""定时调度服务（T12）。

``SchedulerService`` 把「收盘后扫描 watchlist → 推送异动 → 刷新大盘看板」
编排为一轮作业（``run_once``），并可用 apscheduler 按周一至周五的配置时刻
定时触发（``start``）。

依赖纪律：
- ``run_once`` **不依赖 apscheduler**：是 CLI ``--once`` 与测试的入口，
  注入 ``scan_fn`` / Fake provider 即可完全离线运行。
- apscheduler 为**惰性 import**：未安装时仅 ``start()`` 抛带安装提示的
  ``RuntimeError``；``run_once`` 不受影响。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from ashare_prober.config import DEFAULT_DB_PATH, TZ, ScheduleSettings, get_settings
from ashare_prober.scheduler.jobs import (
    DEFAULT_OUTPUT_DIR,
    push_events,
    refresh_dashboard,
    scan_watchlist,
)

logger = structlog.get_logger(__name__)

#: 扫描作业函数签名（便于注入测试替身）。
ScanFn = Callable[[Any, Any, list[str], Optional[date]], tuple[list[Any], list[tuple[str, str]]]]


def _split_hhmm(value: str, default: tuple[int, int] = (15, 5)) -> tuple[int, int]:
    """把 ``HH:MM`` 时刻解析为 ``(hour, minute)``；非法时返回 ``default``。"""
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return default
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


class SchedulerService:
    """定时调度服务：扫描 → 推送 → 刷新看板。

    Attributes:
        provider: 数据门面（可为 ``None``；默认扫描函数运行时惰性构造）。
        store: 本地库（可为 ``None``；默认扫描函数运行时惰性构造）。
        settings: 全局配置（缺省 ``get_settings()``）。
        scan_fn: 扫描作业函数（缺省 ``scan_watchlist``；测试注入替身的关键）。
        symbols: 待扫描标的（缺省取 ``settings.watchlist.symbols``）。
        out_dir: 看板输出目录（缺省项目根 ``outputs/``）。
    """

    def __init__(
        self,
        provider: Any = None,
        store: Any = None,
        settings: Any = None,
        scan_fn: Optional[ScanFn] = None,
        symbols: Optional[list[str]] = None,
        out_dir: Path | str | None = None,
    ) -> None:
        """初始化调度服务。

        Args:
            provider: 数据门面；``None`` 时在 ``run_once`` 中惰性构造默认门面。
            store: 本地库；``None`` 时在 ``run_once`` 中按 ``settings.db_path`` 惰性构造。
            settings: 配置对象；``None`` 时取 ``get_settings()``。
            scan_fn: 扫描函数（签名 ``(provider, store, symbols, day)``）；
                ``None`` 时用 ``scan_watchlist``。
            symbols: 标的列表；``None`` 时取 ``settings.watchlist.symbols``。
            out_dir: 看板输出目录；``None`` 时用 ``DEFAULT_OUTPUT_DIR``。
        """
        self.settings: Any = settings if settings is not None else get_settings()

        # 名单惰性解析：symbols 显式传入时固定不变；否则在 run_once/start
        # 执行时按「store 名单优先（真源）→ settings.watchlist 兜底」解析，
        # 这样页面上刚加的票下一轮扫描即可被覆盖。
        self._symbols_explicit: bool = symbols is not None
        if symbols is None:
            watchlist = getattr(self.settings, "watchlist", None)
            symbols = list(getattr(watchlist, "symbols", []) or [])
        self.symbols: list[str] = [str(s) for s in symbols]

        self.scan_fn: ScanFn = scan_fn if scan_fn is not None else scan_watchlist
        self.out_dir: Path = Path(out_dir) if out_dir is not None else DEFAULT_OUTPUT_DIR
        self.provider: Any = provider
        self.store: Any = store
        self._scheduler: Any | None = None

    # ------------------------------------------------------------------ #
    # 依赖惰性构造（仅默认扫描路径需要真实 provider / store）
    # ------------------------------------------------------------------ #
    def _ensure_live_deps(self) -> tuple[Any, Any]:
        """惰性构造默认数据门面与本地库（失败降级为 ``None``，不抛）。"""
        if self.provider is None:
            try:
                from ashare_prober.data.provider import build_default_provider

                self.provider = build_default_provider()
            except Exception as exc:  # noqa: BLE001 - 无数据源环境降级
                logger.warning("默认数据源构造失败，扫描将空转", error=str(exc))
        if self.store is None:
            try:
                from ashare_prober.data.store import Store

                self.store = Store(getattr(self.settings, "db_path", None) or DEFAULT_DB_PATH)
            except Exception as exc:  # noqa: BLE001 - 存储不可用时降级
                logger.warning("本地库构造失败，事件将不落库", error=str(exc))
        return self.provider, self.store

    def _resolve_symbols(self) -> list[str]:
        """惰性解析待扫描标的（每次执行时解析，保证新增标的即时生效）。

        优先级：
        1. 构造时显式传入的 ``symbols`` → 原样使用；
        2. ``store`` 非 None → ``resolve_watchlist(store, settings)``
           （DB 名单为真源，空名单降级配置兜底）；
        3. 其余维持旧行为 → ``settings.watchlist.symbols``。
        """
        if self._symbols_explicit:
            return self.symbols
        if self.store is not None:
            try:
                from ashare_prober.data.watchlist import resolve_watchlist

                resolved = resolve_watchlist(self.store, self.settings)
            except Exception as exc:  # noqa: BLE001 - 名单解析失败降级配置兜底
                logger.warning("名单解析失败，回退配置 watchlist", error=str(exc))
                resolved = []
            return [str(s) for s in resolved]
        return self.symbols

    def _effective_settings(self) -> Any:
        """合并页面自助配置覆盖，返回本轮作业的生效配置。

        - ``store`` 为 ``None`` → 直接返回基础配置（行为与历史版本一致）；
        - ``store`` 非 ``None`` → 用 ``resolve_runtime_settings`` 把
          ``app_settings`` 表的覆盖段字段级合并进基础配置（页面改完下一轮
          立即生效，无需重启）；解析失败回退基础配置，绝不抛出。
        """
        if self.store is None:
            return self.settings
        try:
            from ashare_prober.data.appsettings import resolve_runtime_settings

            return resolve_runtime_settings(self.store, self.settings)
        except Exception as exc:  # noqa: BLE001 - 配置接线失败不阻断调度
            logger.warning("生效配置解析失败，回退基础配置", error=str(exc))
            return self.settings

    # ------------------------------------------------------------------ #
    # 一轮作业（CLI --once 与测试入口；不依赖 apscheduler）
    # ------------------------------------------------------------------ #
    def run_once(self, day: Optional[date] = None) -> dict[str, Any]:
        """同步执行一轮作业：scan → push（enabled）→ refresh_dashboard（enabled）。

        Args:
            day: 判定交易日；``None`` 表示今天。

        Returns:
            汇总 dict：``{"events": n, "pushed": n, "failures": [...],
            "dashboard": True/False}``。``pushed`` 为成功推送的通道次数据。
        """
        provider, store = self.provider, self.store
        if self.scan_fn is scan_watchlist:
            # 默认扫描路径需要真实数据源 / 本地库（惰性构造，失败降级）。
            provider, store = self._ensure_live_deps()

        # 生效配置放在依赖构造之后：惰性构造出的 store 也参与覆盖合并。
        effective = self._effective_settings()
        schedule: ScheduleSettings = (
            getattr(effective, "schedule", None) or ScheduleSettings()
        )

        # 名单惰性解析放在依赖构造之后：惰性构造出的 store 也参与真源解析。
        symbols = self._resolve_symbols()

        events, failures = self.scan_fn(provider, store, symbols, day)

        summary: dict[str, Any] = {
            "events": len(events),
            "pushed": 0,
            "failures": list(failures),
            "dashboard": False,
        }

        # 步骤二：推送（总开关关闭 / 无事件时跳过）。
        # store 非 None 时传生效配置（页面覆盖的 push 段本轮立即生效）；
        # store 为 None 时保持历史调用形态 push_events(events) 完全不变。
        if bool(getattr(schedule, "push", True)) and events:
            try:
                if store is not None:
                    results = push_events(events, settings=effective)
                else:
                    results = push_events(events)
            except Exception as exc:  # noqa: BLE001 - 推送步骤失败不阻断看板
                logger.warning("推送步骤异常，已隔离", error=str(exc))
                results = []
            summary["pushed"] = sum(1 for r in results if bool(getattr(r, "ok", False)))

        # 步骤三：刷新大盘看板（总开关关闭时跳过）。
        if bool(getattr(schedule, "refresh_dashboard", True)):
            try:
                summary["dashboard"] = refresh_dashboard(self.out_dir, provider, store)
            except Exception as exc:  # noqa: BLE001 - 双保险：作业层已兜底
                logger.warning("看板刷新步骤异常，已隔离", error=str(exc))
                summary["dashboard"] = False

        return summary

    # ------------------------------------------------------------------ #
    # 定时运行（apscheduler 惰性依赖）
    # ------------------------------------------------------------------ #
    def start(self, blocking: bool = False) -> None:
        """按配置时刻（周一至周五）启动定时调度。

        Args:
            blocking: ``True`` 用 ``BlockingScheduler``（阻塞当前线程，适合
                常驻进程前台运行）；``False`` 用 ``BackgroundScheduler``。

        Raises:
            RuntimeError: 未安装 apscheduler 时抛出，信息含安装提示；
                ``run_once`` 不受此影响。
        """
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.schedulers.blocking import BlockingScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as exc:  # pragma: no cover - 由测试 monkeypatch 驱动
            raise RuntimeError(
                "未安装 apscheduler，无法启动定时调度。"
                "请先执行: pip install apscheduler（或 uv add apscheduler）。"
                "若只需执行一轮，可使用 --once 模式。"
            ) from exc

        # 生效配置：store 上页面覆盖的 schedule 段（扫描时刻/开关）即时生效。
        effective = self._effective_settings()
        schedule: ScheduleSettings = (
            getattr(effective, "schedule", None) or ScheduleSettings()
        )
        scheduler_cls = BlockingScheduler if blocking else BackgroundScheduler
        scheduler = scheduler_cls(timezone=TZ)

        hour, minute = _split_hhmm(schedule.scan_time)
        scheduler.add_job(
            self._scheduled_run,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=TZ),
            id="pm-scan",
            name="收盘后扫描 + 推送 + 看板刷新",
            replace_existing=True,
        )

        am_time = str(getattr(schedule, "am_scan_time", "") or "").strip()
        if am_time:
            am_hour, am_minute = _split_hhmm(am_time)
            scheduler.add_job(
                self._scheduled_run,
                CronTrigger(
                    day_of_week="mon-fri", hour=am_hour, minute=am_minute, timezone=TZ
                ),
                id="am-scan",
                name="早盘扫描",
                replace_existing=True,
            )

        self._scheduler = scheduler
        scheduler.start()
        logger.info(
            "定时调度已启动",
            blocking=blocking,
            scan_time=schedule.scan_time,
            am_scan_time=am_time or "(未启用)",
            symbols=len(self._resolve_symbols()),
        )

    def _scheduled_run(self) -> None:
        """调度器回调：跑一轮作业并记日志；异常隔离，绝不让调度线程崩溃。"""
        try:
            summary = self.run_once()
            logger.info("定时作业完成", **summary)
        except Exception as exc:  # noqa: BLE001 - 调度回调兜底
            logger.warning("定时作业异常", error=f"{type(exc).__name__}: {exc}")

    def stop(self) -> None:
        """安全关闭调度器（未启动时为空操作）。"""
        scheduler = self._scheduler
        if scheduler is None:
            return
        try:
            scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响进程退出
            logger.warning("调度器关闭异常", error=str(exc))
        finally:
            self._scheduler = None
