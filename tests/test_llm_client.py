"""LLM 客户端单测（T18）。

- 假 LLMClient 返回预设 dict，验证 chat_json 接口可用。
- 真实 OpenAICompatibleClient 在 api_key 缺失时抛清晰 LLMError（不联网）。
"""

from __future__ import annotations

import pytest

from ashare_prober.config import LLMSettings
from ashare_prober.research.llm_client import (
    LLMClient,
    LLMError,
    OpenAICompatibleClient,
)


class FakeLLMClient(LLMClient):
    """返回预设 dict 的假客户端（不联网）。"""

    model = "fake-model"

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload if payload is not None else {
            "bull_view": "利好",
            "bear_view": "风险",
            "confidence": 0.7,
            "conclusion": "综合结论",
        }
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.calls.append((system_prompt, user_prompt))
        return dict(self.payload)


def test_fake_client_returns_payload() -> None:
    client = FakeLLMClient()
    out = client.chat_json("sys", "usr")
    assert out["confidence"] == 0.7
    assert client.calls == [("sys", "usr")]


def test_real_client_missing_key_raises() -> None:
    # 未配置 api_key：调用即抛清晰错误，不涉及网络。
    client = OpenAICompatibleClient(LLMSettings(api_key=""))
    with pytest.raises(LLMError):
        client.chat_json("sys", "usr")


def test_real_client_model_property() -> None:
    client = OpenAICompatibleClient(LLMSettings(api_key="x", model="deepseek-chat"))
    assert client.model == "deepseek-chat"
