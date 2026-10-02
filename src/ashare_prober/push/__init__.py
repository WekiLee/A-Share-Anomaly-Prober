"""推送通道（T24）：把异动告警 / 大盘看板主动推到企业微信或飞书群。

对外暴露：
- ``PushMessage`` / ``PushResult`` / ``PushError``：消息与结果模型。
- ``PushChannel``：通道抽象基类（``send`` 不抛异常，``send_strict`` 抛 ``PushError``）。
- ``WecomChannel`` / ``FeishuChannel``：企业微信 / 飞书群机器人实现。
- ``PushManager``：多通道编排器（自动装配、失败隔离、异动事件格式化）。
"""

from __future__ import annotations

from ashare_prober.push.channels import (
    FeishuChannel,
    PushChannel,
    WecomChannel,
    feishu_sign,
)
from ashare_prober.push.manager import PushManager, format_event_message
from ashare_prober.push.models import PushError, PushMessage, PushResult

__all__ = [
    "FeishuChannel",
    "PushChannel",
    "PushError",
    "PushManager",
    "PushMessage",
    "PushResult",
    "WecomChannel",
    "feishu_sign",
    "format_event_message",
]
