"""akshare 主数据源适配器（T06）。

实现 ``DataSource`` 协议，使用 akshare 真实接口填充归一模型。
- 所有网络/接口/解析异常统一归一为 ``SourceError``。
- akshare 在函数级惰性 import，未安装时不影响模块导入（冒烟测试默认 skip）。
- 接口名随版本变动，以「返回字段契约」为准（见 DATA_SOURCES.md）。

注意：本模块不被 ``Provider`` 在 import 期直接引用；仅在
``provider.build_default_provider()`` 工厂内按需导入，避免无 akshare 环境崩溃。
"""

from __future__ import annotations

import datetime as _dt
from datetime import date, datetime
from typing import Any, Sequence

from ashare_prober.config import TZ
from ashare_prober.data.base import DataSource, SourceError
from ashare_prober.data.models import (
    Announcement,
    DragonTiger,
    ExternalQuote,
    IndexQuote,
    LimitPoolItem,
    MarketBreadth,
    MoneyFlow,
    NewsItem,
    Quote,
    SectorFlow,
)

# --------------------------------------------------------------------------- #
# 涨跌停判定阈值（口径说明）
# --------------------------------------------------------------------------- #
# 全市场快照（stock_zh_a_spot_em）只有「涨跌幅」列，没有涨跌停标记，故用阈值估算：
#   - 主板（沪 60/601/603/605，深 000/001/002/003）：±10%  → 取 ±9.8 留安全边际
#   - 创业板（30 开头）/ 科创板（688 开头）：      ±20%  → 取 ±19.8
#   - 北交所（4 / 8 / 920 开头）：                 ±30%  → 取 ±29.8
# 已知偏差（如实记录，不做修正）：
#   1) ST / *ST 股涨跌幅限制为 ±5%，会被阈值法漏计；
#   2) 新股上市首日、退市整理期等特殊涨跌幅限制同样不在上述三档内。
# 因此涨跌停**家数优先以涨跌停池长度为准**（fetch_limit_pool），
# 阈值统计仅在取不到涨跌停池时的兜底估算。
_LIMIT_THRESHOLDS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("30", "688"), 19.8),  # 创业板 / 科创板
    (("4", "8", "920"), 29.8),  # 北交所
)
_MAIN_BOARD_THRESHOLD = 9.8  # 主板兜底阈值

# --------------------------------------------------------------------------- #
# 美股指数接口候选名（实测 akshare 1.18.84）
# --------------------------------------------------------------------------- #
# 实测 1.18.84 **没有** 以下三个接口（设计书口径的历史函数名已被移除/改名）：
#   stock_us_index_spot_em / stock_us_spot_em / stock_us_index_spot_sina
# 实测可用的是 index_global_spot_em（东方财富「全球指数」实时行情，含
# 道琼斯 / 纳斯达克 / 标普500）。故按候选名依次探测，全部失败再以
# index_global_spot_em 兜底并按名称过滤出美股指数，仍失败才抛 SourceError。
_US_INDEX_CANDIDATES: tuple[str, ...] = (
    "stock_us_index_spot_em",
    "stock_us_spot_em",
    "stock_us_index_spot_sina",
)
#: ``index_global_spot_em`` 兜底时按名称过滤的美股指数关键词。
# 注意「标普」必须写成「标普500」：实测全球指数里还有「澳大利亚标普200」，
# 宽泛的「标普」会把非美股指数误收进「美股指数」类别。
_US_INDEX_NAME_KEYWORDS: tuple[str, ...] = ("道琼斯", "纳斯达克", "标普500")


def _fetch_us_index_df(ak: Any) -> Any:
    """获取美股指数行情 DataFrame：候选名依次探测 + 全球指数兜底。

    Args:
        ak: 已导入的 akshare 模块。

    Raises:
        SourceError: 所有候选接口不存在或调用失败时。
    """
    last_exc: Exception | None = None
    for func_name in _US_INDEX_CANDIDATES:
        func = getattr(ak, func_name, None)
        if func is None:
            continue  # 实测 1.18.84：前三个候选均不存在，直接跳过
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - 换下一个候选
            last_exc = exc

    # 兜底：全球指数实时行情（含美股三大指数），按名称过滤，避免把
    # 恒生 / 日经等其它市场指数误标为「美股指数」。
    func = getattr(ak, "index_global_spot_em", None)
    if func is not None:
        try:
            df = func()
            if df is not None and not df.empty and "名称" in df.columns:
                pattern = "|".join(_US_INDEX_NAME_KEYWORDS)
                df = df[df["名称"].astype(str).str.contains(pattern, na=False)]
            return df
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    raise SourceError(
        "美股指数无可用接口（候选 "
        + "/".join(_US_INDEX_CANDIDATES)
        + " 及 index_global_spot_em 均失败）",
        source="akshare",
        cause=last_exc,
    )


def _import_ak() -> Any:
    """惰性导入 akshare；失败时抛 SourceError。"""
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # pragma: no cover - 依赖未安装时
        raise SourceError(
            "akshare 未安装或导入失败，请先安装：pip install akshare",
            source="akshare",
            cause=exc,
        ) from exc
    return ak


def _market_of(symbol: str) -> str:
    """根据代码前缀推断交易所（用于 akshare 个别接口需要的 market 参数）。"""
    code = symbol.lower().lstrip("shszbj.")
    if code.startswith(("60", "68", "9", "5", "11", "113", "110")):
        return "sh"
    if code.startswith(("0", "3", "12", "15", "16", "30")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    return "sh"


def _today() -> date:
    """以北京时间返回当前交易日。"""
    return datetime.now(_dt.timezone.utc).astimezone().date()


class AkShareSource:
    """akshare 主源实现。"""

    name = "akshare"

    # ------------------------------------------------------------------ #
    # 行情
    # ------------------------------------------------------------------ #
    def fetch_quote(self, symbol: str) -> Quote:
        ak = _import_ak()
        try:
            df = ak.stock_zh_a_spot_em()
        except Exception as exc:
            raise SourceError(
                f"获取全市场实时行情失败: {exc}", source=self.name, cause=exc
            ) from exc
        code = symbol.lower().lstrip("shszbj.")
        row = df[df["代码"].astype(str).str.upper() == code.upper()]
        if row.empty:
            raise SourceError(
                f"akshare 未找到标的行情: {symbol}", source=self.name
            )
        record = row.iloc[0].to_dict()
        return Quote.from_akshare_row(record, trade_date=_today())

    # ------------------------------------------------------------------ #
    # 新闻
    # ------------------------------------------------------------------ #
    def fetch_news(self, symbol: str) -> list[NewsItem]:
        ak = _import_ak()
        try:
            df = ak.stock_news_em(symbol=symbol)
        except Exception as exc:
            raise SourceError(
                f"获取个股新闻失败: {exc}", source=self.name, cause=exc
            ) from exc
        items: list[NewsItem] = []
        for _, r in df.iterrows():
            items.append(NewsItem.from_akshare_row(r.to_dict(), symbol=symbol))
        return items

    # ------------------------------------------------------------------ #
    # 公告
    # ------------------------------------------------------------------ #
    def fetch_announcements(self, symbol: str, day: date) -> list[Announcement]:
        ak = _import_ak()
        try:
            df = ak.stock_notice_report(
                symbol=symbol, date=day.strftime("%Y%m%d")
            )
        except Exception as exc:
            raise SourceError(
                f"获取个股公告失败: {exc}", source=self.name, cause=exc
            ) from exc
        announcements: list[Announcement] = []
        for _, r in df.iterrows():
            announcements.append(
                Announcement.from_akshare_row(r.to_dict(), stock_code=symbol)
            )
        return announcements

    # ------------------------------------------------------------------ #
    # 龙虎榜
    # ------------------------------------------------------------------ #
    def fetch_dragon_tiger(
        self, symbol: str, start: date, end: date
    ) -> list[DragonTiger]:
        ak = _import_ak()
        try:
            df = ak.stock_lhb_detail_em(
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
        except Exception as exc:
            raise SourceError(
                f"获取龙虎榜失败: {exc}", source=self.name, cause=exc
            ) from exc
        code = symbol.lower().lstrip("shszbj.")
        df = df[df["代码"].astype(str).str.upper() == code.upper()]
        result: list[DragonTiger] = []
        for _, r in df.iterrows():
            rec = r.to_dict()
            net = float(rec.get("龙虎榜净买额") or 0.0)
            reason = str(rec.get("上榜原因") or "")
            day_val = _parse_day(rec.get("上榜日期"))
            result.append(
                DragonTiger.from_akshare(
                    stock_code=symbol,
                    day=day_val,
                    reason=reason,
                    net_buy=net,
                )
            )
        return result

    # ------------------------------------------------------------------ #
    # 资金流
    # ------------------------------------------------------------------ #
    def fetch_money_flow(self, symbol: str) -> MoneyFlow:
        ak = _import_ak()
        try:
            df = ak.stock_individual_fund_flow(
                stock=symbol, market=_market_of(symbol)
            )
        except Exception as exc:
            raise SourceError(
                f"获取个股资金流失败: {exc}", source=self.name, cause=exc
            ) from exc
        if df.empty:
            raise SourceError(
                f"akshare 未返回资金流: {symbol}", source=self.name
            )
        rec = df.iloc[-1].to_dict()  # 取最近一日
        return MoneyFlow.from_akshare_row(rec, trade_date=_today())

    # ------------------------------------------------------------------ #
    # 外围行情
    # ------------------------------------------------------------------ #
    def fetch_external(self, category: str) -> list[ExternalQuote]:
        ak = _import_ak()
        category = (category or "").strip()
        try:
            if "港股" in category:
                df = ak.stock_hk_index_spot_em()
            elif "美股" in category:
                df = _fetch_us_index_df(ak)
            elif "汇率" in category or "usd" in category.lower():
                df = ak.currency_usd_cny_sina()
            elif "期货" in category or "a50" in category.lower():
                df = ak.futures_zh_spot(symbol="A50")
            else:
                # 未知类别按外围指数兜底返回空列表，避免误报。
                return []
        except Exception as exc:
            raise SourceError(
                f"获取外围行情失败(category={category}): {exc}",
                source=self.name,
                cause=exc,
            ) from exc

        result: list[ExternalQuote] = []
        for _, r in df.iterrows():
            result.append(
                ExternalQuote.from_akshare_row(r.to_dict(), category=category)
            )
        return result

    # ------------------------------------------------------------------ #
    # 大盘看板：指数行情 / 市场宽度 / 板块资金流 / 涨跌停池
    # ------------------------------------------------------------------ #
    def fetch_index_quotes(self, codes: Sequence[str]) -> list[IndexQuote]:
        """获取指数行情（沪深重要指数快照），按 ``codes`` 过滤。

        取不到全部时返回已取到的部分（不抛错），由上层按缺口如实记录。

        Args:
            codes: 指数代码列表，如 ``["000001", "399001"]``。
        """
        ak = _import_ak()
        try:
            df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        except Exception as exc:
            raise SourceError(
                f"获取指数行情失败: {exc}", source=self.name, cause=exc
            ) from exc
        wanted = {str(c).strip() for c in codes}
        result: list[IndexQuote] = []
        for _, r in df.iterrows():
            row = r.to_dict()
            code = str(_pick_col(row, "代码", "code", "symbol") or "").strip()
            if wanted and code not in wanted:
                continue
            result.append(IndexQuote.from_row(row))
        # 源未返回名称时补回调用方传入的名称映射由上层处理，此处保持原样。
        return result

    def fetch_market_breadth(self) -> MarketBreadth:
        """统计全市场涨跌家数与涨跌停家数。

        口径：``stock_zh_a_spot_em()`` 全市场快照按「涨跌幅」列判定
        上涨(>0) / 下跌(<0) / 平盘(==0)；涨跌停用阈值估算，
        阈值口径见模块顶部 ``_LIMIT_THRESHOLDS`` 注释。
        """
        ak = _import_ak()
        try:
            df = ak.stock_zh_a_spot_em()
        except Exception as exc:
            raise SourceError(
                f"获取全市场行情快照失败: {exc}", source=self.name, cause=exc
            ) from exc
        if df is None or df.empty:
            raise SourceError("全市场快照为空", source=self.name)

        try:
            pct_values = _to_float_list(df["涨跌幅"].tolist())
        except Exception as exc:  # noqa: BLE001 - 列名随版本变动
            raise SourceError(
                f"全市场快照缺少「涨跌幅」列: {exc}", source=self.name, cause=exc
            ) from exc

        up = sum(1 for v in pct_values if v > 0)
        down = sum(1 for v in pct_values if v < 0)
        flat = sum(1 for v in pct_values if v == 0)

        limit_up = 0
        limit_down = 0
        codes: list[str] = []
        try:
            codes = [str(c).strip() for c in df["代码"].tolist()]
        except Exception:  # noqa: BLE001 - 取不到代码列则退化为统一主板阈值
            codes = []
        for idx, value in enumerate(pct_values):
            code = codes[idx] if idx < len(codes) else ""
            threshold = _limit_threshold(code)
            if value >= threshold:
                limit_up += 1
            elif value <= -threshold:
                limit_down += 1

        return MarketBreadth(
            up_count=up,
            down_count=down,
            flat_count=flat,
            limit_up_count=limit_up,
            limit_down_count=limit_down,
            total=len(pct_values),
        )

    def fetch_sector_flow(self, top_n: int = 10) -> list[SectorFlow]:
        """获取板块（行业）资金流：主力净流入最高 top_n + 净流出最多 top_n。

        返回顺序：先流入榜（``rank`` 为 +1..+n），后流出榜（``rank`` 为 -1..-n），
        便于上层按 ``rank`` 符号拆分两个榜单。

        Args:
            top_n: 每侧条数上限。
        """
        ak = _import_ak()
        try:
            df = ak.stock_sector_fund_flow_rank(
                indicator="今日", sector_type="行业资金流"
            )
        except Exception as exc:
            raise SourceError(
                f"获取板块资金流失败: {exc}", source=self.name, cause=exc
            ) from exc
        if df is None or df.empty:
            raise SourceError("板块资金流返回为空", source=self.name)

        rows: list[dict] = [r.to_dict() for _, r in df.iterrows()]
        parsed: list[SectorFlow] = []
        for row in rows:
            name = str(_pick_col(row, "名称", "name", "板块") or "").strip()
            inflow = _as_float(_pick_col(row, "今日主力净流入-净额", "主力净流入-净额", "主力净流入"))
            pct = _as_float(_pick_col(row, "今日涨跌幅", "涨跌幅"))
            if not name:
                continue
            parsed.append(
                SectorFlow(name=name, pct_change=pct, main_net_inflow=inflow)
            )
        if not parsed:
            raise SourceError("板块资金流无有效行", source=self.name)

        n = max(1, int(top_n))
        inflow_rank = sorted(parsed, key=lambda s: s.main_net_inflow, reverse=True)
        outflow_rank = sorted(parsed, key=lambda s: s.main_net_inflow)

        result: list[SectorFlow] = []
        for pos, item in enumerate(inflow_rank[:n], start=1):
            result.append(
                SectorFlow(
                    name=item.name,
                    pct_change=item.pct_change,
                    main_net_inflow=item.main_net_inflow,
                    rank=pos,
                )
            )
        for pos, item in enumerate(outflow_rank[:n], start=1):
            result.append(
                SectorFlow(
                    name=item.name,
                    pct_change=item.pct_change,
                    main_net_inflow=item.main_net_inflow,
                    rank=-pos,
                )
            )
        return result

    def fetch_limit_pool(self, day: date, kind: str = "up") -> list[LimitPoolItem]:
        """获取涨跌停池明细。

        Args:
            day: 交易日。
            kind: ``"up"`` 涨停池 / ``"down"`` 跌停池。

        接口兼容说明：akshare 各版本跌停池函数名不一（``stock_dt_pool_em`` 在
        ``1.18.x`` 已被移除，改名为 ``stock_zt_pool_dtgc_em``），故按候选名依次
        尝试，全部不存在时才归一为 ``SourceError`` 走降级。
        """
        ak = _import_ak()
        day_str = day.strftime("%Y%m%d")
        label = "跌停池" if kind == "down" else "涨停池"
        candidates = (
            ("stock_dt_pool_em", "stock_zt_pool_dtgc_em")
            if kind == "down"
            else ("stock_zt_pool_em",)
        )
        df = None
        last_exc: Exception | None = None
        for func_name in candidates:
            func = getattr(ak, func_name, None)
            if func is None:
                continue
            try:
                df = func(date=day_str)
                break
            except Exception as exc:  # noqa: BLE001 - 接口/网络异常统一归一
                last_exc = exc
        if df is None:
            raise SourceError(
                f"获取{label}失败(date={day_str}): "
                f"{last_exc or 'akshare 未提供可用接口 ' + '/'.join(candidates)}",
                source=self.name,
                cause=last_exc,
            ) from last_exc
        if df is None or df.empty:
            return []
        return [
            LimitPoolItem.from_akshare_row(r.to_dict(), trade_date=day)
            for _, r in df.iterrows()
        ]


def _as_float(value: Any, default: float = 0.0) -> float:
    """尽力转 float；空值 / 非数字回退 default。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "--", "None", "nan", "NaN"):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _limit_threshold(code: str) -> float:
    """按代码前缀返回该标的的涨跌停阈值（%，绝对值）。

    Args:
        code: 标的代码（纯数字，如 ``"600600"`` / ``"301001"``）。
    """
    for prefixes, threshold in _LIMIT_THRESHOLDS:
        for prefix in prefixes:
            if code.startswith(prefix):
                return threshold
    return _MAIN_BOARD_THRESHOLD


def _to_float_list(values: Any) -> list[float]:
    """将一列原始值尽力转成 float 列表；无法解析的值丢弃（不计入统计）。"""
    result: list[float] = []
    for value in list(values):
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            continue
    return result


def _pick_col(row: dict, *keys: str) -> Any:
    """按候选键顺序取第一个存在且非 None 的值。"""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _parse_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) and not isinstance(value, datetime) else value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
