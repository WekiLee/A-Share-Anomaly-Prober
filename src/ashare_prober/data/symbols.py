"""标的代码 / 名称解析与模糊匹配（T13）。

命令行与上层分析器允许以「任意形态」指定标的：

- 纯代码：``600519``
- 带交易所前缀：``sh600519`` / ``SZ000001``
- 带交易所后缀：``600519.SH`` / ``000001.sz``
- 中文全称或简称：``贵州茅台``
- 中文片段（模糊）：``茅台`` → ``贵州茅台``
- 拼音首字母（可选）：``gzmt`` → ``贵州茅台``（需环境已安装 ``pypinyin``）

本模块负责把上述输入归一到唯一的 :class:`SymbolInfo`，并给出「你是不是要找…」
的候选列表，供 CLI 消歧提示使用。

设计原则：
- 标的表（全市场 A 股代码/名称）经 :class:`~ashare_prober.data.cache.Cache` 缓存，
  TTL 取 ``TTL.FINANCE``（24 小时，名单变动极慢）。
- 标的表加载函数（``loader``）可注入：单测用假数据、生产默认走 akshare。
- **严禁编造数据**：网络失败且无缓存时抛 ``SourceError``，绝不返回猜测结果。
- 拼音增强为软依赖：``pypinyin`` 缺失时静默跳过该匹配策略（不新增硬依赖）。
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from ashare_prober.config import DEFAULT_DB_PATH, TTL
from ashare_prober.data.base import SourceError
from ashare_prober.data.cache import Cache
from ashare_prober.data.models import _pick, _to_str  # 复用既有容错取值工具

logger = logging.getLogger(__name__)

#: 全量标的表的缓存键（与 provider 共用同一份 ``prober.cache.db``）。
SYMBOL_TABLE_CACHE_KEY = "symbols:all"

#: 交易所前缀 → 市场标识。顺序敏感：``920``（北交所）必须先于 ``9``（沪市）判定。
_BJ_PREFIXES: tuple[str, ...] = ("920", "4", "8")
_SH_PREFIXES: tuple[str, ...] = ("60", "68", "11", "9", "5")
_SZ_PREFIXES: tuple[str, ...] = ("30", "12", "15", "16", "0", "3")

#: 交易所后缀（``600519.SH``）与市场前缀（``sh600519``），统一小写比较。
_EXCHANGE_SUFFIXES: frozenset[str] = frozenset({"sh", "sz", "bj"})
_MARKET_PREFIXES: tuple[str, ...] = ("sh", "sz", "bj")

#: 候选提示最多展示条数（异常消息用）。
_MAX_HINT = 5

#: 「忽略新鲜度」读取缓存时使用的窗口（约 31 年，等价于任何年龄均可命中）。
#: Cache.get 在 ttl=None 时会回退到写入时的 ttl，故显式传一个极大窗口来兜底读取。
_ANY_AGE_TTL = 10**9


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def infer_market(code: str) -> str:
    """根据代码前缀推断交易所标识（``sh`` / ``sz`` / ``bj``）。

    Args:
        code: 标的代码，允许带非数字字符（会先抽取数字）。

    Returns:
        ``"sh"`` / ``"sz"`` / ``"bj"``；无法判定时按沪市兜底返回 ``"sh"``。

    规则（与 ``ARCHITECTURE`` 共享知识一致）：
    - 沪市：``60`` / ``68`` / ``9`` / ``5`` / ``11`` 开头
    - 深市：``0`` / ``3`` / ``12`` / ``15`` / ``16`` / ``30`` 开头
    - 北交所：``4`` / ``8`` / ``920`` 开头
    """
    text = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not text:
        return "sh"
    for prefixes, market in (
        (_BJ_PREFIXES, "bj"),
        (_SH_PREFIXES, "sh"),
        (_SZ_PREFIXES, "sz"),
    ):
        for prefix in prefixes:
            if text.startswith(prefix):
                return market
    return "sh"


def _normalize_code(raw: Any) -> str:
    """把任意形态的代码清洗为「纯数字」串；不足 6 位左侧补零。"""
    digits = "".join(ch for ch in str(raw if raw is not None else "") if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(6) if len(digits) < 6 else digits


def _to_halfwidth(text: str) -> str:
    """全角字符转半角（``ＳＨ６００５１９`` → ``SH600519``）。"""
    return unicodedata.normalize("NFKC", text) if text else text


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
class SymbolInfo(BaseModel):
    """标的基础信息（代码 / 名称 / 交易所）。

    Attributes:
        code: 6 位纯数字代码，如 ``"600519"``。
        name: 中文简称，如 ``"贵州茅台"``。
        market: ``"sh"`` / ``"sz"`` / ``"bj"``，缺省时由代码前缀推断。
    """

    model_config = ConfigDict(extra="ignore")

    code: str = ""
    name: str = ""
    market: str = ""

    @model_validator(mode="after")
    def _infer_market(self) -> "SymbolInfo":
        """代码存在而市场为空时，按前缀补全 market。"""
        if not self.market and self.code:
            self.market = infer_market(self.code)
        return self

    @property
    def prefixed(self) -> str:
        """带交易所前缀的代码，如 ``"sh600519"``（派生属性，不落库）。"""
        return f"{self.market or infer_market(self.code)}{self.code}"

    @classmethod
    def from_akshare_row(cls, row: Mapping[str, Any]) -> "SymbolInfo":
        """从 akshare 标的行归一；键名兼容 ``code/代码`` 与 ``name/名称``。

        Args:
            row: 一行原始数据（dict / Series.to_dict() 皆可）。
        """
        return cls(
            code=_normalize_code(_to_str(_pick(row, "code", "代码", "symbol", "股票代码"))),
            name=_to_str(_pick(row, "name", "名称", "股票简称", "简称")),
        )


class SymbolNotFound(SourceError):
    """无法把用户输入解析为唯一标的。

    Attributes:
        candidates: 候选标的列表，供上层提示「你是不是要找…」；无候选时为空列表。
    """

    def __init__(
        self,
        message: str,
        *,
        candidates: Optional[Sequence[SymbolInfo]] = None,
        source: str = "symbols",
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, source=source, cause=cause)
        self.candidates: list[SymbolInfo] = list(candidates or [])


# --------------------------------------------------------------------------- #
# 拼音首字母（软依赖：pypinyin 缺失则静默降级）
# --------------------------------------------------------------------------- #
_PYPINYIN_UNAVAILABLE = False


def _pinyin_initials(name: str) -> str:
    """返回中文名称的拼音首字母串（``"贵州茅台"`` → ``"gzmt"``）。

    ``pypinyin`` 未安装或调用异常时返回空串，由调用方按「无候选」处理，
    绝不因缺少软依赖而抛出异常（项目不引入该硬依赖）。
    """
    global _PYPINYIN_UNAVAILABLE
    if not name or _PYPINYIN_UNAVAILABLE:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        _PYPINYIN_UNAVAILABLE = True
        logger.debug("pypinyin 未安装，跳过拼音首字母匹配")
        return ""
    except Exception as exc:  # noqa: BLE001 - 任何导入兼容问题都静默降级
        _PYPINYIN_UNAVAILABLE = True
        logger.debug("pypinyin 导入失败，跳过拼音首字母匹配：%s", exc)
        return ""

    try:
        letters = lazy_pinyin(name, style=Style.FIRST_LETTER)
    except Exception as exc:  # noqa: BLE001 - 拼音库异常不影响主流程
        logger.debug("拼音首字母计算失败(%s)：%s", name, exc)
        return ""
    return "".join(ch for ch in "".join(letters) if ch.isalpha()).lower()


def _pinyin_hits(table: Sequence[SymbolInfo], token: str) -> list[SymbolInfo]:
    """按拼音首字母（精确或前缀）在标的表中匹配。"""
    hits: list[SymbolInfo] = []
    for item in table:
        initials = _pinyin_initials(item.name)
        if initials and (initials == token or initials.startswith(token)):
            hits.append(item)
    return hits


def _rank(item: SymbolInfo, token: str) -> tuple[int, int, int, str]:
    """候选排序键：代码精确 > 名称精确 > 名称更短 > 代码更小。"""
    return (
        0 if item.code == token else 1,
        0 if item.name == token else 1,
        len(item.name),
        item.code,
    )


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
class SymbolResolver:
    """标的解析器：归一化输入 → 查标的表 → 精确/模糊匹配 → 返回唯一标的。

    标的表默认由 :func:`load_from_akshare` 提供并经 TTL 缓存；可通过构造参数
    ``loader`` 注入假数据（单测离线）或自定义数据源。

    Example:
        >>> resolver = SymbolResolver(cache=Cache(None), loader=lambda: [
        ...     SymbolInfo(code="600519", name="贵州茅台")
        ... ])
        >>> resolver.resolve("sh600519").name
        '贵州茅台'
    """

    def __init__(
        self,
        cache: Optional[Cache] = None,
        loader: Optional[Callable[[], Any]] = None,
    ) -> None:
        """初始化解析器。

        Args:
            cache: TTL 缓存；``None`` 时内部构建默认缓存（``prober.cache.db``）。
            loader: 标的表加载函数，返回 ``SymbolInfo`` 序列（或其 dict 形式）；
                ``None`` 时使用 :func:`load_from_akshare`。
        """
        self._cache: Optional[Cache] = cache if cache is not None else _default_cache()
        self._loader: Optional[Callable[[], Any]] = loader

    # ------------------------------------------------------------------ #
    # 归一化
    # ------------------------------------------------------------------ #
    def normalize(self, query: str) -> str:
        """清洗用户输入的标的串，返回「仅剩数字或中文」的归一化结果。

        处理：去首尾空格 → 全角转半角 → 去内部空白 → 去 ``.SH/.SZ/.BJ`` 后缀 →
        去 ``sh/sz/bj`` 前缀（大小写不敏感）。

        Args:
            query: 原始输入，如 ``"sh600519"`` / ``"600519.SH"`` / ``" 贵州茅台 "``。

        Returns:
            归一化后的串，如 ``"600519"`` / ``"贵州茅台"``。
        """
        text = _to_halfwidth(str(query if query is not None else "")).strip()
        text = "".join(ch for ch in text if not ch.isspace())
        if not text:
            return ""

        # 交易所后缀：600519.SH / 000001.sz
        if "." in text:
            head, _, tail = text.rpartition(".")
            if tail.lower() in _EXCHANGE_SUFFIXES:
                text = head

        # 市场前缀：sh600519 / SZ000001（纯数字代码不含字母，不会被误删）
        lowered = text.lower()
        for prefix in _MARKET_PREFIXES:
            if lowered.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix) :]
                break

        return text.strip()

    # ------------------------------------------------------------------ #
    # 标的表（带缓存）
    # ------------------------------------------------------------------ #
    def table(self, force: bool = False) -> list[SymbolInfo]:
        """返回全量标的表（优先命中缓存，失败回源）。

        Args:
            force: 跳过缓存强制回源（回源成功后会刷新缓存）。

        Raises:
            SourceError: 回源失败且无可用缓存时抛出（严禁编造数据）。
        """
        if not force:
            cached = self._read_cache(ignore_ttl=False)
            if cached is not None:
                return cached

        try:
            items = self._load()
        except Exception as exc:  # noqa: BLE001 - 任何回源异常统一归一
            stale = self._read_cache(ignore_ttl=True)
            if stale is not None:
                logger.warning("标的表回源失败，改用已缓存名单：%s", exc)
                return stale
            raise SourceError(
                f"标的表加载失败: {exc}", source="symbols", cause=exc
            ) from exc

        if not items:
            logger.warning("标的表为空（加载源未返回有效数据）")
            return items

        self._write_cache(items)
        return items

    def _load(self) -> list[SymbolInfo]:
        """调用注入的 loader（缺省 akshare）并把结果归一为 ``SymbolInfo`` 列表。"""
        loader = self._loader or load_from_akshare
        raw: Any = loader()
        if not raw:
            return []
        items: list[SymbolInfo] = []
        seen: set[str] = set()
        for row in raw:
            item = _coerce_symbol(row)
            if item is None or not item.code or item.code in seen:
                continue
            seen.add(item.code)
            items.append(item)
        return items

    def _read_cache(self, ignore_ttl: bool = False) -> Optional[list[SymbolInfo]]:
        """从缓存读取标的表；未命中 / 解析失败返回 ``None``。

        Args:
            ignore_ttl: 为 True 时忽略新鲜度（回源失败后的兜底读取）。
        """
        if self._cache is None:
            return None
        try:
            raw: Any = self._cache.get(
                SYMBOL_TABLE_CACHE_KEY,
                ttl=_ANY_AGE_TTL if ignore_ttl else int(TTL.FINANCE),
            )
        except Exception:  # noqa: BLE001 - 缓存未命中或已过期
            return None
        if not isinstance(raw, list):
            return None
        items: list[SymbolInfo] = []
        for row in raw:
            item = _coerce_symbol(row)
            if item is not None:
                items.append(item)
        return items or None

    def _write_cache(self, items: Sequence[SymbolInfo]) -> None:
        """把标的表写入缓存（TTL 取 ``TTL.FINANCE``）。"""
        if self._cache is None:
            return
        try:
            self._cache.set(
                SYMBOL_TABLE_CACHE_KEY,
                [item.model_dump(mode="json") for item in items],
                int(TTL.FINANCE),
            )
        except Exception as exc:  # noqa: BLE001 - 缓存不可写不得影响主流程
            logger.warning("标的表写入缓存失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 解析 / 搜索
    # ------------------------------------------------------------------ #
    def resolve(self, query: str) -> SymbolInfo:
        """把用户输入解析为唯一标的。

        匹配优先级：6 位代码精确 → 2~6 位代码前缀 → 名称精确 → 名称包含 →
        拼音首字母。多命中一律抛 :class:`SymbolNotFound` 并附候选。

        Args:
            query: 用户输入的标的串（代码 / 名称 / 前缀形态均可）。

        Raises:
            SymbolNotFound: 无匹配或存在多个匹配。
            SourceError: 标的表不可用（无缓存且回源失败）。
        """
        raw = str(query if query is not None else "")
        token = self.normalize(raw)
        if not token:
            raise SymbolNotFound(f"标的输入为空，无法解析: {raw!r}")

        table = self.table()

        # 1) / 2) 数字：代码精确匹配 → 代码前缀匹配
        if token.isdigit():
            if len(token) == 6:
                for item in table:
                    if item.code == token:
                        return item
            if 2 <= len(token) <= 6:
                hits = [item for item in table if item.code.startswith(token)]
                if len(hits) == 1:
                    return hits[0]
                if len(hits) > 1:
                    raise self._not_found(raw, hits)

        # 3) 中文名称精确匹配
        for item in table:
            if item.name == token:
                return item

        # 4) 中文名称包含（模糊）匹配
        fuzzy = [item for item in table if token and token in item.name]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise self._not_found(raw, fuzzy)

        # 5) 拼音首字母（软依赖，缺失时静默跳过）
        if token.isascii() and token.isalpha():
            hits = _pinyin_hits(table, token)
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise self._not_found(raw, hits)

        raise self._not_found(raw, [])

    def search(self, query: str, limit: int = 10) -> list[SymbolInfo]:
        """返回候选标的（代码前缀 / 名称包含 / 拼音首字母），供消歧与 CLI 搜索。

        永不抛异常：标的表不可用或查不到时返回空列表。

        Args:
            query: 查询串。
            limit: 返回条数上限；``<=0`` 时返回空列表。
        """
        token = self.normalize(query)
        if not token:
            return []
        try:
            table = self.table()
        except Exception as exc:  # noqa: BLE001 - 搜索为「尽力而为」接口
            logger.warning("标的表不可用，search 返回空：%s", exc)
            return []

        found: dict[str, SymbolInfo] = {}
        for item in table:
            if item.code.startswith(token) or token in item.name:
                found.setdefault(item.code, item)
        if token.isascii() and token.isalpha():
            for item in _pinyin_hits(table, token):
                found.setdefault(item.code, item)

        ordered = sorted(found.values(), key=lambda item: _rank(item, token))
        count = max(0, int(limit))
        return ordered[:count]

    def resolve_many(
        self, queries: Sequence[str]
    ) -> tuple[list[SymbolInfo], list[str]]:
        """批量解析；单个失败不阻断其它。

        Args:
            queries: 标的输入序列。

        Returns:
            ``(成功解析的标的列表, 失败的输入列表)``，两者顺序均与入参一致。
        """
        resolved: list[SymbolInfo] = []
        failed: list[str] = []
        for raw in queries or []:
            text = str(raw if raw is not None else "")
            try:
                resolved.append(self.resolve(text))
            except (SymbolNotFound, SourceError) as exc:
                logger.warning("解析标的失败：%s（%s）", text, exc)
                failed.append(text)
        return resolved, failed

    # ------------------------------------------------------------------ #
    # 内部：异常构造
    # ------------------------------------------------------------------ #
    def _not_found(
        self, query: str, candidates: Sequence[SymbolInfo]
    ) -> SymbolNotFound:
        """构造带候选提示的 ``SymbolNotFound``。"""
        items = sorted(candidates, key=lambda item: (len(item.name), item.code))
        hint = ""
        if items:
            preview = "、".join(
                f"{item.name}({item.prefixed})" for item in items[:_MAX_HINT]
            )
            hint = f"；你是否要找：{preview}"
        return SymbolNotFound(f"未找到匹配标的: {query}{hint}", candidates=items)


# --------------------------------------------------------------------------- #
# 默认标的表加载（akshare）
# --------------------------------------------------------------------------- #
def load_from_akshare() -> list[SymbolInfo]:
    """从 akshare 拉取全市场 A 股「代码-名称」表。

    优先 ``stock_info_a_code_name()``（列 ``code``/``name``）；失败回退
    ``stock_zh_a_spot_em()``（列 ``代码``/``名称``）。两者皆败抛 ``SourceError``。

    Raises:
        SourceError: akshare 未安装 / 接口失败 / 返回为空。
    """
    ak = _import_akshare()
    try:
        records = _to_records(ak.stock_info_a_code_name())
    except Exception as first_exc:  # noqa: BLE001
        logger.warning(
            "stock_info_a_code_name 失败，回退 stock_zh_a_spot_em：%s", first_exc
        )
        try:
            records = _to_records(ak.stock_zh_a_spot_em())
        except Exception as second_exc:  # noqa: BLE001
            raise SourceError(
                f"获取 A 股标的列表失败: {second_exc}",
                source="akshare",
                cause=second_exc,
            ) from second_exc

    items: list[SymbolInfo] = []
    seen: set[str] = set()
    for row in records:
        item = _coerce_symbol(row)
        if item is None or not item.code or item.code in seen:
            continue
        seen.add(item.code)
        items.append(item)

    if not items:
        raise SourceError("akshare 未返回有效标的列表", source="akshare")
    return items


def _import_akshare() -> Any:
    """惰性导入 akshare；失败抛 SourceError（不污染模块导入期）。"""
    try:
        import akshare as ak
    except Exception as exc:  # pragma: no cover - 依赖未安装时
        raise SourceError(
            "akshare 未安装或导入失败，请先安装：pip install akshare",
            source="akshare",
            cause=exc,
        ) from exc
    return ak


def _to_records(frame: Any) -> list[Mapping[str, Any]]:
    """把 akshare 返回的 DataFrame 转为 dict 行列表。"""
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        try:
            rows: Any = frame.to_dict("records")
        except TypeError:  # pragma: no cover - 非 pandas 对象兜底
            rows = frame.to_dict()
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, Mapping)]
    if isinstance(frame, Sequence) and not isinstance(frame, (str, bytes)):
        return [row for row in frame if isinstance(row, Mapping)]
    return []


def _coerce_symbol(row: Any) -> Optional[SymbolInfo]:
    """把任意行（``SymbolInfo`` / dict）转为 ``SymbolInfo``；失败返回 ``None``。"""
    if isinstance(row, SymbolInfo):
        return row
    if not isinstance(row, Mapping):
        return None
    try:
        return SymbolInfo.model_validate(row)
    except Exception:  # noqa: BLE001 - 脏行跳过，不阻断整表
        logger.debug("跳过无法归一的标的行: %r", row)
        return None


def _default_cache() -> Cache:
    """构建默认缓存（项目 ``data/prober.cache.db``）；不可用时退化为内存缓存。"""
    try:
        path = DEFAULT_DB_PATH.with_name("prober.cache.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        return Cache(path)
    except Exception as exc:  # noqa: BLE001 - 缓存不可用不得阻断解析
        logger.warning("默认标的表缓存不可用，改用内存缓存：%s", exc)
        return Cache(None)


__all__ = [
    "SymbolInfo",
    "SymbolResolver",
    "SymbolNotFound",
    "load_from_akshare",
    "infer_market",
    "SYMBOL_TABLE_CACHE_KEY",
]
