"""TTL 缓存层（T05）。

轻量缓存，基于「内存字典（快速命中）+ SQLite（跨进程持久化）」双层实现，
避免引入 Redis 等重基建（见 ARCHITECTURE 第 1.2 节选型）。

语义：
- ``set(key, value, ttl)``：写入；``ttl`` 为存活秒数，``None`` 表示永久（用于日线/公告）。
- ``get(key, ttl=None)``：命中且未过期返回 value；否则抛 ``CacheMiss``。
  若 ``get`` 显式传入 ``ttl``，则以「更严格的窗口」重新判定新鲜度。
- value 须为 JSON 可序列化对象（pydantic 模型请先 ``model_dump(mode="json")``）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from ashare_prober.data.base import CacheMiss

# 内存条目结构：(stored_at, ttl, value)
_MemEntry = tuple[float, Optional[int], Any]


class Cache:
    """基于内存 + SQLite 的 TTL 缓存。"""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        """初始化缓存。

        Args:
            db_path: SQLite 数据库路径；``None`` 使用内存库（``:memory:``，进程内有效）。
        """
        self._mem: dict[str, _MemEntry] = {}
        self._conn = sqlite3.connect(
            ":memory:" if db_path is None else str(db_path),
            check_same_thread=False,
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key       TEXT PRIMARY KEY,
                value     TEXT NOT NULL,
                stored_at REAL NOT NULL,
                ttl       REAL            -- NULL 表示永久
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """写入缓存；``ttl=None`` 表示永久不过期。"""
        now = time.time()
        payload = json.dumps(value, ensure_ascii=False)
        self._mem[key] = (now, ttl, value)
        self._conn.execute(
            "INSERT INTO cache (key, value, stored_at, ttl) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "stored_at=excluded.stored_at, ttl=excluded.ttl",
            (key, payload, now, ttl),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get(self, key: str, ttl: Optional[int] = None) -> Any:
        """读取缓存；命中且新鲜返回 value，否则抛 ``CacheMiss``。

        Args:
            key: 缓存键。
            ttl: 可选的新鲜度窗口（秒）。若提供，将替代写入时的 ttl 做更严格判定。
        """
        now = time.time()
        entry = self._mem.get(key)
        if entry is None:
            entry = self._load_from_db(key)
        if entry is None:
            raise CacheMiss(key)

        stored_at, stored_ttl, value = entry
        effective_ttl = ttl if ttl is not None else stored_ttl
        if effective_ttl is not None and (now - stored_at) > effective_ttl:
            # 过期：清理后视为未命中。
            self._invalidate(key)
            raise CacheMiss(key)
        return value

    def _load_from_db(self, key: str) -> Optional[_MemEntry]:
        """从 SQLite 载入并回填内存；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT value, stored_at, ttl FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        payload, stored_at, stored_ttl = row
        value: Any = json.loads(payload)
        entry: _MemEntry = (stored_at, None if stored_ttl is None else int(stored_ttl), value)
        self._mem[key] = entry
        return entry

    def _invalidate(self, key: str) -> None:
        self._mem.pop(key, None)
        self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """清空全部缓存（内存 + 数据库）。"""
        self._mem.clear()
        self._conn.execute("DELETE FROM cache")
        self._conn.commit()

    def close(self) -> None:
        """关闭底层数据库连接。"""
        self._conn.close()
