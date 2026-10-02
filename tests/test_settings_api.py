"""设置 API / 看板设置卡片测试（全离线）。

覆盖：
- ``viewer``：``start_viewer_thread`` 起真服务（127.0.0.1、port=0 系统分配），
  验证 ``/api/settings`` 三端点的状态码、响应形状与部分成功语义；
  **秘密脱敏红线**：响应与任何日志输出都不得出现明文 secret。
- 模板：渲染结果含「⚙️ 设置」卡片、API 路径、「恢复默认」与静态模式提示文案。
- 真实 ``Store`` 落库集成：数据层契约（``store.get_setting`` 等四个方法 +
  ``data/appsettings`` 模块）由并行开发提供，缺失时自动 skip，落地后即生效。

API 层测试使用**契约忠实**的替身存储（``FakeSettingsStore``）；``appsettings``
模块缺失时注入一个契约等价的桩模块（``_make_stub_appsettings``），保证任何
阶段都能验证 API 形状，0 failed。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import types
import urllib.error
import urllib.request
from dataclasses import asdict
from typing import Any, Optional

import pytest

from ashare_prober.config import (
    LLMSettings,
    PushSettings,
    ScheduleSettings,
    Settings,
)
from ashare_prober.dashboard.models import MarketDashboard
from ashare_prober.presentation.render import Render
from ashare_prober.presentation.viewer import mask_secret, start_viewer_thread

# --------------------------------------------------------------------------- #
# 固定测试配置（注入 base_settings，保证 GET 初始值确定性，不依赖真实 config.toml）
# --------------------------------------------------------------------------- #
_PLAIN_LLM_KEY = "sk-inject-PLAIN-1234567890abcdef"
_PLAIN_WECOM_HOOK = "https://wecom.example.com/hook/PLAINKEY9"

BASE_SETTINGS = Settings(
    llm=LLMSettings(
        base_url="https://api.example.com/v1",
        api_key=_PLAIN_LLM_KEY,
        model="test-model",
        temperature=0.2,
    ),
    push=PushSettings(
        wecom_webhook=_PLAIN_WECOM_HOOK,
        feishu_webhook="",
        feishu_secret="",
        enabled=True,
    ),
    schedule=ScheduleSettings(
        enabled=True,
        scan_time="15:10",
        am_scan_time="",
        push=True,
        refresh_dashboard=True,
    ),
)


# --------------------------------------------------------------------------- #
# 测试替身：契约忠实的设置存储（与 data.store 契约签名一致）
# --------------------------------------------------------------------------- #
class FakeSettingsStore:
    """设置契约四方法的内存实现（线程安全；与 watchlist 替身互不干扰）。"""

    def __init__(self) -> None:
        self._settings: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get_setting(self, key: str) -> Optional[dict]:
        with self._lock:
            row = self._settings.get(str(key))
            return dict(row) if row is not None else None

    def set_setting(self, key: str, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("设置必须是 JSON 对象")
        with self._lock:
            self._settings[str(key)] = dict(data)

    def delete_setting(self, key: str) -> bool:
        with self._lock:
            return self._settings.pop(str(key), None) is not None

    def list_setting_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._settings.keys())


# --------------------------------------------------------------------------- #
# appsettings 桩模块（仅当数据层并行代码未落地时注入，契约等价）
# --------------------------------------------------------------------------- #
_STUB_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _stub_validate_section(section: str, data: dict) -> tuple[dict, list[str]]:
    """桩校验器：与规格示例同口径（时间 HH:MM、webhook 必须 https、温度数字）。"""
    errors: list[str] = []
    if section == "llm":
        base_url = str(data.get("base_url", "") or "")
        if base_url and not base_url.startswith(("http://", "https://")):
            errors.append("base_url 必须是 http(s) 地址")
        temperature = data.get("temperature")
        if temperature is not None:
            try:
                float(temperature)
            except (TypeError, ValueError):
                errors.append("temperature 必须为数字")
    elif section == "push":
        for name in ("wecom_webhook", "feishu_webhook"):
            hook = str(data.get(name, "") or "")
            if hook and not hook.startswith("https://"):
                errors.append(f"{name} 必须是 https 地址")
    elif section == "schedule":
        for name in ("scan_time", "am_scan_time"):
            value = str(data.get(name, "") or "")
            if value and not _STUB_TIME_RE.match(value):
                errors.append(f"{name} 必须为 HH:MM 格式")
    return {}, errors


def _make_stub_appsettings() -> types.ModuleType:
    """构造契约等价的 ``ashare_prober.data.appsettings`` 桩模块。"""
    from ashare_prober.config import get_settings

    mod = types.ModuleType("ashare_prober.data.appsettings")
    mod.EDITABLE_SECTIONS = ("llm", "push", "schedule")

    def resolve_runtime_settings(store: Any, settings: Any = None) -> Settings:
        base = settings or get_settings()

        def merged(section_cls: Any, current: Any, key: str) -> Any:
            raw = asdict(current)
            override = store.get_setting(key)
            if isinstance(override, dict):
                raw.update(override)
            return section_cls.from_dict(raw)

        return Settings(
            data_dir=base.data_dir,
            db_path=base.db_path,
            llm=merged(LLMSettings, base.llm, "llm"),
            push=merged(PushSettings, base.push, "push"),
            schedule=merged(ScheduleSettings, base.schedule, "schedule"),
            watchlist=base.watchlist,
        )

    mod.validate_section = _stub_validate_section
    mod.resolve_runtime_settings = resolve_runtime_settings
    return mod


def _appsettings_ready() -> bool:
    """真实数据层 ``appsettings`` 是否已具备契约三件套。"""
    try:
        from ashare_prober.data import appsettings as mod
    except Exception:  # noqa: BLE001 - 并行开发中未落地
        return False
    return all(
        hasattr(mod, name)
        for name in ("EDITABLE_SECTIONS", "validate_section", "resolve_runtime_settings")
    )


# --------------------------------------------------------------------------- #
# HTTP 助手
# --------------------------------------------------------------------------- #
def _get(base: str, path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_raw(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(base: str, path: str, payload: Optional[dict]) -> tuple[int, Any]:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    return _post_raw(req)


def _post_raw(req: urllib.request.Request) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_bad_json(base: str, path: str) -> tuple[int, Any]:
    req = urllib.request.Request(
        base + path, data=b"{not-json", headers={"Content-Type": "application/json"}, method="POST"
    )
    return _post_raw(req)


# --------------------------------------------------------------------------- #
# 服务 fixture
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cfg_viewer(monkeypatch, tmp_path):
    """起真服务（port=0），返回命名空间（base/store）。

    数据层 ``appsettings`` 未落地时注入契约等价桩模块（测试结束自动还原），
    使 API 形状用例在任何并行阶段都可运行。
    """
    if not _appsettings_ready():
        monkeypatch.setitem(sys.modules, "ashare_prober.data.appsettings", _make_stub_appsettings())
    store = FakeSettingsStore()
    httpd, thread = start_viewer_thread(
        tmp_path, port=0, store=store, settings=BASE_SETTINGS
    )
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield type("CfgViewerCtx", (), {"base": base, "store": store})()
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


_SCHEDULE_VALID = {
    "enabled": True,
    "scan_time": "15:30",
    "am_scan_time": "",
    "push": True,
    "refresh_dashboard": True,
}


# --------------------------------------------------------------------------- #
# mask_secret 单元
# --------------------------------------------------------------------------- #
class TestMaskSecret:
    """脱敏规则：空 → 空；≤8 → ****；否则前6…后4。"""

    def test_empty(self) -> None:
        assert mask_secret("") == ""
        assert mask_secret(None) == ""

    def test_short_is_asterisks(self) -> None:
        assert mask_secret("abc") == "****"
        assert mask_secret("12345678") == "****"

    def test_long_keeps_head_and_tail(self) -> None:
        assert mask_secret("sk-inject-PLAIN-1234567890abcdef") == "sk-inj…cdef"


# --------------------------------------------------------------------------- #
# GET /api/settings
# --------------------------------------------------------------------------- #
class TestSettingsGet:
    """GET：生效值 + 秘密脱敏形状。"""

    def test_get_returns_three_sections_from_base_settings(self, cfg_viewer) -> None:
        status, body = _get(cfg_viewer.base, "/api/settings")
        assert status == 200
        assert body["ok"] is True
        sections = body["sections"]
        assert set(sections) == {"llm", "push", "schedule"}
        assert sections["llm"]["base_url"] == "https://api.example.com/v1"
        assert sections["llm"]["model"] == "test-model"
        assert sections["llm"]["temperature"] == 0.2
        assert sections["schedule"]["scan_time"] == "15:10"
        assert sections["schedule"]["enabled"] is True

    def test_get_masks_secrets_with_set_flag(self, cfg_viewer) -> None:
        _, body = _get(cfg_viewer.base, "/api/settings")
        sections = body["sections"]
        assert sections["llm"]["api_key"] == {"masked": "sk-inj…cdef", "set": True}
        assert sections["push"]["wecom_webhook"]["set"] is True
        # 空 webhook：masked 为空串且 set=False
        assert sections["push"]["feishu_webhook"] == {"masked": "", "set": False}
        assert sections["push"]["feishu_secret"] == {"masked": "", "set": False}
        # 非敏感字段不是包装形状
        assert isinstance(sections["llm"]["base_url"], str)
        assert isinstance(sections["schedule"]["push"], bool)

    def test_get_response_never_contains_plaintext_secrets(self, cfg_viewer) -> None:
        status, raw = _get_raw(cfg_viewer.base, "/api/settings")
        assert status == 200
        assert _PLAIN_LLM_KEY not in raw
        assert _PLAIN_WECOM_HOOK not in raw


# --------------------------------------------------------------------------- #
# POST /api/settings
# --------------------------------------------------------------------------- #
class TestSettingsSave:
    """POST：合法段落库明文；非法段不落库；部分成功语义。"""

    def test_post_valid_llm_persists_plaintext_and_changes_effective(self, cfg_viewer) -> None:
        payload = {
            "llm": {
                "base_url": "https://new.example.com/v1",
                "api_key": "sk-NEW-PLAIN-7777",
                "model": "new-model",
                "temperature": 0.5,
            }
        }
        status, body = _post(cfg_viewer.base, "/api/settings", payload)
        assert status == 200
        assert body == {"ok": True, "saved": ["llm"], "failed": {}}
        # 落库的是**原始明文**
        stored = cfg_viewer.store.get_setting("llm")
        assert stored is not None
        assert stored["api_key"] == "sk-NEW-PLAIN-7777"
        assert stored["base_url"] == "https://new.example.com/v1"
        # 生效值随之改变；响应仍不回明文
        status, raw = _get_raw(cfg_viewer.base, "/api/settings")
        assert status == 200
        assert "https://new.example.com/v1" in raw
        assert "sk-NEW-PLAIN-7777" not in raw
        assert _PLAIN_LLM_KEY not in raw
        _, body = _get(cfg_viewer.base, "/api/settings")
        assert body["sections"]["llm"]["api_key"]["masked"].startswith("sk-NEW")
        assert body["sections"]["llm"]["api_key"]["set"] is True

    def test_post_partial_failure_keeps_valid_sections(self, cfg_viewer) -> None:
        payload = {
            "schedule": dict(_SCHEDULE_VALID),
            "push": {"wecom_webhook": "http://bad.example/hook", "enabled": True},
        }
        status, body = _post(cfg_viewer.base, "/api/settings", payload)
        assert status == 400
        assert body["ok"] is False
        assert body["saved"] == ["schedule"]  # 合法段仍落库（部分成功语义）
        assert body["failed"]["push"]  # 非法段给出逐段 errors
        assert isinstance(body["failed"]["push"], list) and body["failed"]["push"]
        assert cfg_viewer.store.get_setting("schedule") is not None
        assert cfg_viewer.store.get_setting("push") is None

    def test_post_invalid_scan_time_rejected(self, cfg_viewer) -> None:
        bad = dict(_SCHEDULE_VALID, scan_time="905")
        status, body = _post(cfg_viewer.base, "/api/settings", {"schedule": bad})
        assert status == 400
        assert body["ok"] is False
        assert body["failed"]["schedule"]
        assert cfg_viewer.store.get_setting("schedule") is None

    def test_post_non_dict_section_rejected(self, cfg_viewer) -> None:
        status, body = _post(cfg_viewer.base, "/api/settings", {"llm": "oops"})
        assert status == 400
        assert body["failed"]["llm"]

    def test_post_unknown_section_key_rejected(self, cfg_viewer) -> None:
        status, body = _post(cfg_viewer.base, "/api/settings", {"nope": {"x": 1}})
        assert status == 400
        assert body["ok"] is False
        assert body["failed"]["nope"]

    def test_post_bad_json_returns_400(self, cfg_viewer) -> None:
        status, body = _post_bad_json(cfg_viewer.base, "/api/settings")
        assert status == 400
        assert body["ok"] is False


# --------------------------------------------------------------------------- #
# POST /api/settings/reset
# --------------------------------------------------------------------------- #
class TestSettingsReset:
    """reset：删除 DB 覆盖后 GET 回落到配置兜底值。"""

    def test_reset_restores_config_fallback(self, cfg_viewer) -> None:
        # 先保存覆盖
        status, _ = _post(
            cfg_viewer.base, "/api/settings", {"schedule": dict(_SCHEDULE_VALID)}
        )
        assert status == 200
        _, body = _get(cfg_viewer.base, "/api/settings")
        assert body["sections"]["schedule"]["scan_time"] == "15:30"
        # reset
        status, body = _post(cfg_viewer.base, "/api/settings/reset", {"section": "schedule"})
        assert status == 200
        assert body == {"ok": True}
        assert cfg_viewer.store.get_setting("schedule") is None
        # GET 回落到 base_settings 兜底值
        _, body = _get(cfg_viewer.base, "/api/settings")
        assert body["sections"]["schedule"]["scan_time"] == "15:10"

    def test_reset_unknown_section_returns_400(self, cfg_viewer) -> None:
        status, body = _post(cfg_viewer.base, "/api/settings/reset", {"section": "nope"})
        assert status == 400
        assert body["ok"] is False

    def test_reset_bad_json_returns_400(self, cfg_viewer) -> None:
        status, _ = _post_bad_json(cfg_viewer.base, "/api/settings/reset")
        assert status == 400


# --------------------------------------------------------------------------- #
# 路由兜底与秘密纪律
# --------------------------------------------------------------------------- #
class TestRoutingAndSecretDiscipline:
    """未知 API 404 JSON；任何日志输出不得包含明文 secret。"""

    def test_unknown_api_post_returns_404_json(self, cfg_viewer) -> None:
        status, body = _post(cfg_viewer.base, "/api/other", {"x": 1})
        assert status == 404
        assert body["ok"] is False

    def test_no_plaintext_secret_in_stdout_stderr(self, cfg_viewer, capsys) -> None:
        """起服务 + 存取秘密的整个过程中，stdout/stderr 不出现明文 api_key。"""
        fresh_key = "sk-FRESH-PLAIN-abcdef123456"
        status, _ = _post(
            cfg_viewer.base,
            "/api/settings",
            {
                "llm": {
                    "base_url": "https://fresh.example.com/v1",
                    "api_key": fresh_key,
                    "model": "fresh-model",
                    "temperature": 0.1,
                }
            },
        )
        assert status == 200
        _get_raw(cfg_viewer.base, "/api/settings")
        _post(cfg_viewer.base, "/api/settings/reset", {"section": "llm"})
        captured = capsys.readouterr()
        assert fresh_key not in captured.out
        assert fresh_key not in captured.err
        assert _PLAIN_LLM_KEY not in captured.out
        assert _PLAIN_LLM_KEY not in captured.err


# --------------------------------------------------------------------------- #
# 模板：设置卡片
# --------------------------------------------------------------------------- #
class TestDashboardTemplateSettings:
    """看板模板渲染结果包含设置卡片要素与静态降级提示。"""

    def _render(self) -> str:
        return Render().render_dashboard(MarketDashboard())

    def test_contains_settings_card_elements(self) -> None:
        html = self._render()
        assert "设置" in html
        assert "LLM / 推送 / 调度" in html
        assert "/api/settings" in html
        assert "恢复默认" in html
        assert "保存" in html

    def test_contains_static_fallback_note(self) -> None:
        html = self._render()
        assert "静态模式为只读" in html
        assert "prober serve" in html

    def test_secret_inputs_have_autocomplete_off_and_password(self) -> None:
        html = self._render()
        assert 'autocomplete="off"' in html
        assert 'type="password"' in html
        assert 'data-secret="1"' in html

    def test_card_renders_static_summary(self) -> None:
        html = self._render()
        assert "cfg-static" in html
        assert "15:05" in html  # 静态摘要展示默认扫描时刻


# --------------------------------------------------------------------------- #
# 真实 Store 集成（数据层并行开发：契约就绪后自动生效，否则 skip）
# --------------------------------------------------------------------------- #
class TestRealStoreSettingsIntegration:
    """用真实 tmp ``Store`` 验证明文落库与生效值回流。"""

    def test_settings_roundtrip_with_real_store(self, tmp_path) -> None:
        from ashare_prober.data.store import Store

        store_ready = all(
            hasattr(Store, name)
            for name in ("get_setting", "set_setting", "delete_setting", "list_setting_keys")
        )
        if not (store_ready and _appsettings_ready()):
            pytest.skip("数据层设置契约尚未落地（并行开发中）")
        store = Store(tmp_path / "settings.db")
        httpd, thread = start_viewer_thread(
            tmp_path / "out", port=0, store=store, settings=BASE_SETTINGS
        )
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            # 保存 → 真实 Store 落明文
            status, body = _post(
                base, "/api/settings", {"schedule": dict(_SCHEDULE_VALID)}
            )
            assert status == 200
            assert body["saved"] == ["schedule"]
            stored = store.get_setting("schedule")
            assert stored is not None
            assert stored["scan_time"] == "15:30"
            # GET 生效值来自 DB 覆盖
            _, body = _get(base, "/api/settings")
            assert body["sections"]["schedule"]["scan_time"] == "15:30"
            # reset → 回落配置兜底
            status, body = _post(base, "/api/settings/reset", {"section": "schedule"})
            assert status == 200 and body == {"ok": True}
            assert store.get_setting("schedule") is None
            _, body = _get(base, "/api/settings")
            assert body["sections"]["schedule"]["scan_time"] == "15:10"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
