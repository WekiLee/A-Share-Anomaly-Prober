"""推送通道数据模型（T24）。

定义推送层的三个核心类型：
- ``PushMessage``：待推送消息（标题 / 正文 / 级别 / 详情链接 / @ 名单）。
- ``PushResult``：单通道推送结果（成功与否、HTTP 状态码、说明、耗时）。
- ``PushError``：推送失败统一异常，携带通道名、根因与状态码。

设计原则：本模块零副作用、零网络依赖，可被任意层安全 import。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# 常量：消息级别
# --------------------------------------------------------------------------- #
LEVEL_INFO = "info"  # 一般提示
LEVEL_WATCH = "watch"  # 关注
LEVEL_ALERT = "alert"  # 警报

VALID_LEVELS: tuple[str, ...] = (LEVEL_INFO, LEVEL_WATCH, LEVEL_ALERT)


class PushMessage(BaseModel):
    """待推送的消息体。

    Attributes:
        title: 消息标题（必填，会作为正文首行）。
        content: 正文（markdown 或纯文本均可；企业微信按 markdown 渲染）。
        level: 级别，取值 ``info`` / ``watch`` / ``alert``，默认 ``info``。
        url: 可选详情链接（渲染为「查看详情」）。
        mentions: @ 名单。飞书填 ``user_id``（如 ``ou_xxx``）；
            企业微信填手机号或 ``@all``。
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    content: str = ""
    level: str = LEVEL_INFO
    url: str = ""
    mentions: list[str] = Field(default_factory=list)

    def normalized_level(self) -> str:
        """返回合法级别；非法值回退 ``info``。"""
        return self.level if self.level in VALID_LEVELS else LEVEL_INFO


class PushResult(BaseModel):
    """单通道推送结果。

    Attributes:
        channel: 通道名（``wecom`` / ``feishu`` / 自定义通道）。
        ok: 是否成功（HTTP 正常且业务返回码为 0）。
        status_code: HTTP 状态码；未发出请求时为 ``None``。
        message: 结果说明；失败时记录失败原因（errcode / errmsg / 异常信息）。
        elapsed_ms: 本次推送耗时（毫秒）。
    """

    model_config = ConfigDict(extra="ignore")

    channel: str = ""
    ok: bool = False
    status_code: int | None = None
    message: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（便于日志 / CLI 表格展示）。"""
        return {
            "channel": self.channel,
            "ok": self.ok,
            "status_code": self.status_code,
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
        }


class PushError(RuntimeError):
    """推送失败统一异常。

    Attributes:
        channel: 抛出该异常的通道名。
        cause: 原始异常（网络错误等），可能为 ``None``。
        status_code: 已知时的 HTTP 状态码。
        message: 人类可读的失败原因。
    """

    def __init__(
        self,
        message: str,
        channel: str = "",
        cause: BaseException | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.channel: str = channel
        self.cause: BaseException | None = cause
        self.status_code: int | None = status_code

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        prefix = f"[{self.channel}] " if self.channel else ""
        return f"{prefix}{self.message}"
