"""本地查看服务（T25 http.server 包装：静态文件 + 本地 watchlist API）。

``start_viewer(output_dir, port)`` 在本地起一个服务，指向上一步生成的
``outputs/`` 目录，便于手机 / 浏览器直接打开自包含 HTML。

在纯静态服务之上，本模块额外暴露一组**仅本机回环**的监控名单 API，
供看板页（``market_dashboard.html``）在 ``prober serve`` 模式下自助增删名单：

- ``GET  /api/watchlist``       → ``{"ok":true,"items":[{code,name,added_at,active}]}``
- ``POST /api/watchlist/add``   → body ``{"query":"茅台"}``；成功 ``{"ok":true,"code":..,"name":..}``；
  歧义 409（附 ``candidates``，≤8 条）；查无 404；非法 / 落库 ValueError 400。
- ``POST /api/watchlist/remove`` → body ``{"code":"600519"}``；成功 200，未找到 404。
- ``POST /api/watchlist/active`` → body ``{"code":"600519","active":false}``；同上。

在 watchlist API 之上，本模块还暴露一组**运行时自助配置 API**（看板页「⚙️ 设置」
卡片的后端，改完即生效；DB 覆盖 > config.toml 兜底）：

- ``GET  /api/settings``       → ``{"ok":true,"sections":{llm,push,schedule}}``；
  值为 ``resolve_runtime_settings(store)`` 的**生效值**；秘密字段
  （``api_key`` / ``wecom_webhook`` / ``feishu_webhook`` / ``feishu_secret``）
  永不回明文，统一 ``{"masked": <脱敏值>, "set": bool}`` 形状。
- ``POST /api/settings``       → body 三段可选 ``{"llm":{...},"push":{...},"schedule":{...}}``；
  每段过 ``validate_section``：全对 → ``store.set_setting`` 存**原始明文**；
  有错 → 该段不落库并回 400；部分成功语义（合法段仍落库）。
  成功 ``{"ok":true,"saved":[...],"failed":{}}``；部分失败 400 附逐段 errors。
- ``POST /api/settings/reset`` → body ``{"section":"push"}``；删除该段 DB 覆盖，
  回落 config.toml 兜底值；section 非法 400。

**秘密纪律**：任何日志（含异常信息）不输出明文 secret；API 异常统一 JSON 兜底。
配置校验 / 生效值解析来自 ``ashare_prober.data.appsettings``（并行落地；缺失时
GET 自动回退 config.toml 兜底值，POST/reset 返回 503 JSON，绝不裸 500）。

除上述路径外全部走 ``SimpleHTTPRequestHandler`` 原静态逻辑（``/`` 默认命中
``index.html`` 看板页）。任何 API 异常都以 JSON 返回，绝不裸 500 堆栈。

便捷入口：
- :func:`build_server`：构造未启动的 ``HTTPServer``（单测用 ``port=0`` 取真端口）。
- :func:`start_viewer`：阻塞式启动（对外行为与旧版完全兼容）。
- :func:`start_viewer_thread`：后台线程启动，返回 ``(server, thread)``（单测/嵌入用）。
"""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: 合法标的代码：6 位纯数字（与数据层 ``add_watchlist`` 的 ValueError 口径一致）。
_CODE_RE = re.compile(r"^\d{6}$")

#: 歧义候选返回上限（看板页点选消歧，避免刷屏）。
_MAX_CANDIDATES = 8

#: 秘密字段名（永不回明文；响应里统一 ``{"masked":..,"set":..}`` 形状）。
_SECRET_FIELDS: frozenset[str] = frozenset(
    {"api_key", "wecom_webhook", "feishu_webhook", "feishu_secret"}
)

#: GET /api/settings 各段非敏感字段（原样返回；顺序即响应顺序）。
_SECTION_PLAIN_FIELDS: dict[str, tuple[str, ...]] = {
    "llm": ("base_url", "model", "temperature", "max_tokens", "timeout"),
    "push": ("enabled", "timeout"),
    "schedule": ("enabled", "scan_time", "am_scan_time", "push", "refresh_dashboard"),
}

#: 各段秘密字段（脱敏返回）。
_SECTION_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "llm": ("api_key",),
    "push": ("wecom_webhook", "feishu_webhook", "feishu_secret"),
    "schedule": (),
}


def mask_secret(value: object) -> str:
    """秘密值脱敏：空 → ``""``；长度 ≤8 → ``"****"``；否则 ``前6…后4``。

    Args:
        value: 原始秘密值（None / 非字符串一律先转字符串处理）。

    Returns:
        可安全回传给前端的脱敏字符串（**永不含明文**）。
    """
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return f"{text[:6]}…{text[-4:]}"


def _load_appsettings() -> Optional[Any]:
    """惰性加载 ``ashare_prober.data.appsettings``（并行落地前返回 ``None``）。

    校验模块必须同时具备契约三件套（``EDITABLE_SECTIONS`` / ``validate_section`` /
    ``resolve_runtime_settings``）才视为就绪；缺失时 API 自动降级，绝不崩溃。
    """
    try:
        from ashare_prober.data import appsettings as mod
    except Exception:  # noqa: BLE001 - 数据层并行开发中，缺失即降级
        return None
    required = ("EDITABLE_SECTIONS", "validate_section", "resolve_runtime_settings")
    if all(hasattr(mod, name) for name in required):
        return mod
    return None


def _settings_sections_payload(settings: Any) -> dict[str, dict]:
    """把 ``Settings`` 聚合体转为 ``GET /api/settings`` 的 ``sections`` 形状。

    非敏感字段原样返回；秘密字段转为 ``{"masked":..,"set":bool}``（见
    :func:`mask_secret`）。缺段（如异常聚合体）时该段直接不出现在响应里。
    """
    sections: dict[str, dict] = {}
    for section in ("llm", "push", "schedule"):
        obj = getattr(settings, section, None)
        if obj is None:
            continue
        payload: dict[str, Any] = {}
        for name in _SECTION_PLAIN_FIELDS.get(section, ()):  # 非敏感：原样
            payload[name] = getattr(obj, name, None)
        for name in _SECTION_SECRET_FIELDS.get(section, ()):  # 秘密：脱敏
            raw = str(getattr(obj, name, "") or "")
            payload[name] = {"masked": mask_secret(raw), "set": bool(raw)}
        sections[section] = payload
    return sections


class WatchlistAPIHandler(SimpleHTTPRequestHandler):
    """静态文件 + 监控名单 API 的请求处理器。

    ``store`` / ``resolver`` 以类属性注入（经 :func:`build_server` 动态绑定子类），
    分别对接 ``ashare_prober.data.store.Store`` 契约方法与
    ``ashare_prober.data.symbols.SymbolResolver``。
    """

    server_version = "AshareProberViewer/2.1"

    # 由 build_server 动态绑定；缺省 None 时 API 一律返回 JSON 错误而非崩溃。
    store: Any = None
    resolver: Any = None
    # 配置兜底聚合体（Settings）；None 时 GET /api/settings 走 config.toml 兜底。
    base_settings: Any = None

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802 - http.server 命名约定
        """GET 路由：``/api/watchlist``、``/api/settings`` 走 API，其余走静态逻辑。"""
        path = urlsplit(self.path).path
        if path == "/api/watchlist":
            self._handle_list()
        elif path == "/api/settings":
            self._handle_settings_get()
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server 命名约定
        """POST 路由：watchlist 三个 + settings 两个 API；其它路径一律 404 JSON。"""
        path = urlsplit(self.path).path
        if path == "/api/watchlist/add":
            self._handle_add()
        elif path == "/api/watchlist/remove":
            self._handle_remove()
        elif path == "/api/watchlist/active":
            self._handle_active()
        elif path == "/api/settings":
            self._handle_settings_save()
        elif path == "/api/settings/reset":
            self._handle_settings_reset()
        else:
            self._send_json(404, {"ok": False, "error": "unknown api endpoint"})

    # ------------------------------------------------------------------ #
    # API 实现
    # ------------------------------------------------------------------ #
    def _handle_list(self) -> None:
        """``GET /api/watchlist``：返回监控名单（业务空集也是 200 空列表）。"""
        if self.store is None:
            self._send_json(500, {"ok": False, "error": "本地库未初始化"})
            return
        try:
            raw = self.store.list_watchlist() or []
        except Exception as exc:  # noqa: BLE001 - 任何异常都以 JSON 返回
            self._send_json(500, {"ok": False, "error": f"读取监控列表失败：{exc}"})
            return
        items: list[dict] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            items.append(
                {
                    "code": str(row.get("code", "")),
                    "name": str(row.get("name", "")),
                    "added_at": row.get("added_at"),
                    "active": bool(row.get("active", True)),
                }
            )
        self._send_json(200, {"ok": True, "items": items})

    def _handle_add(self) -> None:
        """``POST /api/watchlist/add``：解析输入 → 落库；歧义/查无/非法分别处理。"""
        body = self._read_json_body()
        if body is None:
            return  # 已回 400
        query = str((body or {}).get("query") or "").strip()
        if not query:
            self._send_json(400, {"ok": False, "error": "query 不能为空"})
            return
        try:
            info = self.resolver.resolve(query)
        except Exception as exc:  # noqa: BLE001 - 按候选有无区分 409 / 404
            candidates = list(getattr(exc, "candidates", None) or [])
            if candidates:
                items = [
                    {"code": str(c.code), "name": str(c.name)}
                    for c in candidates[:_MAX_CANDIDATES]
                ]
                self._send_json(409, {"ok": False, "error": "ambiguous", "candidates": items})
            else:
                self._send_json(404, {"ok": False, "error": str(exc)})
            return
        try:
            # 新增返回 True / 更新返回 False；两者都视为成功。
            self.store.add_watchlist(info.code, info.name, active=True)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - 落库未知异常以 JSON 兜底
            self._send_json(500, {"ok": False, "error": f"写入监控列表失败：{exc}"})
            return
        self._send_json(200, {"ok": True, "code": str(info.code), "name": str(info.name)})

    def _handle_remove(self) -> None:
        """``POST /api/watchlist/remove``：按代码删除；未找到 404。"""
        body = self._read_json_body()
        if body is None:
            return
        code = str((body or {}).get("code") or "").strip()
        if not _CODE_RE.match(code):
            self._send_json(400, {"ok": False, "error": f"非法代码: {code}"})
            return
        try:
            removed = bool(self.store.remove_watchlist(code))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": f"删除监控标的失败：{exc}"})
            return
        if removed:
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": f"未找到监控标的 {code}"})

    def _handle_active(self) -> None:
        """``POST /api/watchlist/active``：暂停 / 恢复监控；未找到 404。"""
        body = self._read_json_body()
        if body is None:
            return
        code = str((body or {}).get("code") or "").strip()
        active = (body or {}).get("active")
        if not _CODE_RE.match(code):
            self._send_json(400, {"ok": False, "error": f"非法代码: {code}"})
            return
        if not isinstance(active, bool):
            self._send_json(400, {"ok": False, "error": "active 必须为布尔值"})
            return
        try:
            updated = bool(self.store.set_watchlist_active(code, active))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": f"更新监控状态失败：{exc}"})
            return
        if updated:
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": f"未找到监控标的 {code}"})

    # ------------------------------------------------------------------ #
    # 设置 API（运行时自助配置；秘密脱敏是红线）
    # ------------------------------------------------------------------ #
    def _resolve_effective_settings(self) -> Optional[Any]:
        """解析当前生效配置：DB 覆盖 > 注入兜底 > config.toml 兜底。

        数据层 ``appsettings`` 未就绪或 ``store`` 不支持设置契约时，自动降级为
        配置文件兜底值；任何异常都吞掉并降级（异常细节**不落日志**，防止秘密泄漏）。
        """
        mod = _load_appsettings()
        if (
            mod is not None
            and self.store is not None
            and hasattr(self.store, "get_setting")
        ):
            try:
                return mod.resolve_runtime_settings(self.store, self.base_settings)
            except Exception:  # noqa: BLE001 - 降级为兜底配置，不输出异常细节
                logger.debug("resolve_runtime_settings 失败，回退配置文件兜底值")
        if self.base_settings is not None:
            return self.base_settings
        from ashare_prober.config import get_settings

        return get_settings()

    def _handle_settings_get(self) -> None:
        """``GET /api/settings``：返回三段生效值；秘密字段脱敏（永不回明文）。"""
        try:
            settings = self._resolve_effective_settings()
            if settings is None:
                self._send_json(500, {"ok": False, "error": "配置不可用"})
                return
            self._send_json(
                200, {"ok": True, "sections": _settings_sections_payload(settings)}
            )
        except Exception:  # noqa: BLE001 - 统一 JSON 兜底，无裸 500
            self._send_json(500, {"ok": False, "error": "读取设置失败"})

    def _handle_settings_save(self) -> None:
        """``POST /api/settings``：逐段校验 → 合法段落库原始明文；部分失败 400。

        部分成功语义：同一请求里合法段照常 ``set_setting``，非法段收集进
        ``failed``（不落库）；只要存在失败段即整体 400。
        """
        body = self._read_json_body()
        if body is None:
            return
        mod = _load_appsettings()
        if (
            mod is None
            or self.store is None
            or not hasattr(self.store, "set_setting")
        ):
            self._send_json(
                503, {"ok": False, "error": "配置存储尚未就绪（数据层未落地）"}
            )
            return
        saved: list[str] = []
        failed: dict[str, list[str]] = {}
        editable = set(mod.EDITABLE_SECTIONS)
        # 未知段名：一律拒绝（不静默忽略，避免前端拼写错误被吞掉）。
        for key in body:
            if key not in editable:
                failed[str(key)] = ["未知配置段"]
        for section in mod.EDITABLE_SECTIONS:  # 按契约顺序处理，保证确定性
            if section not in body:
                continue
            data = body[section]
            if not isinstance(data, dict):
                failed[section] = ["配置段必须是 JSON 对象"]
                continue
            try:
                _cleaned, errors = mod.validate_section(section, data)
            except Exception:  # noqa: BLE001 - 校验器异常视为该段失败
                failed[section] = ["校验失败"]
                continue
            if errors:
                failed[section] = [str(e) for e in errors]
                continue
            try:
                # 契约：落库**原始明文**（validate 的清理结果只用于即时生效）。
                self.store.set_setting(section, data)
            except Exception:  # noqa: BLE001 - 写库失败按该段失败计
                failed[section] = ["写入失败"]
                continue
            saved.append(section)
        if failed:
            self._send_json(400, {"ok": False, "saved": saved, "failed": failed})
        else:
            self._send_json(200, {"ok": True, "saved": saved, "failed": {}})

    def _handle_settings_reset(self) -> None:
        """``POST /api/settings/reset``：删除该段 DB 覆盖，回落配置文件兜底值。"""
        body = self._read_json_body()
        if body is None:
            return
        mod = _load_appsettings()
        if (
            mod is None
            or self.store is None
            or not hasattr(self.store, "delete_setting")
        ):
            self._send_json(
                503, {"ok": False, "error": "配置存储尚未就绪（数据层未落地）"}
            )
            return
        section = str((body or {}).get("section") or "").strip()
        if section not in set(mod.EDITABLE_SECTIONS):
            self._send_json(400, {"ok": False, "error": f"未知配置段: {section}"})
            return
        try:
            self.store.delete_setting(section)
        except Exception:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": "删除配置失败"})
            return
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _read_json_body(self) -> Optional[dict]:
        """按 ``Content-Length`` 读取并解析 JSON body；失败时回 400 并返回 ``None``。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "请求体为空"})
            return None
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"ok": False, "error": f"请求体不是合法 JSON：{exc}"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
            return None
        return body

    def _send_json(self, status: int, payload: dict) -> None:
        """统一 JSON 响应（UTF-8、带 Content-Length，绝不裸栈）。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准库签名
        """降噪日志：单行输出且降为 debug 级，避免 /api 高频请求刷屏。"""
        logger.debug("%s %s", self.address_string(), format % args)


def build_server(
    output_dir: Union[str, Path],
    port: int = 8000,
    store: Optional[Any] = None,
    resolver: Optional[Any] = None,
    settings: Optional[Any] = None,
) -> HTTPServer:
    """构造绑定 ``127.0.0.1`` 的查看服务（**不启动**）。

    Args:
        output_dir: 待服务的目录（通常为 ``outputs/``）。
        port: 监听端口；``0`` 时由系统分配（从 ``server_address[1]`` 取真端口）。
        store: 本地库（实现 watchlist / settings 契约方法）；``None`` 时惰性构造默认 ``Store``。
        resolver: 标的解析器；``None`` 时惰性构造默认 ``SymbolResolver``。
        settings: 配置兜底聚合体（``Settings``）；``None`` 时 GET /api/settings
            走 ``config.get_settings()``（config.toml）兜底。纯新增可选参数，
            不影响既有调用方（cli.py 位置参数兼容）。

    Returns:
        未启动的 ``HTTPServer`` 实例。
    """
    directory = str(Path(output_dir).resolve())
    # 惰性构造默认依赖：不在 import 期触碰 sqlite / 标的表，保持启动轻量。
    if store is None:
        from ashare_prober.data.store import Store

        store = Store()
    if resolver is None:
        from ashare_prober.data.symbols import SymbolResolver

        resolver = SymbolResolver()
    # 用动态子类绑定实例级依赖（SimpleHTTPRequestHandler 不接受额外 kwargs）。
    handler_cls = type(
        "BoundWatchlistHandler",
        (WatchlistAPIHandler,),
        {"store": store, "resolver": resolver, "base_settings": settings},
    )
    handler = functools.partial(handler_cls, directory=directory)
    # 仅本机回环，避免暴露到局域网。
    return HTTPServer(("127.0.0.1", port), handler)


def start_viewer(
    output_dir: Union[str, Path],
    port: int = 8000,
    store: Optional[Any] = None,
    resolver: Optional[Any] = None,
    settings: Optional[Any] = None,
) -> None:
    """启动本地查看服务（静态文件 + watchlist / settings API），阻塞直到 Ctrl+C。

    Args:
        output_dir: 待服务的目录（通常为 ``outputs/``）。
        port: 监听端口，默认 8000。
        store: 本地库（可选）；缺省惰性构造 ``ashare_prober.data.store.Store()``。
        resolver: 标的解析器（可选）；缺省惰性构造 ``SymbolResolver()``。
        settings: 配置兜底聚合体（可选）；缺省走 ``config.get_settings()``。
    """
    build_server(
        output_dir, port=port, store=store, resolver=resolver, settings=settings
    ).serve_forever()


def start_viewer_thread(
    output_dir: Union[str, Path],
    port: int = 8000,
    store: Optional[Any] = None,
    resolver: Optional[Any] = None,
    settings: Optional[Any] = None,
) -> tuple[HTTPServer, threading.Thread]:
    """在后台守护线程启动查看服务，立即返回 ``(server, thread)``。

    主要供单测 / 嵌入使用（``port=0`` 时经 ``server.server_address[1]`` 取真端口）。
    """
    httpd = build_server(
        output_dir, port=port, store=store, resolver=resolver, settings=settings
    )
    thread = threading.Thread(target=httpd.serve_forever, name="prober-viewer", daemon=True)
    thread.start()
    return httpd, thread
