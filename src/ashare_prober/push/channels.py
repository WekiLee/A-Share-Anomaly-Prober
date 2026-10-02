"""推送通道实现：企业微信群机器人 / 飞书自定义机器人（T24）。

设计要点：
- **推送失败不阻断主流程**：``PushChannel.send`` 是模板方法，内部 try/except
  包住 ``send_strict``，把异常降级为 ``ok=False`` 的 ``PushResult``；需要严格
  语义的调用方直接用 ``send_strict``（失败抛 ``PushError``）。
- **惰性导入 httpx**：模块 import 不依赖 httpx 已安装，缺失时仅在实际推送时报错。
- **可注入 / 可 monkeypatch**：真实 HTTP 发送收敛在模块级函数 ``_post_json``，
  通道通过 ``poster`` 参数或 monkeypatch 该函数即可完全离线测试。
- **webhook 从配置读取**：构造时未传入则从 ``get_settings().push`` 取；
  仍为空时 ``send`` 返回 ``ok=False``（不抛异常）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ashare_prober.config import get_settings
from ashare_prober.push.models import PushError, PushMessage, PushResult

# HTTP 发送函数签名：``(url, payload, timeout) -> (status_code, response_body)``。
PostFunc = Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]]


def _post_json(
    url: str, payload: dict[str, Any], timeout: float = 10.0
) -> tuple[int, dict[str, Any]]:
    """向 ``url`` POST 一个 JSON 体，返回 ``(状态码, 响应 dict)``。

    这是推送层唯一的真实网络出口；测试可 monkeypatch 本函数实现离线验证。

    Args:
        url: 目标 webhook 地址。
        payload: 待发送的 JSON 体。
        timeout: 超时（秒）。

    Returns:
        ``(status_code, body_dict)``；响应非 JSON 时 ``body`` 为 ``{}``。

    Raises:
        RuntimeError: 未安装 httpx。
        Exception: 任何网络层异常（由调用方统一降级处理）。
    """
    try:
        import httpx  # 惰性导入：模块 import 阶段不要求 httpx 存在
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        raise RuntimeError("未安装 httpx 库，请先 `pip install httpx`。") from exc

    resp = httpx.post(url, json=payload, timeout=timeout)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应一律视为空 body
        body = {}
    if not isinstance(body, dict):
        body = {"raw": body}
    return int(resp.status_code), body


def _elapsed_ms(started: float) -> int:
    """由 ``time.perf_counter()`` 起点计算耗时（毫秒）。"""
    return int((time.perf_counter() - started) * 1000)


def _as_int(value: Any, default: int = -1) -> int:
    """把响应字段安全转为 int（飞书 / 企业微信返回码可能是字符串）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def feishu_sign(timestamp: str, secret: str) -> str:
    """按飞书规范生成签名：``base64(hmac_sha256(key=f"{ts}\\n{secret}", msg=b""))``。"""
    key = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class PushChannel(ABC):
    """推送通道抽象基类（模板方法模式）。

    子类只需实现 ``send_strict``（失败抛 ``PushError``）；对外暴露的 ``send``
    由基类实现，保证「推送失败不向外抛异常」，仅返回 ``ok=False`` 的结果。
    """

    name: str = "base"  # 通道名（子类覆盖）

    def __init__(self, timeout: float = 10.0, poster: PostFunc | None = None) -> None:
        """
        Args:
            timeout: 单次请求超时（秒）。
            poster: 可选的 HTTP 发送函数；默认使用模块级 ``_post_json``。
        """
        self.timeout: float = timeout
        self._poster: PostFunc | None = poster

    # ------------------------------------------------------------------ #
    # 模板方法：对外唯一入口（不抛异常）
    # ------------------------------------------------------------------ #
    def send(self, msg: PushMessage) -> PushResult:
        """推送一条消息；任何失败都降级为 ``ok=False`` 的 ``PushResult``。"""
        started = time.perf_counter()
        try:
            result = self.send_strict(msg)
        except PushError as exc:
            result = PushResult(
                channel=self.name,
                ok=False,
                status_code=exc.status_code,
                message=str(exc),
                elapsed_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：推送绝不影响主流程
            result = PushResult(
                channel=self.name,
                ok=False,
                message=f"{self.name} 推送异常：{exc}",
                elapsed_ms=_elapsed_ms(started),
            )
        if result.elapsed_ms <= 0:
            result = result.model_copy(update={"elapsed_ms": _elapsed_ms(started)})
        return result

    # ------------------------------------------------------------------ #
    # 子类钩子：严格语义（失败抛 PushError）
    # ------------------------------------------------------------------ #
    @abstractmethod
    def send_strict(self, msg: PushMessage) -> PushResult:
        """执行推送；失败抛 ``PushError``。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 公共工具
    # ------------------------------------------------------------------ #
    def _post(self, url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """发起一次 POST：优先用注入的 poster，否则用模块级 ``_post_json``。"""
        poster: PostFunc = self._poster or _post_json
        return poster(url, payload, self.timeout)


class WecomChannel(PushChannel):
    """企业微信群机器人（markdown 消息）。

    请求体：``{"msgtype": "markdown", "markdown": {"content": "<title>\\n\\n<body>"}}``
    成功判定：HTTP 2xx 且响应 ``errcode == 0``。
    """

    name = "wecom"

    def __init__(
        self,
        webhook: str = "",
        timeout: float = 10.0,
        poster: PostFunc | None = None,
        settings: Any | None = None,
    ) -> None:
        """
        Args:
            webhook: 机器人 webhook；留空则从配置读取。
            timeout: 请求超时（秒）。
            poster: 可选 HTTP 发送函数（测试注入）。
            settings: 可选配置对象；留空则使用 ``get_settings()``。
        """
        super().__init__(timeout=timeout, poster=poster)
        self.webhook: str = webhook
        self._settings: Any | None = settings

    def resolve_webhook(self) -> str:
        """返回生效的 webhook：构造参数优先，其次配置。"""
        if self.webhook:
            return self.webhook
        try:
            cfg = self._settings if self._settings is not None else get_settings()
            return str(getattr(cfg.push, "wecom_webhook", "") or "")
        except Exception:  # noqa: BLE001 - 配置不可用时按未配置处理
            return ""

    def build_payload(self, msg: PushMessage) -> dict[str, Any]:
        """构造企业微信请求体。"""
        body = msg.content or ""
        if msg.url:
            link = f"[查看详情]({msg.url})"
            body = f"{body}\n\n{link}" if body else link
        if msg.mentions:
            at = " ".join(f"<@{m}>" for m in msg.mentions)
            body = f"{body}\n\n{at}" if body else at
        content = f"{msg.title}\n\n{body}" if body else msg.title
        return {"msgtype": "markdown", "markdown": {"content": content}}

    def send_strict(self, msg: PushMessage) -> PushResult:
        """推送到企业微信；失败抛 ``PushError``。"""
        started = time.perf_counter()
        url = self.resolve_webhook()
        if not url:
            raise PushError(
                "未配置企业微信 webhook：请在 config.toml 的 [push] 段填写 "
                "wecom_webhook，或在构造 WecomChannel 时传入。",
                channel=self.name,
            )

        payload = self.build_payload(msg)
        try:
            status, body = self._post(url, payload)
        except PushError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一包装
            raise PushError(
                f"企业微信推送网络错误：{exc}", channel=self.name, cause=exc
            ) from exc

        if status >= 400:
            raise PushError(
                f"企业微信推送 HTTP 失败：status={status} body={body}",
                channel=self.name,
                status_code=status,
            )

        errcode = _as_int(body.get("errcode"), default=-1)
        errmsg = str(body.get("errmsg", "") or "")
        if "errcode" not in body:
            raise PushError(
                f"企业微信响应缺少 errcode 字段：{body}",
                channel=self.name,
                status_code=status,
            )
        if errcode != 0:
            raise PushError(
                f"企业微信推送失败：errcode={errcode} errmsg={errmsg}",
                channel=self.name,
                status_code=status,
            )
        return PushResult(
            channel=self.name,
            ok=True,
            status_code=status,
            message=errmsg or "ok",
            elapsed_ms=_elapsed_ms(started),
        )


class FeishuChannel(PushChannel):
    """飞书自定义机器人（text 消息，可选签名校验）。

    请求体：``{"msg_type": "text", "content": {"text": "<title>\\n<body>"}}``
    配置了 ``secret`` 时额外携带 ``timestamp`` 与 ``sign``。
    成功判定：响应 ``code == 0`` 或 ``StatusCode == 0``。
    """

    name = "feishu"

    def __init__(
        self,
        webhook: str = "",
        secret: str = "",
        timeout: float = 10.0,
        poster: PostFunc | None = None,
        settings: Any | None = None,
    ) -> None:
        """
        Args:
            webhook: 机器人 webhook；留空则从配置读取。
            secret: 签名密钥；留空则从配置的 ``feishu_secret`` 读取。
            timeout: 请求超时（秒）。
            poster: 可选 HTTP 发送函数（测试注入）。
            settings: 可选配置对象；留空则使用 ``get_settings()``。
        """
        super().__init__(timeout=timeout, poster=poster)
        self.webhook: str = webhook
        self.secret: str = secret
        self._settings: Any | None = settings

    def resolve_webhook(self) -> str:
        """返回生效的 webhook：构造参数优先，其次配置。"""
        if self.webhook:
            return self.webhook
        try:
            cfg = self._settings if self._settings is not None else get_settings()
            return str(getattr(cfg.push, "feishu_webhook", "") or "")
        except Exception:  # noqa: BLE001 - 配置不可用时按未配置处理
            return ""

    def resolve_secret(self) -> str:
        """返回生效的签名密钥：构造参数优先，其次配置。"""
        if self.secret:
            return self.secret
        try:
            cfg = self._settings if self._settings is not None else get_settings()
            return str(getattr(cfg.push, "feishu_secret", "") or "")
        except Exception:  # noqa: BLE001 - 配置不可用时按未配置处理
            return ""

    def build_payload(self, msg: PushMessage) -> dict[str, Any]:
        """构造飞书请求体（含可选签名）。"""
        text = f"{msg.title}\n{msg.content}" if msg.content else msg.title
        if msg.url:
            text = f"{text}\n{msg.url}"
        if msg.mentions:
            at = " ".join(f'<at user_id="{m}">' for m in msg.mentions)
            text = f"{text}\n{at}"

        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        secret = self.resolve_secret()
        if secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = feishu_sign(timestamp, secret)
        return payload

    def send_strict(self, msg: PushMessage) -> PushResult:
        """推送到飞书；失败抛 ``PushError``。"""
        started = time.perf_counter()
        url = self.resolve_webhook()
        if not url:
            raise PushError(
                "未配置飞书 webhook：请在 config.toml 的 [push] 段填写 "
                "feishu_webhook，或在构造 FeishuChannel 时传入。",
                channel=self.name,
            )

        payload = self.build_payload(msg)
        try:
            status, body = self._post(url, payload)
        except PushError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一包装
            raise PushError(
                f"飞书推送网络错误：{exc}", channel=self.name, cause=exc
            ) from exc

        if status >= 400:
            raise PushError(
                f"飞书推送 HTTP 失败：status={status} body={body}",
                channel=self.name,
                status_code=status,
            )

        code = body.get("code", body.get("StatusCode"))
        if code is None:
            raise PushError(
                f"飞书响应缺少 code 字段：{body}",
                channel=self.name,
                status_code=status,
            )
        if _as_int(code, default=-1) != 0:
            raise PushError(
                f"飞书推送失败：code={code} msg={body.get('msg', '')}",
                channel=self.name,
                status_code=status,
            )
        return PushResult(
            channel=self.name,
            ok=True,
            status_code=status,
            message=str(body.get("msg", "") or "ok"),
            elapsed_ms=_elapsed_ms(started),
        )
