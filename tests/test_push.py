"""推送通道单测（T24）。

**全部离线**：真实 HTTP 发送被注入的 ``FakePoster`` 替换，任何用例都不会发起
外网请求，也不使用真实 webhook 地址。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.config import PushSettings, Settings
from ashare_prober.push import (
    FeishuChannel,
    PushChannel,
    PushError,
    PushManager,
    PushMessage,
    PushResult,
    WecomChannel,
)
from ashare_prober.push import channels as channels_mod

# 测试专用占位地址（不是真实 webhook，且永远不会真正发出请求）。
FAKE_WECOM_URL = "https://example.invalid/wecom?key=test-only"
FAKE_FEISHU_URL = "https://example.invalid/feishu?token=test-only"


class FakePoster:
    """HTTP 发送替身：记录调用并返回预设响应（或抛出预设异常）。"""

    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def __call__(
        self, url: str, payload: dict[str, Any], timeout: float = 10.0
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, payload, timeout))
        if self.exc is not None:
            raise self.exc
        return self.status, dict(self.body)

    @property
    def payload(self) -> dict[str, Any]:
        """最近一次请求的请求体。"""
        assert self.calls, "未发起任何请求"
        return self.calls[-1][1]


class RecordingChannel(PushChannel):
    """记录消息的假通道（用于验证 PushManager 编排）。"""

    name = "recording"

    def __init__(self, ok: bool = True, status: int = 200) -> None:
        super().__init__(timeout=1.0)
        self.ok = ok
        self.status = status
        self.messages: list[PushMessage] = []

    def send_strict(self, msg: PushMessage) -> PushResult:
        self.messages.append(msg)
        if not self.ok:
            raise PushError("模拟通道失败", channel=self.name, status_code=self.status)
        return PushResult(channel=self.name, ok=True, status_code=self.status, message="ok")


def _no_push_settings() -> Settings:
    """返回一个「未配置任何推送」的配置对象（避免读取本机 config.toml）。"""
    return Settings(push=PushSettings())


def _sample_message() -> PushMessage:
    return PushMessage(
        title="[警报] 放量上破 · 贵州茅台(600519)",
        content="- 放量 3.2 倍，收盘价 1712.00",
        level="alert",
    )


# --------------------------------------------------------------------------- #
# 企业微信
# --------------------------------------------------------------------------- #
def test_wecom_missing_webhook_returns_not_ok_without_raising() -> None:
    poster = FakePoster(body={"errcode": 0, "errmsg": "ok"})
    channel = WecomChannel(webhook="", poster=poster, settings=_no_push_settings())

    result = channel.send(_sample_message())

    assert result.ok is False
    assert "未配置" in result.message
    assert result.channel == "wecom"
    assert poster.calls == []  # 未发请求


def test_wecom_success_payload_is_markdown() -> None:
    poster = FakePoster(body={"errcode": 0, "errmsg": "ok"})
    channel = WecomChannel(webhook=FAKE_WECOM_URL, timeout=3.5, poster=poster)

    result = channel.send(_sample_message())

    assert result.ok is True
    assert result.status_code == 200
    assert result.elapsed_ms >= 0
    assert poster.calls[0][0] == FAKE_WECOM_URL
    assert poster.calls[0][2] == 3.5  # 超时透传
    payload = poster.payload
    assert payload["msgtype"] == "markdown"
    content = payload["markdown"]["content"]
    assert content.startswith("[警报] 放量上破 · 贵州茅台(600519)")
    assert "放量 3.2 倍" in content


def test_wecom_errcode_nonzero_is_failure() -> None:
    poster = FakePoster(body={"errcode": 93000, "errmsg": "invalid webhook url"})
    channel = WecomChannel(webhook=FAKE_WECOM_URL, poster=poster)

    result = channel.send(_sample_message())

    assert result.ok is False
    assert "93000" in result.message
    assert "invalid webhook url" in result.message


def test_wecom_send_strict_raises_while_send_does_not() -> None:
    poster = FakePoster(body={"errcode": 45009, "errmsg": "api freq out of limit"})
    channel = WecomChannel(webhook=FAKE_WECOM_URL, poster=poster)

    with pytest.raises(PushError) as excinfo:
        channel.send_strict(_sample_message())
    assert excinfo.value.channel == "wecom"
    assert "45009" in str(excinfo.value)

    # 同样场景，send 不抛异常，只返回 ok=False
    fallback = channel.send(_sample_message())
    assert fallback.ok is False
    assert "45009" in fallback.message


def test_wecom_http_error_status_is_failure() -> None:
    poster = FakePoster(status=500, body={"errcode": 0, "errmsg": "ok"})
    channel = WecomChannel(webhook=FAKE_WECOM_URL, poster=poster)

    result = channel.send(_sample_message())

    assert result.ok is False
    assert "500" in result.message


# --------------------------------------------------------------------------- #
# 飞书
# --------------------------------------------------------------------------- #
def test_feishu_success_payload_is_text() -> None:
    poster = FakePoster(body={"code": 0, "msg": "success"})
    channel = FeishuChannel(webhook=FAKE_FEISHU_URL, poster=poster)

    result = channel.send(_sample_message())

    assert result.ok is True
    payload = poster.payload
    assert payload["msg_type"] == "text"
    text = payload["content"]["text"]
    assert text.startswith("[警报] 放量上破 · 贵州茅台(600519)")
    assert "放量 3.2 倍" in text


def test_feishu_with_secret_has_timestamp_and_sign() -> None:
    poster = FakePoster(body={"StatusCode": 0, "msg": "ok"})
    channel = FeishuChannel(webhook=FAKE_FEISHU_URL, secret="s3cr3t", poster=poster)

    result = channel.send(_sample_message())

    assert result.ok is True
    payload = poster.payload
    assert "timestamp" in payload
    assert payload["sign"]  # 非空字符串
    assert isinstance(payload["sign"], str)
    assert payload["sign"] == channels_mod.feishu_sign(payload["timestamp"], "s3cr3t")


def test_feishu_without_secret_has_no_sign() -> None:
    poster = FakePoster(body={"code": 0, "msg": "success"})
    channel = FeishuChannel(webhook=FAKE_FEISHU_URL, poster=poster, settings=_no_push_settings())

    channel.send(_sample_message())

    payload = poster.payload
    assert "sign" not in payload
    assert "timestamp" not in payload


def test_feishu_mentions_are_appended() -> None:
    poster = FakePoster(body={"code": 0, "msg": "success"})
    channel = FeishuChannel(webhook=FAKE_FEISHU_URL, poster=poster)
    msg = PushMessage(title="T", content="C", mentions=["ou_abc", "ou_def"])

    channel.send(msg)

    text = poster.payload["content"]["text"]
    assert '<at user_id="ou_abc">' in text
    assert '<at user_id="ou_def">' in text


def test_feishu_nonzero_code_is_failure() -> None:
    poster = FakePoster(body={"code": 19001, "msg": "sign invalid"})
    channel = FeishuChannel(webhook=FAKE_FEISHU_URL, poster=poster)

    result = channel.send(_sample_message())

    assert result.ok is False
    assert "19001" in result.message
    assert "sign invalid" in result.message


# --------------------------------------------------------------------------- #
# 网络异常
# --------------------------------------------------------------------------- #
def test_network_error_is_degraded_not_raised() -> None:
    poster = FakePoster(exc=RuntimeError("模拟网络错误：connection refused"))
    channel = WecomChannel(webhook=FAKE_WECOM_URL, poster=poster)

    result = channel.send(_sample_message())

    assert result.ok is False
    assert "模拟网络错误" in result.message


def test_network_error_strict_raises_push_error_with_cause() -> None:
    boom = RuntimeError("boom")
    channel = WecomChannel(webhook=FAKE_WECOM_URL, poster=FakePoster(exc=boom))

    with pytest.raises(PushError) as excinfo:
        channel.send_strict(_sample_message())
    assert excinfo.value.cause is boom
    assert "boom" in str(excinfo.value)


def test_missing_httpx_is_reported_as_result_not_import_crash() -> None:
    """即使 httpx 不可用，模块级 ``_post_json`` 也应返回清晰错误而非崩溃导入。"""

    def _no_httpx(
        url: str, payload: dict[str, Any], timeout: float = 10.0
    ) -> tuple[int, dict[str, Any]]:
        raise RuntimeError("未安装 httpx 库，请先 `pip install httpx`。")

    channel = WecomChannel(webhook=FAKE_WECOM_URL, poster=_no_httpx)
    result = channel.send(_sample_message())

    assert result.ok is False
    assert "httpx" in result.message


# --------------------------------------------------------------------------- #
# PushManager
# --------------------------------------------------------------------------- #
def test_manager_multi_channel_partial_failure() -> None:
    ok_channel = RecordingChannel(ok=True)
    bad_channel = RecordingChannel(ok=False)
    manager = PushManager(channels=[ok_channel, bad_channel], settings=_no_push_settings())

    results = manager.send(_sample_message())

    assert len(results) == 2
    assert PushManager.any_ok(results) is True
    assert PushManager.all_ok(results) is False
    assert results[0].ok is True
    assert results[1].ok is False
    assert len(ok_channel.messages) == 1  # 失败通道不影响成功通道


def test_manager_send_event_formats_message() -> None:
    ok_channel = RecordingChannel(ok=True)
    manager = PushManager(channels=[ok_channel], settings=_no_push_settings())
    event = AnomalyEvent(
        id="EVT-600519-20261009-001",
        ref_code="600519",
        ref_name="贵州茅台",
        type="volume_break_up",
        level="alert",
        detected_at=datetime(2026, 10, 9, 14, 32, 5),
        status="tentative",
        rule_id="volume_break",
    )

    results = manager.send_event(event, extra="主力净流入 +2.3 亿")

    assert len(results) == 1
    assert results[0].ok is True
    msg = ok_channel.messages[0]
    assert "600519" in msg.title
    assert "贵州茅台" in msg.title
    assert "警报" in msg.title
    assert "放量上破" in msg.title
    assert "EVT-600519-20261009-001" in msg.content
    assert "2026-10-09 14:32:05" in msg.content
    assert "主力净流入 +2.3 亿" in msg.content
    assert msg.level == "alert"


def test_manager_without_channels_returns_empty_list() -> None:
    manager = PushManager(channels=[], settings=_no_push_settings())

    assert manager.channels == []
    assert manager.send(_sample_message()) == []
    assert manager.send_event(
        AnomalyEvent(id="EVT-000001-20261009-001", ref_code="000001")
    ) == []


def test_manager_auto_assembles_configured_channels() -> None:
    settings = Settings(
        push=PushSettings(
            wecom_webhook=FAKE_WECOM_URL, feishu_webhook=FAKE_FEISHU_URL
        )
    )
    manager = PushManager(settings=settings)

    assert manager.channel_names() == ["wecom", "feishu"]
    assert manager.channels[0].webhook == FAKE_WECOM_URL  # type: ignore[union-attr]


def test_manager_disabled_push_assembles_nothing() -> None:
    settings = Settings(
        push=PushSettings(wecom_webhook=FAKE_WECOM_URL, enabled=False)
    )
    manager = PushManager(settings=settings)

    assert manager.channels == []


def test_manager_add_channel_dedupes_by_name() -> None:
    first = RecordingChannel(ok=True)
    manager = PushManager(channels=[first], settings=_no_push_settings())
    manager.add_channel(RecordingChannel(ok=False))

    assert len(manager.channels) == 1
    assert manager.channels[0] is not first


# --------------------------------------------------------------------------- #
# 抽象基类与模型
# --------------------------------------------------------------------------- #
def test_push_channel_is_abstract() -> None:
    with pytest.raises(TypeError):
        PushChannel()  # type: ignore[abstract]


def test_push_message_defaults() -> None:
    msg = PushMessage(title="标题")

    assert msg.content == ""
    assert msg.level == "info"
    assert msg.url == ""
    assert msg.mentions == []
    assert msg.normalized_level() == "info"
    assert PushMessage(title="t", level="weird").normalized_level() == "info"


def test_push_result_to_dict() -> None:
    result = PushResult(channel="wecom", ok=True, status_code=200, message="ok", elapsed_ms=12)

    assert result.to_dict() == {
        "channel": "wecom",
        "ok": True,
        "status_code": 200,
        "message": "ok",
        "elapsed_ms": 12,
    }


def test_push_settings_defaults_are_offline_safe() -> None:
    """新增字段必须有默认值，且 from_dict 兼容缺字段。"""
    settings = PushSettings.from_dict({})

    assert settings.wecom_webhook == ""
    assert settings.feishu_webhook == ""
    assert settings.feishu_secret == ""
    assert settings.timeout == 10.0
    assert settings.enabled is True

    full = PushSettings.from_dict(
        {
            "wecom_webhook": "w",
            "feishu_webhook": "f",
            "feishu_secret": "s",
            "timeout": 5,
            "enabled": False,
        }
    )
    assert full.timeout == 5.0
    assert full.enabled is False
    assert full.feishu_secret == "s"
