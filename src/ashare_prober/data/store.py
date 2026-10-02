"""本地库（T05）。

基于 Python 标准库 ``sqlite3`` 实现轻量本地存储（默认 ``data/prober.db``），
避免引入 MySQL / Redis（见 ARCHITECTURE 第 1.2 节）。

表设计：
- ``raw_*``：原始缓存（序列化后的模型 JSON），用于回源与复盘。
- ``fact_*``：清洗后的事实表，列化存储，便于查询与相似案例检索。

本模块对外提供：``save_quote / get_quote_raw``、``save_news / get_news``，
以及通用 ``upsert(table, row, pk)`` 供上层按需扩展。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ashare_prober.config import DEFAULT_DB_PATH
from ashare_prober.anomaly.models import AnomalyEvent  # 异动事件持久化（无循环依赖）
# 注意：EventDossier 不在此处顶层导入，改为 save_dossier/get_dossier 内惰性导入，
# 以避免 data.__init__ → provider → store → attribution.models → data.models →
# data.__init__ 的循环导入。运行时调用路径下 data 包已完全初始化，惰性导入安全。
from ashare_prober.data.models import NewsItem, Quote


class Store:
    """本地 SQLite 存储。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def init_schema(self) -> None:
        """创建 raw_*/fact_* 表（幂等）。"""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_quote (
                symbol     TEXT NOT NULL,
                date       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (symbol, date)
            );

            CREATE TABLE IF NOT EXISTS fact_quote (
                symbol     TEXT NOT NULL,
                date       TEXT NOT NULL,
                name       TEXT,
                open       REAL,
                high       REAL,
                low        REAL,
                close      REAL,
                pre_close  REAL,
                volume     REAL,
                amount     REAL,
                pct_change REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, date)
            );

            CREATE TABLE IF NOT EXISTS raw_news (
                id         TEXT PRIMARY KEY,
                payload    TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fact_news (
                id           TEXT PRIMARY KEY,
                source       TEXT,
                title        TEXT,
                url          TEXT,
                published_at TEXT,
                summary      TEXT,
                symbols      TEXT,            -- JSON list[str]
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fact_anomaly_event (
                id           TEXT PRIMARY KEY,
                scope        TEXT,
                ref_code     TEXT,
                ref_name     TEXT,
                type         TEXT,
                level        TEXT,
                detected_at  TEXT,
                status       TEXT,
                rule_id      TEXT,
                meta_status  TEXT,
                payload      TEXT NOT NULL     -- 事件模型 JSON（完整还原）
            );

            CREATE TABLE IF NOT EXISTS fact_event_dossier (
                id               TEXT PRIMARY KEY,
                anomaly_event_id TEXT,
                version          INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT,
                payload          TEXT NOT NULL     -- 事件档案 JSON（完整还原）
            );

            CREATE TABLE IF NOT EXISTS fact_research_report (
                id           TEXT PRIMARY KEY,
                dossier_id   TEXT,
                created_at   TEXT,
                model        TEXT,
                payload      TEXT NOT NULL     -- 研判报告 JSON（完整还原）
            );

            CREATE TABLE IF NOT EXISTS fact_approval_request (
                id             TEXT PRIMARY KEY,
                report_id      TEXT,
                status         TEXT,
                created_at     TEXT,
                decided_at     TEXT,
                decision_note  TEXT,
                approver       TEXT,
                payload        TEXT NOT NULL   -- 人审申请 JSON（完整还原）
            );

            CREATE TABLE IF NOT EXISTS user_watchlist (
                code      TEXT PRIMARY KEY,        -- 6 位代码
                name      TEXT NOT NULL DEFAULT '',
                added_at  TEXT NOT NULL,           -- ISO 时间
                active    INTEGER NOT NULL DEFAULT 1   -- 1=监控中 0=已暂停
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,   -- "llm" / "push" / "schedule"
                value      TEXT NOT NULL,      -- 该段配置的 JSON 对象
                updated_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 通用 upsert
    # ------------------------------------------------------------------ #
    def upsert(self, table: str, row: dict[str, Any], pk: list[str]) -> None:
        """通用写入：按主键幂等插入或更新。

        Args:
            table: 表名。
            row: 列名 -> 值。
            pk: 主键列名列表（用于 ``ON CONFLICT`` 判定）。
        """
        if not row:
            return
        columns = list(row.keys())
        placeholders = ", ".join("?" for _ in columns)
        col_sql = ", ".join(columns)
        update_sql = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in pk)
        conflict_sql = ", ".join(pk)
        if update_sql:
            sql = (
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict_sql}) DO UPDATE SET {update_sql}"
            )
        else:
            sql = (
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict_sql}) DO NOTHING"
            )
        self._conn.execute(sql, [row[c] for c in columns])
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 行情：save_quote / get_quote_raw
    # ------------------------------------------------------------------ #
    def save_quote(self, quote: Quote) -> None:
        """落库个股行情（raw + fact 双写）。"""
        day_str = quote.date.isoformat() if quote.date else ""
        now = _now_iso()
        # 原始缓存
        self.upsert(
            "raw_quote",
            {
                "symbol": quote.symbol,
                "date": day_str,
                "payload": quote.model_dump_json(),
                "fetched_at": now,
            },
            pk=["symbol", "date"],
        )
        # 清洗事实
        self.upsert(
            "fact_quote",
            {
                "symbol": quote.symbol,
                "date": day_str,
                "name": quote.name,
                "open": quote.open,
                "high": quote.high,
                "low": quote.low,
                "close": quote.close,
                "pre_close": quote.pre_close,
                "volume": quote.volume,
                "amount": quote.amount,
                "pct_change": quote.pct_change,
                "updated_at": now,
            },
            pk=["symbol", "date"],
        )

    def get_quote_raw(self, symbol: str, day: date) -> Optional[Quote]:
        """从 raw_quote 读取原始行情；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT payload FROM raw_quote WHERE symbol = ? AND date = ?",
            (symbol, day.isoformat()),
        ).fetchone()
        if row is None:
            return None
        return Quote.model_validate_json(row["payload"])

    # ------------------------------------------------------------------ #
    # 新闻：save_news / get_news
    # ------------------------------------------------------------------ #
    def save_news(self, items: Iterable[NewsItem]) -> int:
        """落库个股新闻（raw + fact 双写），返回写入条数。"""
        count = 0
        now = _now_iso()
        for item in items:
            item_id = item.id or _make_news_id(item)
            self.upsert(
                "raw_news",
                {
                    "id": item_id,
                    "payload": item.model_dump_json(),
                    "fetched_at": now,
                },
                pk=["id"],
            )
            pub = item.published_at.isoformat() if item.published_at else ""
            self.upsert(
                "fact_news",
                {
                    "id": item_id,
                    "source": item.source,
                    "title": item.title,
                    "url": item.url,
                    "published_at": pub,
                    "summary": item.summary,
                    "symbols": json.dumps(item.symbols, ensure_ascii=False),
                    "updated_at": now,
                },
                pk=["id"],
            )
            count += 1
        return count

    def get_news(
        self,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list[NewsItem]:
        """读取新闻；可按标的相关性 / 发布时间过滤。"""
        # 从 raw_news 读取序列化后的原始模型，保证字段无损还原。
        rows = self._conn.execute("SELECT payload FROM raw_news").fetchall()
        result: list[NewsItem] = []
        for row in rows:
            item = NewsItem.model_validate_json(row["payload"])
            if symbol and symbol not in item.symbols:
                continue
            if since and (item.published_at is None or item.published_at < since):
                continue
            result.append(item)
        return result

    # ------------------------------------------------------------------ #
    # 异动事件：save_event / get_event（T11 引擎落库）
    # ------------------------------------------------------------------ #
    def save_event(self, event: AnomalyEvent) -> None:
        """落库单个异动事件（按 id 幂等 upsert）。"""
        detected_at = event.detected_at
        detected_iso = (
            detected_at.isoformat()
            if isinstance(detected_at, datetime)
            else str(detected_at)
        )
        self.upsert(
            "fact_anomaly_event",
            {
                "id": event.id,
                "scope": event.scope,
                "ref_code": event.ref_code,
                "ref_name": event.ref_name,
                "type": event.type,
                "level": event.level,
                "detected_at": detected_iso,
                "status": event.status,
                "rule_id": event.rule_id,
                "meta_status": event.meta_status,
                "payload": event.model_dump_json(),
            },
            pk=["id"],
        )

    def get_event(self, event_id: str) -> Optional[AnomalyEvent]:
        """按 id 读取异动事件；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT payload FROM fact_anomaly_event WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return AnomalyEvent.model_validate_json(row["payload"])

    def list_events(
        self, limit: int = 10, since: Optional[datetime] = None
    ) -> list[AnomalyEvent]:
        """按 ``detected_at`` 倒序读取异动事件（供大盘看板「近期异动」使用）。

        Args:
            limit: 返回条数上限。
            since: 起始时间过滤（含）；``None`` 表示不过滤。
        """
        sql = "SELECT payload FROM fact_anomaly_event"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE detected_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY detected_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [AnomalyEvent.model_validate_json(row["payload"]) for row in rows]

    # ------------------------------------------------------------------ #
    # 事件档案：save_dossier / get_dossier（T17 归因落库）
    # ------------------------------------------------------------------ #
    def save_dossier(self, dossier: "EventDossier") -> None:
        """落库单个事件档案（按 id 幂等 upsert）。"""
        from ashare_prober.attribution.models import EventDossier  # 惰性导入，避免循环依赖

        created_at = dossier.created_at
        created_iso = (
            created_at.isoformat()
            if isinstance(created_at, datetime)
            else str(created_at)
        )
        self.upsert(
            "fact_event_dossier",
            {
                "id": dossier.id,
                "anomaly_event_id": dossier.anomaly_event_id,
                "version": int(dossier.version),
                "created_at": created_iso,
                "payload": dossier.model_dump_json(),
            },
            pk=["id"],
        )

    def get_dossier(self, dossier_id: str) -> Optional["EventDossier"]:
        """按 id 读取事件档案；不存在返回 None。"""
        from ashare_prober.attribution.models import EventDossier  # 惰性导入，避免循环依赖

        row = self._conn.execute(
            "SELECT payload FROM fact_event_dossier WHERE id = ?", (dossier_id,)
        ).fetchone()
        if row is None:
            return None
        return EventDossier.model_validate_json(row["payload"])

    def get_dossier_by_event(self, event_id: str) -> Optional["EventDossier"]:
        """按关联异动事件 ID 读取事件档案；不存在返回 None。

        Args:
            event_id: 异动事件 ID（``AnomalyEvent.id``，形如 ``EVT-<code>-<date>-<seq>``）。
        """
        from ashare_prober.attribution.models import EventDossier  # 惰性导入，避免循环依赖

        row = self._conn.execute(
            "SELECT payload FROM fact_event_dossier WHERE anomaly_event_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return EventDossier.model_validate_json(row["payload"])

    # ------------------------------------------------------------------ #
    # 研判报告：save_report / get_report（T20）
    # ------------------------------------------------------------------ #
    def save_report(self, report: "ResearchReport") -> None:
        """落库研判报告（按 id 幂等 upsert）。"""
        from ashare_prober.research.models import ResearchReport  # 惰性导入，避免循环依赖

        created_at = report.created_at
        created_iso = (
            created_at.isoformat()
            if isinstance(created_at, datetime)
            else str(created_at)
        )
        self.upsert(
            "fact_research_report",
            {
                "id": report.id,
                "dossier_id": report.dossier_id,
                "created_at": created_iso,
                "model": report.model,
                "payload": report.model_dump_json(),
            },
            pk=["id"],
        )

    def get_report(self, report_id: str) -> Optional["ResearchReport"]:
        """按 id 读取研判报告；不存在返回 None。"""
        from ashare_prober.research.models import ResearchReport  # 惰性导入，避免循环依赖

        row = self._conn.execute(
            "SELECT payload FROM fact_research_report WHERE id = ?", (report_id,)
        ).fetchone()
        if row is None:
            return None
        return ResearchReport.model_validate_json(row["payload"])

    def get_report_by_dossier(self, dossier_id: str) -> Optional["ResearchReport"]:
        """按关联事件档案 ID 读取研判报告；不存在返回 None。

        Args:
            dossier_id: 事件档案 ID（``EventDossier.id``）。若存在多版本研判，取最新一条。
        """
        from ashare_prober.research.models import ResearchReport  # 惰性导入，避免循环依赖

        row = self._conn.execute(
            "SELECT payload FROM fact_research_report WHERE dossier_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (dossier_id,),
        ).fetchone()
        if row is None:
            return None
        return ResearchReport.model_validate_json(row["payload"])

    # ------------------------------------------------------------------ #
    # 人审申请：save_approval / get_approval（T21）
    # ------------------------------------------------------------------ #
    def save_approval(self, request: "ApprovalRequest") -> None:
        """落库人审申请（按 id 幂等 upsert）。"""
        from ashare_prober.research.models import ApprovalRequest  # 惰性导入，避免循环依赖

        created_at = request.created_at
        created_iso = (
            created_at.isoformat()
            if isinstance(created_at, datetime)
            else str(created_at)
        )
        decided_at = request.decided_at
        decided_iso = (
            decided_at.isoformat()
            if isinstance(decided_at, datetime)
            else (str(decided_at) if decided_at is not None else None)
        )
        self.upsert(
            "fact_approval_request",
            {
                "id": request.id,
                "report_id": request.report_id,
                "status": request.status,
                "created_at": created_iso,
                "decided_at": decided_iso,
                "decision_note": request.decision_note,
                "approver": request.approver,
                "payload": request.model_dump_json(),
            },
            pk=["id"],
        )

    def get_approval(self, request_id: str) -> Optional["ApprovalRequest"]:
        """按 id 读取人审申请；不存在返回 None。"""
        from ashare_prober.research.models import ApprovalRequest  # 惰性导入，避免循环依赖

        row = self._conn.execute(
            "SELECT payload FROM fact_approval_request WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        return ApprovalRequest.model_validate_json(row["payload"])

    def get_approval_by_report(self, report_id: str) -> Optional["ApprovalRequest"]:
        """按关联研判报告 ID 读取人审申请；不存在返回 None。

        Args:
            report_id: 研判报告 ID（``ResearchReport.id``）。
        """
        from ashare_prober.research.models import ApprovalRequest  # 惰性导入，避免循环依赖

        row = self._conn.execute(
            "SELECT payload FROM fact_approval_request WHERE report_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (report_id,),
        ).fetchone()
        if row is None:
            return None
        return ApprovalRequest.model_validate_json(row["payload"])

    # ------------------------------------------------------------------ #
    # 用户自选名单：user_watchlist CRUD（页面 / CLI 管理的数据层）
    # ------------------------------------------------------------------ #
    def add_watchlist(self, code: str, name: str = "", active: bool = True) -> bool:
        """把标的加入用户自选名单。

        Args:
            code: 股票代码（允许 ``sh600519`` / ``600519.SH`` 等形态，
                内部规范化为 6 位纯数字；非法输入抛 ``ValueError``）。
            name: 标的名称（可选，默认空串）。
            active: 是否监控中（默认 ``True``）。

        Returns:
            新插入返回 ``True``；code 已存在则更新并返回 ``False``。
            重复添加会重新激活（active 按传入值置位），且**仅在新名称非空时
            更新名称**——以纯代码形态重复添加不会抹掉已存的 name。
        """
        norm = _norm_code(code)
        existing = self._conn.execute(
            "SELECT code FROM user_watchlist WHERE code = ?", (norm,)
        ).fetchone()
        if existing is not None:
            if name:
                self._conn.execute(
                    "UPDATE user_watchlist SET name = ?, active = ? WHERE code = ?",
                    (name, int(active), norm),
                )
            else:
                # name 为空：保留原名称，仅更新 active 位。
                self._conn.execute(
                    "UPDATE user_watchlist SET active = ? WHERE code = ?",
                    (int(active), norm),
                )
            self._conn.commit()
            return False
        self._conn.execute(
            "INSERT INTO user_watchlist (code, name, added_at, active) "
            "VALUES (?, ?, ?, ?)",
            (norm, name, _now_iso(), int(active)),
        )
        self._conn.commit()
        return True

    def remove_watchlist(self, code: str) -> bool:
        """把标的从用户自选名单删除。

        Args:
            code: 股票代码（自动规范化为 6 位；非法输入抛 ``ValueError``）。

        Returns:
            存在并删除返回 ``True``；不存在返回 ``False``。
        """
        norm = _norm_code(code)
        cur = self._conn.execute(
            "DELETE FROM user_watchlist WHERE code = ?", (norm,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def set_watchlist_active(self, code: str, active: bool) -> bool:
        """把标的在名单中的 ``active`` 位置位 / 复位（暂停与恢复监控）。

        Args:
            code: 股票代码（自动规范化为 6 位；非法输入抛 ``ValueError``）。
            active: ``True`` 恢复监控；``False`` 暂停监控。

        Returns:
            行存在并更新返回 ``True``；行不存在返回 ``False``。
        """
        norm = _norm_code(code)
        cur = self._conn.execute(
            "UPDATE user_watchlist SET active = ? WHERE code = ?",
            (int(active), norm),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_watchlist(self, active_only: bool = False) -> list[dict]:
        """列出用户自选名单。

        Args:
            active_only: ``True`` 时仅返回 ``active=1``（监控中）的标的。

        Returns:
            字典列表 ``[{"code": ..., "name": ..., "added_at": ..., "active": bool}]``；
            按 ``added_at`` 降序（后加的在前）。
        """
        sql = (
            "SELECT code, name, added_at, active FROM user_watchlist"
        )
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY added_at DESC"
        rows = self._conn.execute(sql).fetchall()
        return [
            {
                "code": row["code"],
                "name": row["name"],
                "added_at": row["added_at"],
                "active": bool(row["active"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # 页面自助配置：app_settings KV（页面配置「即改即生效」的数据层）
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str) -> dict | None:
        """读取一个配置段的覆盖 JSON。

        Args:
            key: 配置段名（``"llm"`` / ``"push"`` / ``"schedule"``）。

        Returns:
            覆盖内容 dict；无记录、JSON 损坏或非对象时返回 ``None``（不抛）。
        """
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (str(key),)
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["value"])
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def set_setting(self, key: str, data: dict) -> None:
        """写入 / 覆盖一个配置段（upsert，``value`` 为 JSON 文本）。

        Args:
            key: 配置段名。
            data: 该段配置的 JSON 对象（dict）。
        """
        self._conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (str(key), json.dumps(data, ensure_ascii=False), _now_iso()),
        )
        self._conn.commit()

    def delete_setting(self, key: str) -> bool:
        """删除一个配置段覆盖（恢复为配置文件默认值）。

        Args:
            key: 配置段名。

        Returns:
            存在并删除返回 ``True``；不存在返回 ``False``。
        """
        cur = self._conn.execute(
            "DELETE FROM app_settings WHERE key = ?", (str(key),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_setting_keys(self) -> list[str]:
        """列出现有覆盖的配置段名（按字典序）。

        Returns:
            段名列表，如 ``["llm", "push"]``。
        """
        rows = self._conn.execute(
            "SELECT key FROM app_settings ORDER BY key"
        ).fetchall()
        return [str(row["key"]) for row in rows]

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


#: 6 位纯数字股票代码形态。
_CODE_RE = re.compile(r"^\d{6}$")


def _norm_code(code: str) -> str:
    """把用户输入的代码规范化为 6 位纯数字（与 ``cli._strip_market`` 思路一致）。

    支持 ``sh600519`` / ``600519.SH`` / ``SZ000001`` / `` 600519 `` 等形态；
    非法输入（中文、位数不足、非数字等）抛 ``ValueError``。

    Note:
        不 import ``cli``，避免 data → cli 的反向依赖。
    """
    t = (code or "").strip().lower()
    t = re.sub(r"^(sh|sz|bj)\.?", "", t)
    t = re.sub(r"\.(sh|sz|bj)$", "", t)
    if not _CODE_RE.match(t):
        raise ValueError(f"非法股票代码: {code!r}（需为 6 位数字，如 600519）")
    return t


def _make_news_id(item: NewsItem) -> str:
    """为缺失 id 的新闻生成一个稳定 ID（标题 + 时间哈希）。"""
    base = f"{item.source}|{item.title}|{item.published_at}"
    return f"NEWS-{abs(hash(base))}"
