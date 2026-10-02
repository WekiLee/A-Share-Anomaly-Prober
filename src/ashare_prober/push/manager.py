"""推送管理器：多通道编排 + 异动事件格式化（T24）。

职责：
- 按配置自动装配已启用的通道（企业微信 / 飞书）。
- 向所有通道广播消息，**单通道失败不影响其它通道**，并返回逐通道结果。
- 把 ``AnomalyEvent`` 格式化为可读的 ``PushMessage``（``send_event``）。

本模块只做编排与格式化，不负责渲染 HTML，也不直接发网络请求。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.config import get_settings
from ashare_prober.push.channels import FeishuChannel, PushChannel, WecomChannel
from ashare_prober.push.models import LEVEL_ALERT, LEVEL_INFO, PushMessage, PushResult

# --------------------------------------------------------------------------- #
# 展示文案映射
# --------------------------------------------------------------------------- #
LEVEL_LABELS: dict[str, str] = {
    LEVEL_ALERT: "警报",
    "watch": "关注",
    LEVEL_INFO: "提示",
}

TYPE_LABELS: dict[str, str] = {
    "volume_break_up": "放量上破",
    "volume_break_down": "放量下破",
    "sector_flow_flip": "板块资金流翻转",
    "sector_rank_jump": "板块排名跳变",
    "limit_pool_surge": "涨停池异动",
    "index_anomaly": "指数异动",
}

STATUS_LABELS: dict[str, str] = {
    "tentative": "盘中初判",
    "confirmed": "次日确认",
    "rejected": "已驳回",
}


def _label(mapping: dict[str, str], value: str, fallback: str = "") -> str:
    """查表取中文标签；未命中时回退原值或 ``fallback``。"""
    return mapping.get(value, value or fallback)


def _fmt_time(value: datetime) -> str:
    """把 ``detected_at`` 格式化为 ``YYYY-MM-DD HH:MM:SS``。"""
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, ValueError):  # pragma: no cover - 异常输入兜底
        return str(value)


def _subject(event: AnomalyEvent) -> str:
    """构造「名称(代码)」展示串，缺失时退化。"""
    name = (event.ref_name or "").strip()
    code = (event.ref_code or "").strip()
    if name and code:
        return f"{name}({code})"
    return name or code or "—"


def format_event_message(event: AnomalyEvent, extra: str = "") -> PushMessage:
    """把异动事件格式化为推送消息。

    Args:
        event: 异动事件。
        extra: 补充正文（如研判结论 / 归因摘要），可为空。

    Returns:
        ``PushMessage``：标题形如 ``[警报] 放量上破 · 贵州茅台(600519)``。
    """
    level = event.level or LEVEL_INFO
    level_label = _label(LEVEL_LABELS, level, LEVEL_INFO)
    type_label = _label(TYPE_LABELS, event.type, event.type or "异动")
    title = f"[{level_label}] {type_label} · {_subject(event)}"

    lines: list[str] = [
        f"- 事件 ID：{event.id}",
        f"- 触发时间：{_fmt_time(event.detected_at)}",
        f"- 级别：{level_label}（{level}）",
        f"- 标的：{_subject(event)}",
        f"- 类型：{type_label}（{event.type or '-'}）",
        f"- 状态：{_label(STATUS_LABELS, event.status, event.status)}",
    ]
    if event.rule_id:
        lines.append(f"- 规则：{event.rule_id}")
    if extra:
        lines.append("")
        lines.append(extra.strip())

    return PushMessage(title=title, content="\n".join(lines), level=level)


class PushManager:
    """多通道推送编排器。

    Attributes:
        channels: 已装配的通道列表（可能为空）。
        settings: 使用的配置对象。
    """

    def __init__(
        self,
        channels: Sequence[PushChannel] | None = None,
        settings: Any | None = None,
    ) -> None:
        """
        Args:
            channels: 显式传入的通道；为空（None 或空序列）时按配置自动装配。
            settings: 配置对象；留空则使用 ``get_settings()``。
        """
        self.settings: Any = settings if settings is not None else get_settings()
        if channels is None or len(list(channels)) == 0:
            self.channels: list[PushChannel] = self._build_channels(self.settings)
        else:
            self.channels = list(channels)

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_channels(settings: Any) -> list[PushChannel]:
        """按配置装配通道：配置了哪个 webhook 就启用哪个通道。"""
        push = getattr(settings, "push", None)
        if push is None:
            return []
        if not bool(getattr(push, "enabled", True)):
            return []

        timeout = float(getattr(push, "timeout", 10.0) or 10.0)
        built: list[PushChannel] = []
        wecom_webhook = str(getattr(push, "wecom_webhook", "") or "")
        feishu_webhook = str(getattr(push, "feishu_webhook", "") or "")

        if wecom_webhook:
            built.append(
                WecomChannel(webhook=wecom_webhook, timeout=timeout, settings=settings)
            )
        if feishu_webhook:
            built.append(
                FeishuChannel(
                    webhook=feishu_webhook,
                    secret=str(getattr(push, "feishu_secret", "") or ""),
                    timeout=timeout,
                    settings=settings,
                )
            )
        return built

    def add_channel(self, channel: PushChannel) -> None:
        """追加一个通道（去重：同名通道只保留最新一个）。"""
        new_name = str(getattr(channel, "name", ""))
        self.channels = [c for c in self.channels if str(getattr(c, "name", "")) != new_name]
        self.channels.append(channel)

    def channel_names(self) -> list[str]:
        """返回已装配通道名列表。"""
        return [str(getattr(c, "name", "unknown")) for c in self.channels]

    # ------------------------------------------------------------------ #
    # 推送
    # ------------------------------------------------------------------ #
    def send(self, msg: PushMessage) -> list[PushResult]:
        """向所有通道推送同一条消息；单通道失败不影响其它通道。"""
        results: list[PushResult] = []
        for channel in self.channels:
            try:
                results.append(channel.send(msg))
            except Exception as exc:  # noqa: BLE001 - 通道实现失控时兜底
                results.append(
                    PushResult(
                        channel=str(getattr(channel, "name", "unknown")),
                        ok=False,
                        message=f"推送通道异常：{exc}",
                    )
                )
        return results

    def send_event(self, event: AnomalyEvent, extra: str = "") -> list[PushResult]:
        """把异动事件格式化为消息并推送到所有通道。"""
        return self.send(format_event_message(event, extra))

    # ------------------------------------------------------------------ #
    # 结果辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def all_ok(results: Sequence[PushResult]) -> bool:
        """是否全部成功（空结果视为 False）。"""
        return len(results) > 0 and all(r.ok for r in results)

    @staticmethod
    def any_ok(results: Sequence[PushResult]) -> bool:
        """是否至少有一个成功。"""
        return any(r.ok for r in results)
