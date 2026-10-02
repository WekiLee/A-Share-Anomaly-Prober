"""定时调度子包（T12）。

职责：
- ``jobs``：纯函数作业（扫描 watchlist / 推送异动 / 刷新大盘看板）。
  不依赖调度器，可独立调用与单测；全部遵循 best-effort + 异常隔离纪律。
- ``service``：``SchedulerService`` 定时服务。apscheduler 为**惰性依赖**：
  未安装时仅 ``start()`` 抛带安装提示的 ``RuntimeError``，``run_once`` 不受影响。

设计原则：作业层不抛异常（单标的 / 单事件 / 单步骤失败均隔离并记日志），
保证「收盘后自动推送 + 看板刷新」这条链路尽量走完。
"""

from ashare_prober.scheduler.jobs import (
    DEFAULT_OUTPUT_DIR,
    push_events,
    refresh_dashboard,
    scan_watchlist,
)
from ashare_prober.scheduler.service import SchedulerService

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "SchedulerService",
    "push_events",
    "refresh_dashboard",
    "scan_watchlist",
]
