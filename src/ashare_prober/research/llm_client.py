"""LLM 客户端（T18）。

OpenAI 兼容封装（默认 DeepSeek 端点），用于研判层调用。
设计要点：
- **导入时不要求 api_key**；仅在使用时校验，缺失则抛清晰错误（不触发网络）。
- 请求重试（指数退避，最多 3 次）、``max_tokens`` 限额、强制 JSON 输出、低 temperature。
- 暴露统一方法 ``chat_json(system_prompt, user_prompt) -> dict``。
- 定义 ``LLMClient`` 抽象基类，便于单测用假客户端替换（不联网）。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from ashare_prober.config import LLMSettings, get_settings


class LLMError(RuntimeError):
    """LLM 调用相关错误（缺失密钥 / 重试耗尽 / 解析失败）。"""


_MAX_RETRIES = 3
_BASE_BACKOFF = 0.5  # 秒


def _backoff(attempt: int) -> float:
    """指数退避：0.5s, 1.0s, 2.0s ... 上限 4s。"""
    return min(_BASE_BACKOFF * (2 ** (attempt - 1)), 4.0)


class LLMClient(ABC):
    """LLM 客户端协议/基类：所有客户端（真实或测试替身）均实现 ``chat_json``。"""

    @abstractmethod
    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        """发起一次对话并解析为 dict；失败抛 ``LLMError``。"""
        raise NotImplementedError


class OpenAICompatibleClient(LLMClient):
    """OpenAI 兼容 LLM 客户端（DeepSeek / OpenAI / 本地兼容端点）。

    ``openai`` 库在首次调用时**惰性导入**，避免无依赖环境下模块导入失败。
    """

    def __init__(self, settings: Optional[LLMSettings] = None) -> None:
        self.settings: LLMSettings = settings or get_settings().llm
        self._client: Any = None  # 惰性持有的底层 openai 客户端

    @property
    def model(self) -> str:
        """实际使用的模型名（便于落库追溯）。"""
        return self.settings.model

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.settings.api_key:
            raise LLMError(
                "LLM api_key 未配置：请在 config.toml 的 [llm] 中填写 api_key，"
                "或传入 LLMSettings(api_key=...)。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise LLMError("未安装 openai 库，请先 `pip install openai`。") from exc
        self._client = OpenAI(
            base_url=self.settings.base_url, api_key=self.settings.api_key
        )
        return self._client

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        client = self._ensure_client()
        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = client.chat.completions.create(
                    model=self.settings.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self.settings.temperature,
                    max_tokens=self.settings.max_tokens,
                    timeout=self.settings.timeout,
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content or "{}"
                return json.loads(content)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一容错后重试
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    time.sleep(_backoff(attempt))
                    continue
                break
        raise LLMError(
            f"LLM 调用失败（已重试 {_MAX_RETRIES} 次）：{last_exc}"
        ) from last_exc
