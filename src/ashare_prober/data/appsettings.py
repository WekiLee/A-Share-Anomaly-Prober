"""页面自助配置（app_settings）数据层。

把「LLM / 推送 / 调度」三段配置的页面覆盖（存于 ``app_settings`` 表）校验、
合并到运行时配置对象，实现「页面改完下一轮立即生效，无需重启」。

职责边界：
- ``validate_section``：单段配置的白名单键 + 类型/取值校验，返回
  ``(清理后仅含合法键的 dict, 错误列表)``；未知键忽略，绝不抛异常。
- ``resolve_runtime_settings``：把 DB 覆盖段按字段级合并进基础配置；
  覆盖数据损坏 / 校验失败时**忽略该段**并记告警，绝不抛、绝不阻断启动。

秘密纪律：本模块不做任何打印 / 日志输出明文 secret
（``api_key`` / ``webhook`` / ``feishu_secret``）；告警只含段名与错误描述。
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any

from ashare_prober.config import (
    LLMSettings,
    PushSettings,
    ScheduleSettings,
    Settings,
    get_settings,
)

logger = logging.getLogger(__name__)

#: 页面可编辑的配置段名（与 ``app_settings.key`` / 页面路由一一对应）。
EDITABLE_SECTIONS = ("llm", "push", "schedule")

#: ``HH:MM`` 时刻形态（小时/分钟均允许 1~2 位，便于 ``9:5`` 自动补零）。
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})$")


# --------------------------------------------------------------------------- #
# 基础校验工具
# --------------------------------------------------------------------------- #
def _is_plain_str(value: Any) -> bool:
    """是否为字符串（bool 是 int 的子类，但与 str 无关，直接 isinstance）。"""
    return isinstance(value, str)


def _is_bool(value: Any) -> bool:
    """严格布尔判定（``1``/``0`` 等整数不视为布尔）。"""
    return isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """严格数值判定：int/float 均可，bool 排除。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_positive_number(value: Any) -> bool:
    """严格正数判定（用于 timeout 等字段）。"""
    return _is_number(value) and float(value) > 0


def _is_http_url(value: Any) -> bool:
    """LLM base_url 判定：http:// 或 https:// 开头。"""
    return _is_plain_str(value) and (
        value.startswith("http://") or value.startswith("https://")
    )


def _is_https_url(value: Any) -> bool:
    """webhook 判定：https:// 开头（webhook 带密钥，禁止明文 http）。"""
    return _is_plain_str(value) and value.startswith("https://")


def _norm_hhmm(value: Any) -> str | None:
    """校验并规整 ``HH:MM`` 时刻；非法返回 ``None``。

    兼容 ``9:05`` / ``9:5`` 这类非补零写法，统一补零为 ``09:05``；
    ``"905"``（无冒号）、``25:00`` 等一律拒绝。
    """
    if not _is_plain_str(value):
        return None
    match = _TIME_RE.match(value.strip())
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


# --------------------------------------------------------------------------- #
# 分段校验
# --------------------------------------------------------------------------- #
def _validate_llm(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """校验 ``llm`` 段：返回 (仅含合法键的 dict, 错误列表)。"""
    clean: dict[str, Any] = {}
    errors: list[str] = []

    if "base_url" in data:
        if _is_http_url(data["base_url"]):
            clean["base_url"] = data["base_url"]
        else:
            errors.append("llm.base_url 需为以 http:// 或 https:// 开头的字符串")

    for field in ("api_key", "model"):
        if field in data:
            if _is_plain_str(data[field]):
                clean[field] = data[field]
            else:
                errors.append(f"llm.{field} 需为字符串")

    if "temperature" in data:
        value = data["temperature"]
        if _is_number(value) and 0.0 <= float(value) <= 2.0:
            clean["temperature"] = float(value)
        else:
            errors.append("llm.temperature 需为 0~2 之间的数值")

    if "max_tokens" in data:
        value = data["max_tokens"]
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            clean["max_tokens"] = int(value)
        else:
            errors.append("llm.max_tokens 需为正整数")

    if "timeout" in data:
        if _is_positive_number(data["timeout"]):
            clean["timeout"] = float(data["timeout"])
        else:
            errors.append("llm.timeout 需为正数")

    if not clean:
        errors.append("llm: 未提供任何有效字段")
    return clean, errors


def _validate_push(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """校验 ``push`` 段：返回 (仅含合法键的 dict, 错误列表)。"""
    clean: dict[str, Any] = {}
    errors: list[str] = []

    # webhook：空串表示「清空该通道」，否则必须 https:// 开头。
    for field in ("wecom_webhook", "feishu_webhook"):
        if field in data:
            value = data[field]
            if _is_plain_str(value) and (value == "" or _is_https_url(value)):
                clean[field] = value
            else:
                errors.append(f"push.{field} 需为空串或以 https:// 开头的字符串")

    if "feishu_secret" in data:
        if _is_plain_str(data["feishu_secret"]):
            clean["feishu_secret"] = data["feishu_secret"]
        else:
            errors.append("push.feishu_secret 需为字符串")

    if "enabled" in data:
        if _is_bool(data["enabled"]):
            clean["enabled"] = data["enabled"]
        else:
            errors.append("push.enabled 需为布尔值")

    if "timeout" in data:
        if _is_positive_number(data["timeout"]):
            clean["timeout"] = float(data["timeout"])
        else:
            errors.append("push.timeout 需为正数")

    if not clean:
        errors.append("push: 未提供任何有效字段")
    return clean, errors


def _validate_schedule(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """校验 ``schedule`` 段：返回 (仅含合法键的 dict, 错误列表)。"""
    clean: dict[str, Any] = {}
    errors: list[str] = []

    for field in ("enabled", "push", "refresh_dashboard"):
        if field in data:
            if _is_bool(data[field]):
                clean[field] = data[field]
            else:
                errors.append(f"schedule.{field} 需为布尔值")

    if "scan_time" in data:
        norm = _norm_hhmm(data["scan_time"])
        if norm is not None:
            clean["scan_time"] = norm
        else:
            errors.append("schedule.scan_time 需为 HH:MM 时刻")

    if "am_scan_time" in data:
        value = data["am_scan_time"]
        # 空串 = 清空早盘扫描，合法；否则必须为 HH:MM。
        if _is_plain_str(value) and value.strip() == "":
            clean["am_scan_time"] = ""
        else:
            norm = _norm_hhmm(value)
            if norm is not None:
                clean["am_scan_time"] = norm
            else:
                errors.append("schedule.am_scan_time 需为空串或 HH:MM 时刻")

    if not clean:
        errors.append("schedule: 未提供任何有效字段")
    return clean, errors


#: 段名 -> 校验函数（内部派发表，键与 EDITABLE_SECTIONS 严格一致）。
_VALIDATORS = {
    "llm": _validate_llm,
    "push": _validate_push,
    "schedule": _validate_schedule,
}


def validate_section(section: str, data: dict) -> tuple[dict, list[str]]:
    """校验一个配置段的页面覆盖数据。

    未知键一律忽略；只把「键名合法且类型/取值合法」的字段收进结果。

    Args:
        section: 配置段名（须在 :data:`EDITABLE_SECTIONS` 内）。
        data: 页面提交的该段配置 JSON 对象。

    Returns:
        ``(clean, errors)`` 二元组：``clean`` 为仅含合法键的 dict（可为空），
        ``errors`` 为错误描述列表（无错误时为空列表）。``section`` 非法或
        提交内容不含任何合法字段时返回空 dict 与至少一条错误，**不抛异常**。
    """
    if section not in _VALIDATORS:
        return [], [f"未知配置段: {section}"]
    if not isinstance(data, dict):
        return {}, [f"{section}: 配置需为 JSON 对象"]
    return _VALIDATORS[section](data)


# --------------------------------------------------------------------------- #
# 运行时生效配置
# --------------------------------------------------------------------------- #
def resolve_runtime_settings(store: Any, settings: Any = None) -> Settings:
    """把 ``store`` 中 ``app_settings`` 表的覆盖段合并进基础配置。

    对每个可编辑段：
    - 无覆盖记录 → 保持基础配置该段不变；
    - 有覆盖且校验通过 → 按字段级合并（``dataclasses.replace``，未覆盖字段
      保留基础配置值）；
    - 覆盖数据损坏 / 校验失败 → **忽略该段**并记 ``logging.warning``
      （告警内容只含段名与错误描述，不含任何配置值）。

    本函数绝不抛异常、绝不阻断启动；``store`` 读取异常同样按「无覆盖」处理。

    Args:
        store: 本地库（需提供 ``get_setting``；可为任意鸭子类型）。
        settings: 基础配置对象；``None`` 时取全局 ``get_settings()`` 单例。

    Returns:
        合并后的 ``Settings``（新对象；传入的 base 不被就地修改）。
    """
    base: Settings = settings if settings is not None else get_settings()
    overrides: dict[str, Any] = {}

    for section in EDITABLE_SECTIONS:
        try:
            raw = store.get_setting(section)
        except Exception as exc:  # noqa: BLE001 - 存储异常按无覆盖处理
            logger.warning(
                "读取配置覆盖失败，忽略该段: section=%s error=%s", section, exc
            )
            continue
        if raw is None:
            continue
        if not isinstance(raw, dict):
            logger.warning("配置覆盖格式非法，已忽略该段: section=%s", section)
            continue

        clean, errors = validate_section(section, raw)
        if errors:
            # 秘密纪律：只输出错误描述，绝不输出配置值。
            logger.warning(
                "配置覆盖校验失败，已忽略该段: section=%s errors=%s",
                section,
                "; ".join(errors),
            )
            continue
        if not clean:
            continue

        # 字段级合并：仅覆盖 DB 中出现的合法字段，其余字段保留基础配置值。
        overrides[section] = dataclasses.replace(getattr(base, section), **clean)

    if not overrides:
        return base
    return dataclasses.replace(base, **overrides)
