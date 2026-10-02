"""监控名单真源解析（通用方案：代码中不写死任何股票代码）。

优先级：
1. ``store.list_watchlist(active_only=True)`` 非空 → 返回其 code 列表
   （**DB 是唯一真源**，页面上刚加的票立即可被扫描）；
2. 否则取 ``settings.watchlist.symbols`` 作为**兜底**（``WatchlistSettings``
   实现了 ``__iter__``，可直接迭代）；
3. 仍为空 → ``[]``（不报错、不编造任何标的）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["resolve_watchlist"]


def resolve_watchlist(store: Any, settings: Any | None = None) -> list[str]:
    """解析本轮扫描应覆盖的标的列表。

    Args:
        store: 本地库（需提供 ``list_watchlist`` 方法；可为 ``None``，
            此时直接走配置兜底）。
        settings: 全局配置对象；``None`` 时取 ``get_settings()``。

    Returns:
        6 位代码字符串列表；DB 名单与配置兜底都为空时返回 ``[]``。
    """
    # 1) DB 名单优先（唯一真源）。
    if store is not None:
        try:
            rows = store.list_watchlist(active_only=True)
        except Exception:  # noqa: BLE001 - 名单读取失败时降级走配置兜底
            rows = []
        codes = [
            str(row.get("code", "")).strip()
            for row in rows
            if isinstance(row, dict)
        ]
        codes = [c for c in codes if c]
        if codes:
            return codes

    # 2) 配置兜底（[watchlist].symbols）。
    if settings is None:
        from ashare_prober.config import get_settings

        settings = get_settings()

    watchlist = getattr(settings, "watchlist", None)
    symbols: Any = getattr(watchlist, "symbols", None)
    if symbols is None and watchlist is not None:
        # WatchlistSettings 实现了 __iter__，兼容直接迭代。
        symbols = list(watchlist)
    if not symbols:
        # 3) 名单仍为空：不报错、不编造。
        return []
    return [str(s).strip() for s in symbols if str(s).strip()]
