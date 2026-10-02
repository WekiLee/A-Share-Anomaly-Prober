"""全局配置与常量（T02）。

集中定义：
- 红涨绿跌配色（UP_COLOR / DOWN_COLOR / NEUTRAL_COLOR）
- 时区（TZ）
- 缓存 TTL 枚举（QUOTE / NEWS / FINANCE / EXTERNAL）
- 本地存储路径（DATA_DIR / DB_PATH）
- ID 规范辅助函数（make_event_id / make_dossier_id）
- LLM / 推送配置占位（读取 config.toml，缺省安全默认值）

设计原则：本模块可被任意层 import 且不触发副作用；配置文件按需通过
``get_settings()`` 惰性加载。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping, Union

# --------------------------------------------------------------------------- #
# 颜色：红涨绿跌（共享知识约定）
# --------------------------------------------------------------------------- #
UP_COLOR = "#d70015"  # 涨：红
DOWN_COLOR = "#008a3e"  # 跌：绿
NEUTRAL_COLOR = "#888888"  # 中性

# --------------------------------------------------------------------------- #
# 时区：统一以北京时间处理
# --------------------------------------------------------------------------- #
TZ = "Asia/Shanghai"

# --------------------------------------------------------------------------- #
# 缓存 TTL（单位：秒）。见 ARCHITECTURE 第 5 节共享知识。
# --------------------------------------------------------------------------- #
class TTL(IntEnum):
    """缓存存活时长（秒）。"""

    QUOTE = 600  # 行情：10 分钟
    NEWS = 7200  # 新闻 / 公告：2 小时
    FINANCE = 86400  # 财务 / 解禁等：24 小时
    EXTERNAL = 300  # 外围（港股/美股/期货/汇率）：5 分钟


# 永久（不设置过期）哨兵：缓存层用 ``None`` 表示永不过期（用于日线 / 公告长期数据）。
PERMANENT: None = None

# --------------------------------------------------------------------------- #
# 路径：默认项目根 data/ 与 data/prober.db
# --------------------------------------------------------------------------- #
# config.py 位于 src/ashare_prober/config.py，向上三级即仓库根。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "prober.db"

# 默认配置文件（项目根 config.toml）；被 .gitignore 忽略。
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


# --------------------------------------------------------------------------- #
# ID 规范辅助函数（共享知识：EVT-<code>-<date>-<seq> / DSS-<code>-<date>）
# --------------------------------------------------------------------------- #
def _compact_date(value: Union[str, date, datetime]) -> str:
    """将日期统一规整为 ``YYYYMMDD`` 紧凑格式，便于拼入 ID。"""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    # 字符串：去掉所有非数字字符（兼容 "2026-10-09" / "20261009"）。
    return "".join(ch for ch in str(value) if ch.isdigit())


def make_event_id(code: str, day: Union[str, date, datetime], seq: int = 1) -> str:
    """构造异动事件 ID：``EVT-<code>-<YYYYMMDD>-<seq:03d>``。

    Args:
        code: 标的代码，如 ``"600600"``。
        day: 触发日期（date / datetime / 字符串均可）。
        seq: 同标的同日的事件序号，默认 1，补零至 3 位。
    """
    return f"EVT-{code}-{_compact_date(day)}-{seq:03d}"


def make_dossier_id(code: str, day: Union[str, date, datetime]) -> str:
    """构造事件档案 ID：``DSS-<code>-<YYYYMMDD>``。"""
    return f"DSS-{code}-{_compact_date(day)}"


# --------------------------------------------------------------------------- #
# LLM / 推送配置占位（dataclass，缺省安全默认值）
# --------------------------------------------------------------------------- #
@dataclass
class LLMSettings:
    """LLM 研判客户端配置（OpenAI 兼容）。"""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""  # 留空即未配置，运行研判时由上层提示
    model: str = "deepseek-chat"
    temperature: float = 0.3  # 低温度抑制幻觉
    max_tokens: int = 2048  # 输出上限，成本保护
    timeout: float = 30.0  # 请求超时（秒）

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LLMSettings":
        """从配置片段（可能缺字段）合并出安全默认值。"""
        return cls(
            base_url=str(data.get("base_url", cls.base_url)),
            api_key=str(data.get("api_key", cls.api_key)),
            model=str(data.get("model", cls.model)),
            temperature=float(data.get("temperature", cls.temperature)),
            max_tokens=int(data.get("max_tokens", cls.max_tokens)),
            timeout=float(data.get("timeout", cls.timeout)),
        )


@dataclass
class PushSettings:
    """推送通道配置（企业微信 / 飞书 webhook）。

    新增字段均为「有默认值 + from_dict 兼容缺字段」，不影响既有配置语义。
    """

    wecom_webhook: str = ""
    feishu_webhook: str = ""
    feishu_secret: str = ""  # 飞书签名密钥（可选，留空则不签名）
    timeout: float = 10.0  # 单次推送 HTTP 超时（秒）
    enabled: bool = True  # 推送总开关（False 时不装配任何通道）

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PushSettings":
        return cls(
            wecom_webhook=str(data.get("wecom_webhook", cls.wecom_webhook)),
            feishu_webhook=str(data.get("feishu_webhook", cls.feishu_webhook)),
            feishu_secret=str(data.get("feishu_secret", cls.feishu_secret)),
            timeout=float(data.get("timeout", cls.timeout)),
            enabled=bool(data.get("enabled", cls.enabled)),
        )


# --------------------------------------------------------------------------- #
# 自选列表 / 定时调度配置（T12：均为「有默认值 + from_dict 兼容缺字段」的加法）
# --------------------------------------------------------------------------- #
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _normalize_hhmm(value: str, fallback: str) -> str:
    """校验并规整 ``HH:MM`` 时刻字符串；非法时返回 ``fallback``（不抛异常）。

    兼容 ``9:05`` 这类非补零写法，统一规整为 ``09:05``。
    """
    match = _TIME_RE.match(str(value or "").strip())
    if match is None:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return fallback
    return f"{hour:02d}:{minute:02d}"


@dataclass
class WatchlistSettings:
    """自选标的列表（``[watchlist]`` 段）。

    实现了 ``__iter__`` / ``__len__``：可像 list 一样直接迭代，
    兼容 ``AnomalyEngine.run_once`` 对 ``settings.watchlist`` 的历史读取方式。
    """

    symbols: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WatchlistSettings":
        raw = data.get("symbols", [])
        if not isinstance(raw, (list, tuple)):
            raw = []
        symbols = [str(s).strip() for s in raw if str(s).strip()]
        return cls(symbols=symbols)

    def __iter__(self):  # noqa: ANN201 - 迭代器类型随内容而定
        return iter(self.symbols)

    def __len__(self) -> int:
        return len(self.symbols)


@dataclass
class ScheduleSettings:
    """定时调度配置（``[schedule]`` 段）。

    - ``scan_time``：收盘后扫描时刻（``HH:MM``，北京时间），默认 ``15:05``。
    - ``am_scan_time``：早盘扫描时刻；留空表示不做早盘扫描。
    - 时刻字符串非法时回退默认值，**不抛异常**（保证程序可启动）。
    """

    enabled: bool = True
    scan_time: str = "15:05"
    am_scan_time: str = ""
    push: bool = True  # 扫描后是否推送异动事件
    refresh_dashboard: bool = True  # 扫描后是否刷新大盘看板

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScheduleSettings":
        am_raw = str(data.get("am_scan_time", cls.am_scan_time) or "").strip()
        return cls(
            enabled=bool(data.get("enabled", cls.enabled)),
            scan_time=_normalize_hhmm(
                str(data.get("scan_time", cls.scan_time)), cls.scan_time
            ),
            am_scan_time=_normalize_hhmm(am_raw, "") if am_raw else "",
            push=bool(data.get("push", cls.push)),
            refresh_dashboard=bool(data.get("refresh_dashboard", cls.refresh_dashboard)),
        )


@dataclass
class Settings:
    """全局配置聚合。``data_dir`` / ``db_path`` 可被 config.toml 覆盖。"""

    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    llm: LLMSettings = field(default_factory=LLMSettings)
    push: PushSettings = field(default_factory=PushSettings)
    watchlist: WatchlistSettings = field(default_factory=WatchlistSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)

    @classmethod
    def load(cls, path: Union[str, Path, None] = None) -> "Settings":
        """加载配置：优先读取 ``path``（默认项目根 config.toml），缺省安全值兜底。

        读取失败时（文件不存在 / 解析错误）静默回退到默认值，保证程序可启动。
        """
        cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if cfg_path.exists():
            try:
                with cfg_path.open("rb") as fh:
                    raw = tomllib.load(fh)
            except (tomllib.TOMLDecodeError, OSError):
                raw = {}

        data_dir_raw = raw.get("data_dir")
        db_path_raw = raw.get("db_path")

        return cls(
            data_dir=Path(data_dir_raw) if data_dir_raw else DEFAULT_DATA_DIR,
            db_path=Path(db_path_raw) if db_path_raw else DEFAULT_DB_PATH,
            llm=LLMSettings.from_dict(raw.get("llm", {})),
            push=PushSettings.from_dict(raw.get("push", {})),
            watchlist=WatchlistSettings.from_dict(raw.get("watchlist", {})),
            schedule=ScheduleSettings.from_dict(raw.get("schedule", {})),
        )


# --------------------------------------------------------------------------- #
# 惰性单例：``get_settings()`` 缓存一次，测试中可用 ``reload_settings()`` 重置。
# --------------------------------------------------------------------------- #
_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    """返回（并缓存）全局配置；首次调用时从默认路径加载。"""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings.load()
    return _SETTINGS


def reload_settings(path: Union[str, Path, None] = None) -> Settings:
    """强制重新加载配置（测试或热更新时使用）。"""
    global _SETTINGS
    _SETTINGS = Settings.load(path)
    return _SETTINGS
