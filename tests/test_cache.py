"""TTL 缓存单测：命中 / 过期 / 永久 / 跨进程。"""

from __future__ import annotations

from ashare_prober.data.base import CacheMiss
from ashare_prober.data.cache import Cache


def test_set_and_get_hit(tmp_path) -> None:  # noqa: ANN001
    cache = Cache(tmp_path / "cache.db")
    cache.set("k", {"v": 1}, ttl=600)
    assert cache.get("k", ttl=600) == {"v": 1}


def test_miss_when_absent(tmp_path) -> None:  # noqa: ANN001
    cache = Cache(tmp_path / "cache.db")
    try:
        cache.get("nope")
        raise AssertionError("expected CacheMiss")
    except CacheMiss:
        pass


def test_expiry_via_monkeypatch(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    cache = Cache(tmp_path / "cache.db")
    now = 1_000_000.0
    monkeypatch.setattr("ashare_prober.data.cache.time.time", lambda: now)

    cache.set("k", "value", ttl=600)
    assert cache.get("k", ttl=600) == "value"  # 未过期：命中

    monkeypatch.setattr("ashare_prober.data.cache.time.time", lambda: now + 601)
    try:
        cache.get("k", ttl=600)
        raise AssertionError("expected CacheMiss after expiry")
    except CacheMiss:
        pass

    # 重新写入后再次命中（覆盖旧过期条目）
    cache.set("k", "fresh", ttl=600)
    assert cache.get("k", ttl=600) == "fresh"


def test_permanent_never_expires(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    cache = Cache(tmp_path / "cache.db")
    now = 2_000_000.0
    monkeypatch.setattr("ashare_prober.data.cache.time.time", lambda: now)
    cache.set("p", [1, 2, 3], ttl=None)  # 永久
    monkeypatch.setattr(
        "ashare_prober.data.cache.time.time", lambda: now + 10**9
    )
    assert cache.get("p") == [1, 2, 3]


def test_stricter_get_ttl(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    cache = Cache(tmp_path / "cache.db")
    now = 3_000_000.0
    monkeypatch.setattr("ashare_prober.data.cache.time.time", lambda: now)
    cache.set("k", "v", ttl=600)

    # 推进 100s：对 600s 窗口仍新鲜，对 10s 窗口已过期
    monkeypatch.setattr("ashare_prober.data.cache.time.time", lambda: now + 100)
    assert cache.get("k", ttl=600) == "v"  # 宽松窗口：命中
    try:
        cache.get("k", ttl=10)  # 严格窗口：过期
        raise AssertionError("expected CacheMiss with stricter ttl")
    except CacheMiss:
        pass


def test_cross_process_persistence(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "cache.db"
    c1 = Cache(path)
    c1.set("shared", {"a": 1}, ttl=3600)
    c1.close()

    c2 = Cache(path)  # 新连接，应从 SQLite 载入
    assert c2.get("shared", ttl=3600) == {"a": 1}
