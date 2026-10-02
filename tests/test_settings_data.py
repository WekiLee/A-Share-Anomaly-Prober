"""页面自助配置（app_settings）数据层单测（全离线）。

覆盖：
- ``Store`` 新表 ``app_settings`` 四方法（get/set/delete/list + JSON 损坏降级）；
- ``validate_section`` 三段校验（合法值 / 未知键忽略 / 类型错误 / 时刻补零 /
  http webhook 拒绝 / 空 webhook 清空合法 / 温度越界拒绝 / 未知段拒绝）；
- ``resolve_runtime_settings``：无覆盖一致 / 字段级覆盖生效 / 损坏与非法值
  忽略该段不抛 / 传入自定义 base 时不读全局单例；
- ``push_events(store=...)`` 接线：DB 配 webhook → 通道实际装配并出网
  （monkeypatch ``_post_json``）；未配置 → 空结果不抛；
- ``SchedulerService.run_once`` 生效配置接线：DB 覆盖 ``schedule.push=False``
  → ``push_events`` 不执行；
- 秘密纪律：resolve / validate 的日志不出现明文 secret。

**全部离线**：tmp_path 下的临时 SQLite，推送出网用 monkeypatch 拦截。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ashare_prober.config import LLMSettings, Settings
from ashare_prober.data.appsettings import (
    EDITABLE_SECTIONS,
    resolve_runtime_settings,
    validate_section,
)
from ashare_prober.data.store import Store
from ashare_prober.scheduler import service as scheduler_service
from ashare_prober.scheduler.jobs import push_events

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-hook"
SECRET_KEY = "sk-secret-value-do-not-log"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
def _fake_event() -> SimpleNamespace:
    """构造推送格式化所需字段齐全的假异动事件（不发网络）。"""
    return SimpleNamespace(
        id="EVT-600519-20261009-001",
        scope="stock",
        ref_code="600519",
        ref_name="贵州茅台",
        type="volume_break_up",
        level="alert",
        status="tentative",
        rule_id="R-VOL-BREAK",
        detected_at=datetime(2026, 10, 9, 15, 5, 0),
    )


@pytest.fixture()
def store(tmp_path) -> Store:
    """tmp_path 下的临时 Store（全离线）。"""
    return Store(tmp_path / "settings.db")


# --------------------------------------------------------------------------- #
# Store.app_settings 四方法
# --------------------------------------------------------------------------- #
class TestStoreAppSettings:
    def test_set_get_roundtrip(self, store: Store) -> None:
        store.set_setting("llm", {"api_key": "k1", "model": "m1"})
        assert store.get_setting("llm") == {"api_key": "k1", "model": "m1"}

    def test_get_missing_returns_none(self, store: Store) -> None:
        assert store.get_setting("llm") is None
        assert store.get_setting("push") is None

    def test_set_upsert_overwrites(self, store: Store) -> None:
        store.set_setting("push", {"enabled": True})
        store.set_setting("push", {"enabled": False, "timeout": 5.0})
        assert store.get_setting("push") == {"enabled": False, "timeout": 5.0}
        assert store.list_setting_keys() == ["push"]

    def test_delete_returns_existence(self, store: Store) -> None:
        assert store.delete_setting("llm") is False
        store.set_setting("llm", {"model": "m"})
        assert store.delete_setting("llm") is True
        assert store.get_setting("llm") is None
        assert store.delete_setting("llm") is False

    def test_list_keys_sorted(self, store: Store) -> None:
        assert store.list_setting_keys() == []
        store.set_setting("schedule", {"push": True})
        store.set_setting("llm", {"model": "m"})
        store.set_setting("push", {"enabled": True})
        assert store.list_setting_keys() == ["llm", "push", "schedule"]

    def test_corrupted_json_returns_none_not_raise(self, store: Store) -> None:
        # 直接写坏 JSON，模拟 DB 数据损坏。
        store._conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("llm", "{not-valid-json", "2026-10-09T15:05:00"),
        )
        store._conn.commit()
        assert store.get_setting("llm") is None

    def test_non_object_json_returns_none(self, store: Store) -> None:
        store._conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("push", json.dumps([1, 2, 3]), "2026-10-09T15:05:00"),
        )
        store._conn.commit()
        assert store.get_setting("push") is None


# --------------------------------------------------------------------------- #
# validate_section
# --------------------------------------------------------------------------- #
class TestValidateSectionLLM:
    def test_valid_full(self) -> None:
        clean, errors = validate_section(
            "llm",
            {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": SECRET_KEY,
                "model": "deepseek-chat",
                "temperature": 0.5,
                "max_tokens": 1024,
                "timeout": 15,
            },
        )
        assert errors == []
        assert clean == {
            "base_url": "https://api.deepseek.com/v1",
            "api_key": SECRET_KEY,
            "model": "deepseek-chat",
            "temperature": 0.5,
            "max_tokens": 1024,
            "timeout": 15.0,
        }

    def test_unknown_keys_ignored(self) -> None:
        clean, errors = validate_section("llm", {"foo": 1, "api_key": "k"})
        assert errors == []
        assert clean == {"api_key": "k"}

    def test_base_url_must_be_http(self) -> None:
        for bad in ("ftp://x", "api.deepseek.com", "", 123):
            clean, errors = validate_section("llm", {"base_url": bad})
            assert clean == {}
            assert errors

    def test_temperature_bounds(self) -> None:
        for good in (0, 2, 0.7):
            clean, errors = validate_section("llm", {"temperature": good})
            assert errors == []
            assert clean["temperature"] == float(good)
        for bad in (-0.1, 2.01, "hot", True):
            clean, errors = validate_section("llm", {"temperature": bad})
            assert clean == {}
            assert errors

    def test_max_tokens_and_timeout(self) -> None:
        clean, errors = validate_section("llm", {"max_tokens": 10, "timeout": 1.5})
        assert errors == []
        assert clean == {"max_tokens": 10, "timeout": 1.5}
        for bad in ({"max_tokens": 0}, {"max_tokens": 1.5}, {"max_tokens": True},
                    {"timeout": 0}, {"timeout": -1}, {"timeout": "10"}):
            clean, errors = validate_section("llm", bad)
            assert clean == {}
            assert errors

    def test_empty_dict_rejected(self) -> None:
        clean, errors = validate_section("llm", {})
        assert clean == {}
        assert errors == ["llm: 未提供任何有效字段"]

    def test_all_invalid_rejected(self) -> None:
        clean, errors = validate_section("llm", {"api_key": 1, "model": None})
        assert clean == {}
        # 两个非法字段 + 无任何合法字段的兜底提示。
        assert len(errors) == 3
        assert any("api_key" in e for e in errors)
        assert any("model" in e for e in errors)


class TestValidateSectionPush:
    def test_valid_https_webhook(self) -> None:
        clean, errors = validate_section(
            "push", {"wecom_webhook": WEBHOOK, "enabled": True}
        )
        assert errors == []
        assert clean == {"wecom_webhook": WEBHOOK, "enabled": True}

    def test_http_webhook_rejected(self) -> None:
        clean, errors = validate_section(
            "push", {"wecom_webhook": "http://qyapi.example.com/hook"}
        )
        assert clean == {}
        assert errors

    def test_empty_webhook_means_clear(self) -> None:
        clean, errors = validate_section("push", {"wecom_webhook": ""})
        assert errors == []
        assert clean == {"wecom_webhook": ""}

    def test_bool_strict(self) -> None:
        clean, errors = validate_section("push", {"enabled": False})
        assert errors == []
        assert clean == {"enabled": False}
        clean, errors = validate_section("push", {"enabled": 1})
        assert clean == {}
        assert errors

    def test_secret_and_timeout_types(self) -> None:
        clean, errors = validate_section(
            "push", {"feishu_secret": "s", "timeout": 2.5}
        )
        assert errors == []
        assert clean == {"feishu_secret": "s", "timeout": 2.5}
        clean, errors = validate_section(
            "push", {"feishu_secret": 5, "timeout": "10"}
        )
        assert clean == {}
        # 两个非法字段 + 无任何合法字段的兜底提示。
        assert len(errors) == 3
        assert any("feishu_secret" in e for e in errors)
        assert any("timeout" in e for e in errors)

    def test_empty_dict_rejected(self) -> None:
        clean, errors = validate_section("push", {})
        assert clean == {}
        assert errors == ["push: 未提供任何有效字段"]


class TestValidateSectionSchedule:
    def test_valid(self) -> None:
        clean, errors = validate_section(
            "schedule",
            {"enabled": True, "push": False, "refresh_dashboard": True,
             "scan_time": "15:05"},
        )
        assert errors == []
        assert clean == {
            "enabled": True,
            "push": False,
            "refresh_dashboard": True,
            "scan_time": "15:05",
        }

    def test_scan_time_pads_zero(self) -> None:
        clean, errors = validate_section("schedule", {"scan_time": "9:5"})
        assert errors == []
        assert clean == {"scan_time": "09:05"}
        clean, errors = validate_section("schedule", {"scan_time": "9:05"})
        assert errors == []
        assert clean == {"scan_time": "09:05"}

    def test_scan_time_rejects_bad(self) -> None:
        for bad in ("905", "25:00", "12:60", "abc", "", 905, "9-5"):
            clean, errors = validate_section("schedule", {"scan_time": bad})
            assert clean == {}, f"scan_time={bad!r} 应被拒绝"
            assert errors

    def test_am_scan_time_empty_or_hhmm(self) -> None:
        clean, errors = validate_section("schedule", {"am_scan_time": ""})
        assert errors == []
        assert clean == {"am_scan_time": ""}
        clean, errors = validate_section("schedule", {"am_scan_time": "9:30"})
        assert errors == []
        assert clean == {"am_scan_time": "09:30"}
        clean, errors = validate_section("schedule", {"am_scan_time": "905"})
        assert clean == {}
        assert errors

    def test_bool_strict_and_unknown_ignored(self) -> None:
        clean, errors = validate_section(
            "schedule", {"push": True, "symbols": ["600519"]}
        )
        assert errors == []
        assert clean == {"push": True}
        clean, errors = validate_section("schedule", {"push": 1})
        assert clean == {}
        assert errors

    def test_empty_dict_rejected(self) -> None:
        clean, errors = validate_section("schedule", {})
        assert clean == {}
        assert errors == ["schedule: 未提供任何有效字段"]


class TestValidateSectionUnknown:
    def test_unknown_section(self) -> None:
        clean, errors = validate_section("watchlist", {"symbols": ["600519"]})
        assert clean == []
        assert errors == ["未知配置段: watchlist"]

    def test_editable_sections_contract(self) -> None:
        # 契约锚点：段名集合固定，页面与数据层按此对接。
        assert EDITABLE_SECTIONS == ("llm", "push", "schedule")

    def test_non_dict_data(self) -> None:
        clean, errors = validate_section("llm", ["api_key"])  # type: ignore[arg-type]
        assert clean == {}
        assert errors == ["llm: 配置需为 JSON 对象"]


# --------------------------------------------------------------------------- #
# resolve_runtime_settings
# --------------------------------------------------------------------------- #
class TestResolveRuntimeSettings:
    def test_no_override_matches_base(self, store: Store) -> None:
        base = Settings()
        resolved = resolve_runtime_settings(store, base)
        assert resolved.llm == base.llm
        assert resolved.push == base.push
        assert resolved.schedule == base.schedule

    def test_llm_override_field_level(self, store: Store) -> None:
        base = Settings()
        store.set_setting("llm", {"api_key": SECRET_KEY, "model": "m2"})
        resolved = resolve_runtime_settings(store, base)
        assert resolved.llm.api_key == SECRET_KEY
        assert resolved.llm.model == "m2"
        # 未覆盖字段保留基础值。
        assert resolved.llm.base_url == base.llm.base_url
        assert resolved.llm.temperature == base.llm.temperature
        # 基础配置对象不被就地修改。
        assert base.llm.api_key == ""

    def test_push_override(self, store: Store) -> None:
        base = Settings()
        store.set_setting("push", {"wecom_webhook": WEBHOOK})
        resolved = resolve_runtime_settings(store, base)
        assert resolved.push.wecom_webhook == WEBHOOK
        assert resolved.push.feishu_webhook == base.push.feishu_webhook

    def test_schedule_override_with_normalize(self, store: Store) -> None:
        base = Settings()
        store.set_setting(
            "schedule", {"scan_time": "9:05", "push": False}
        )
        resolved = resolve_runtime_settings(store, base)
        assert resolved.schedule.scan_time == "09:05"
        assert resolved.schedule.push is False
        assert resolved.schedule.refresh_dashboard == base.schedule.refresh_dashboard

    def test_corrupted_json_ignored_not_raise(self, store: Store) -> None:
        base = Settings()
        store._conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("llm", "{broken", "2026-10-09T15:05:00"),
        )
        store._conn.commit()
        resolved = resolve_runtime_settings(store, base)
        assert resolved.llm == base.llm

    def test_invalid_values_ignored(self, store: Store) -> None:
        base = Settings()
        store.set_setting("llm", {"temperature": 9.9, "api_key": "keep"})
        store.set_setting("schedule", {"scan_time": "905"})
        resolved = resolve_runtime_settings(store, base)
        # llm 段含非法值 → 整段忽略（不会只保留合法字段的半套覆盖）。
        assert resolved.llm == base.llm
        assert resolved.schedule == base.schedule

    def test_custom_base_no_global_read(self, store: Store, monkeypatch) -> None:
        base = Settings(llm=LLMSettings(api_key="base-key", model="base-model"))
        store.set_setting("llm", {"model": "page-model"})
        # 全局单例读取一旦发生就炸：证明传入 base 时完全不读全局。
        def _boom() -> Settings:
            raise AssertionError("resolve_runtime_settings 不应读取全局单例")

        monkeypatch.setattr(
            "ashare_prober.data.appsettings.get_settings", _boom
        )
        resolved = resolve_runtime_settings(store, base)
        assert resolved.llm.model == "page-model"
        assert resolved.llm.api_key == "base-key"


# --------------------------------------------------------------------------- #
# push_events(store=...) 接线
# --------------------------------------------------------------------------- #
class TestPushEventsWiring:
    def test_db_webhook_assembles_channel_and_posts(
        self, store: Store, monkeypatch
    ) -> None:
        store.set_setting("push", {"wecom_webhook": WEBHOOK})
        captured: list[tuple[str, dict[str, Any]]] = []

        def _fake_post(url: str, payload: dict[str, Any], timeout: float = 10.0):
            captured.append((url, payload))
            return 200, {"errcode": 0, "errmsg": "ok"}

        monkeypatch.setattr(
            "ashare_prober.push.channels._post_json", _fake_post
        )
        results = push_events([_fake_event()], store=store)
        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].channel == "wecom"
        assert captured and captured[0][0] == WEBHOOK

    def test_no_override_no_channel(self, store: Store) -> None:
        assert push_events([_fake_event()], store=store) == []

    def test_push_disabled_in_db(self, store: Store, monkeypatch) -> None:
        store.set_setting("push", {"enabled": False, "wecom_webhook": WEBHOOK})

        def _fail_post(*args: Any, **kwargs: Any):
            raise AssertionError("推送总开关关闭时不应出网")

        monkeypatch.setattr("ashare_prober.push.channels._post_json", _fail_post)
        assert push_events([_fake_event()], store=store) == []

    def test_legacy_call_unchanged(self, monkeypatch) -> None:
        # 旧调用 push_events(events) 行为保持：签名兼容、无需 store。
        # 拦截真实出网口：无论全局配置是否有 webhook 都不允许发请求。
        def _fail_post(*args: Any, **kwargs: Any):
            raise AssertionError("测试环境不应触发真实网络推送")

        monkeypatch.setattr(
            "ashare_prober.push.channels._post_json", _fail_post
        )
        assert push_events([_fake_event()]) == []

    def test_empty_events_short_circuit(self, store: Store) -> None:
        assert push_events([], store=store) == []


# --------------------------------------------------------------------------- #
# SchedulerService 生效配置接线
# --------------------------------------------------------------------------- #
class TestSchedulerEffectiveSettings:
    @staticmethod
    def _scan_fn_with_event(events: list[Any]):
        def _scan(provider, store, symbols, day):
            return list(events), []

        return _scan

    def test_db_push_false_skips_push_events(self, store: Store, monkeypatch) -> None:
        store.set_setting("schedule", {"push": False, "refresh_dashboard": False})
        calls: list[Any] = []
        monkeypatch.setattr(
            scheduler_service, "push_events", lambda *a, **kw: calls.append(a)
        )
        svc = scheduler_service.SchedulerService(
            provider=object(),
            store=store,
            settings=Settings(),
            scan_fn=self._scan_fn_with_event([_fake_event()]),
            symbols=["600519"],
            out_dir="outputs",
        )
        summary = svc.run_once()
        assert calls == []  # DB 覆盖 push=False → push_events 未执行
        assert summary["events"] == 1
        assert summary["pushed"] == 0
        assert summary["dashboard"] is False

    def test_db_push_true_passes_effective_settings(
        self, store: Store, monkeypatch
    ) -> None:
        store.set_setting(
            "schedule", {"push": True, "refresh_dashboard": False}
        )
        store.set_setting("push", {"wecom_webhook": WEBHOOK})
        seen: dict[str, Any] = {}

        def _fake_push(events, settings=None, store_=None):
            seen["settings"] = settings
            return []

        monkeypatch.setattr(scheduler_service, "push_events", _fake_push)
        svc = scheduler_service.SchedulerService(
            provider=object(),
            store=store,
            settings=Settings(),
            scan_fn=self._scan_fn_with_event([_fake_event()]),
            symbols=["600519"],
            out_dir="outputs",
        )
        summary = svc.run_once()
        assert summary["pushed"] == 0
        effective = seen["settings"]
        assert effective.schedule.push is True
        assert effective.push.wecom_webhook == WEBHOOK

    def test_effective_settings_method(self, store: Store) -> None:
        base = Settings()
        store.set_setting("schedule", {"scan_time": "9:05"})
        svc = scheduler_service.SchedulerService(
            provider=object(), store=store, settings=base
        )
        effective = svc._effective_settings()
        assert effective.schedule.scan_time == "09:05"
        assert base.schedule.scan_time == "15:05"  # base 不被修改

    def test_effective_settings_without_store_is_base(self) -> None:
        base = Settings()
        svc = scheduler_service.SchedulerService(settings=base)
        assert svc._effective_settings() is base


# --------------------------------------------------------------------------- #
# 秘密纪律：日志不出现明文 secret
# --------------------------------------------------------------------------- #
class TestSecretDiscipline:
    def test_resolve_warning_does_not_leak_secret(
        self, store: Store, caplog
    ) -> None:
        # 含明文 secret 的非法覆盖 → 整段忽略并告警，日志不得出现明文。
        store.set_setting("llm", {"api_key": SECRET_KEY, "temperature": 9.9})
        with caplog.at_level(logging.WARNING):
            resolved = resolve_runtime_settings(store, Settings())
        assert resolved.llm.api_key != SECRET_KEY
        assert SECRET_KEY not in caplog.text
        assert WEBHOOK not in caplog.text

    def test_validate_output_does_not_leak_secret(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            clean, errors = validate_section(
                "push", {"wecom_webhook": "http://leak.example/hook"}
            )
            assert clean == {}
            assert errors
            validate_section("llm", {"api_key": SECRET_KEY, "model": 1})
        assert SECRET_KEY not in caplog.text
        assert "leak.example" not in caplog.text

    def test_resolve_success_is_quiet(self, store: Store, caplog) -> None:
        store.set_setting("llm", {"api_key": SECRET_KEY})
        with caplog.at_level(logging.WARNING):
            resolved = resolve_runtime_settings(store, Settings())
        assert resolved.llm.api_key == SECRET_KEY  # 覆盖正常生效
        assert caplog.text == ""  # 成功路径零告警，secret 无外泄面
