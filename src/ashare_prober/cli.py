"""命令行入口（T25）。

提供 typer 子命令：
- ``version`` / ``status``：保留的元信息命令。
- ``run``：单标的手动触发「异动 → 归因 → 研判 → 人审 → 渲染」最小闭环。
- ``approve``：对研判申请进行批准 / 驳回，并重渲染反映状态的 HTML。
- ``render``：从本地库取出事件档案并渲染 HTML。
- ``dashboard``：生成大盘数据看板（``outputs/index.html``，即默认页）。
- ``search``：按名称 / 简称 / 代码模糊搜索标的，用于确认代码与消歧。
- ``push``：向已配置通道（企业微信 / 飞书）推送消息。
- ``similar``：按事件 ID 检索本地库中的相似历史案例（T16）。
- ``backtest``：历史日线回测复核异动规则（T26）。
- ``schedule``：定时调度「扫描 watchlist → 推送 → 刷新看板」（T12）。
- ``watchlist``：监控名单管理（增/删/暂停/恢复，真源为本地库 user_watchlist 表）。
- ``serve``：本地 http.server 起一个指 ``outputs/`` 的静态服务（默认页为看板，
  并提供 ``/api/watchlist`` 供页面自助增删名单）。

设计：每一步单点失败都给出清晰错误并安全退出，不崩溃；核心依赖在命令体内惰性导入，
避免无谓的模块加载成本与可选依赖缺失导致的导入期失败。
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Optional

import typer

from ashare_prober.data import Store, build_default_provider

app = typer.Typer(
    name="prober",
    help="A-Share Anomaly Prober —— A 股异动「前因后果」探测与多视角研判工具。",
    no_args_is_help=True,
)

# 默认输出目录（与 renderer / viewer 约定一致）
DEFAULT_OUTPUT_DIR = Path("outputs")


@app.command(help="显示版本号")
def version() -> None:
    """打印当前版本号。"""
    from ashare_prober import __version__

    typer.echo(f"ashare-anomaly-prober {__version__}")


@app.command(help="显示运行状态")
def status() -> None:
    """显示当前阶段能力概览。"""
    typer.echo(
        "ashare-anomaly-prober：数据层 / 异动 / 归因 / 研判 / 展示 均已就绪（T01–T25）。"
    )


def _parse_date(text: str) -> _dt.date:
    """将 CLI 传入的日期字符串解析为 date（支持 2026-10-09 / 20261009）。"""
    return _dt.datetime.strptime(text, "%Y-%m-%d").date()


# 6 位纯数字代码形态
_CODE_RE = re.compile(r"^\d{6}$")


def _strip_market(text: str) -> str:
    """去掉输入里的交易所前缀/后缀，返回纯代码或原文。

    支持 ``sh600519`` / ``600519.SH`` / ``sz000001`` / ``000001.sz`` 等形态；
    非代码形态（中文名、简称）原样返回，交由标的表解析。
    """
    t = (text or "").strip().lower()
    t = re.sub(r"^(sh|sz|bj)\.?", "", t)
    t = re.sub(r"\.(sh|sz|bj)$", "", t)
    return t


def _looks_like_code(text: str) -> bool:
    """输入是否已是可直接使用的 6 位代码形态（允许带交易所前缀/后缀）。"""
    return bool(_CODE_RE.match(_strip_market(text)))


def _resolve_symbol(query: str) -> str:
    """把用户输入的「名称 / 简称 / 代码 / 带前缀代码」归一为 6 位代码。

    - 若输入已是 6 位代码形态（含 ``sh600519``、``600519.SH``），**直接返回，不查标的表**
      ——保证离线可用，也让既有测试不触发网络。
    - 其余（中文名、简称、不足 6 位的代码片段）才查标的表；失败时给出候选提示。
    """
    if _looks_like_code(query):
        return _strip_market(query)
    try:
        from ashare_prober.data.symbols import SymbolNotFound, SymbolResolver

        return SymbolResolver().resolve(query).code
    except SymbolNotFound as exc:
        hint = ""
        candidates = getattr(exc, "candidates", None) or []
        if candidates:
            hint = "；你是否要找：" + "、".join(
                f"{s.name}({s.prefixed})" for s in candidates[:5]
            )
        typer.echo(f"[错误] 无法解析标的：{query}{hint}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001 - 名单拉取失败也给出可操作提示
        typer.echo(
            f"[错误] 标的解析失败：{exc}（首次按名称解析需联网拉取 A 股名单）",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command(help="单标的手动触发异动→归因→研判→人审→渲染")
def run(
    symbol: str = typer.Option(..., "--symbol", help="标的代码，如 600600"),
    date: str = typer.Option(..., "--date", help="判定交易日，如 2026-10-09"),
    scope: str = typer.Option("stock", "--scope", help="作用域：index/sector/stock/limit_pool"),
) -> None:
    """端到端最小闭环：触发异动并产出可手机查看的自包含 HTML。"""
    try:
        day = _parse_date(date)
    except ValueError:
        typer.echo(f"[错误] 日期格式应为 YYYY-MM-DD，收到：{date}", err=True)
        raise typer.Exit(code=1)

    # 标的归一：支持名称 / 简称 / 代码 / 带前缀代码
    code = _resolve_symbol(symbol)
    typer.echo(f"标的: {code}")

    try:
        # 惰性导入，避免无谓加载成本与可选依赖缺失造成的导入期失败
        from ashare_prober.anomaly.engine import AnomalyEngine
        from ashare_prober.attribution.engine import AttributionEngine
        from ashare_prober.presentation import Render
        from ashare_prober.research.approval import ApprovalFlow
        from ashare_prober.research.judge import Judge
        from ashare_prober.research.llm_client import OpenAICompatibleClient
        from ashare_prober.research.models import ResearchReport
        from ashare_prober.research.prompts import PromptSet

        provider = build_default_provider()
        store = Store()
        # 生效配置：页面/库内覆盖 > config.toml（即改即生效，无需重启）
        from ashare_prober.data.appsettings import resolve_runtime_settings

        effective = resolve_runtime_settings(store)
        engine = AnomalyEngine(provider)

        # 1) 异动扫描
        events = engine.scan(scope, code, day)
        if not events:
            typer.echo("未检测到异动事件（返回 0 条）。")
            return
        event = events[0]

        # 2) 归因 → 事件档案
        dossier = AttributionEngine(provider, store=store).build(event)

        # 3) 研判（LLM 未配置 / 失败则降级为占位报告）
        try:
            llm = OpenAICompatibleClient(settings=effective.llm)
            report = Judge(llm, PromptSet()).generate(dossier)
        except Exception as exc:  # noqa: BLE001 - LLM 不可用时降级，不阻断闭环
            typer.echo(f"[提示] 研判跳过（{exc}）")
            report = ResearchReport(
                dossier_id=dossier.id,
                bull_view="（LLM 未配置，跳过研判）",
                bear_view="（LLM 未配置，跳过研判）",
                confidence=0.0,
                conclusion="请配置 LLM 后重新研判",
            )

        # 4) 人审申请
        approval = ApprovalFlow(store).create_request(report)

        # 5) 渲染 HTML
        Render().render_dossier(dossier, report, approval, DEFAULT_OUTPUT_DIR, event=event)

        typer.echo(f"事件数: {len(events)}")
        typer.echo(f"首个异动: {event.id}（{event.type}）")
        typer.echo(f"请求 ID: {approval.id}")
        typer.echo(f"HTML 路径: {DEFAULT_OUTPUT_DIR / (dossier.id + '.html')}")
    except Exception as exc:  # noqa: BLE001 - 顶层容错，给出清晰错误
        typer.echo(f"[错误] run 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="对研判申请进行批准/驳回，并重渲染 HTML")
def approve(
    id: str = typer.Option(..., "--id", help="人审申请 ID，如 REQ-xxxx"),
    yes: bool = typer.Option(False, "--yes", help="批准"),
    no: bool = typer.Option(False, "--no", help="驳回"),
    note: str = typer.Option("", "--note", help="审批意见"),
    approver: str = typer.Option("", "--approver", help="审批人（留空记为未署名）"),
) -> None:
    """批准 / 驳回一个研判申请，并重新渲染反映状态的 HTML。"""
    if yes == no:
        typer.echo("[错误] 必须且只能指定 --yes 或 --no 之一。", err=True)
        raise typer.Exit(code=1)
    try:
        from ashare_prober.presentation import Render
        from ashare_prober.research.approval import ApprovalFlow

        store = Store()
        flow = ApprovalFlow(store)
        request = flow.get(id)
        if request is None:
            typer.echo(f"[错误] 人审申请不存在：{id}", err=True)
            raise typer.Exit(code=1)

        decided = flow.decide(id, approve=yes, note=note, approver=approver)

        # 重新渲染（反映最新审批状态）
        report = store.get_report(decided.report_id)
        dossier = store.get_dossier(report.dossier_id) if report else None
        if dossier is not None:
            event = store.get_event(dossier.anomaly_event_id)
            Render().render_dossier(dossier, report, decided, DEFAULT_OUTPUT_DIR, event=event)
            typer.echo(f"HTML 已重渲染: {DEFAULT_OUTPUT_DIR / (dossier.id + '.html')}")

        typer.echo(f"申请 {id} → {decided.status}（审批人：{decided.approver or '未署名'}）")
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"[错误] approve 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="从本地库取事件档案并渲染 HTML")
def render(
    event: Optional[str] = typer.Option(None, "--event", help="异动事件 ID，如 EVT-xxx"),
    dossier: Optional[str] = typer.Option(None, "--dossier", help="事件档案 ID，如 DSS-xxx"),
    out: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--out", help="输出目录"),
) -> None:
    """按事件 ID 或档案 ID 从本地库取数据并渲染为自包含 HTML。"""
    if not event and not dossier:
        typer.echo("[错误] 需提供 --event 或 --dossier 之一。", err=True)
        raise typer.Exit(code=1)
    try:
        from ashare_prober.presentation import Render

        store = Store()
        dossier_obj = (
            store.get_dossier(dossier)
            if dossier
            else store.get_dossier_by_event(event)  # type: ignore[arg-type]
        )
        if dossier_obj is None:
            typer.echo("[错误] 未找到对应事件档案。", err=True)
            raise typer.Exit(code=1)

        report = store.get_report_by_dossier(dossier_obj.id)
        approval = store.get_approval_by_report(report.id) if report else None
        event_obj = store.get_event(dossier_obj.anomaly_event_id)
        Render().render_dossier(dossier_obj, report, approval, out, event=event_obj)

        typer.echo(f"HTML 路径: {out / (dossier_obj.id + '.html')}")
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"[错误] render 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="生成大盘数据看板（默认页 index.html）")
def dashboard(
    date: Optional[str] = typer.Option(
        None, "--date", help="交易日，如 2026-10-09；留空取当前日期"
    ),
    out: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--out", help="输出目录"),
    top_n: int = typer.Option(10, "--top-n", help="板块资金流 / 涨跌停展示条数"),
) -> None:
    """采集大盘总览（指数 / 涨跌家数 / 板块资金流 / 涨跌停 / 外围 / 近期异动）并渲染默认页。

    单项数据获取失败不会阻断其它项，缺口会写入看板 ``gaps`` 并在页面上显眼列出
    ——本项目纪律：**严禁以 0 或估算值冒充实测数据**。
    """
    try:
        from ashare_prober.dashboard.collector import MarketDashboardBuilder
        from ashare_prober.presentation import Render

        day: Optional[_dt.date] = None
        if date:
            try:
                day = _parse_date(date)
            except ValueError:
                typer.echo(f"[错误] 日期格式应为 YYYY-MM-DD，收到：{date}", err=True)
                raise typer.Exit(code=1)

        # 数据门面不可用时降级为「空看板 + 缺口」，不阻断命令
        provider = None
        try:
            provider = build_default_provider()
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"[提示] 数据门面不可用，看板将以空数据 + 缺口呈现：{exc}")

        dash = MarketDashboardBuilder(
            provider=provider, store=Store(), top_n=top_n
        ).build(day=day)
        Render().render_dashboard(dash, out, "index.html")

        typer.echo(f"看板日期: {dash.date or '—'}")
        typer.echo(
            f"指数 {len(dash.indices)} 个｜板块 {len(dash.sectors_top)} 流入 / "
            f"{len(dash.sectors_bottom)} 流出｜涨停 {len(dash.limit_up)} / "
            f"跌停 {len(dash.limit_down)}｜异动 {len(dash.recent_events)}"
        )
        if dash.gaps:
            typer.echo(f"[数据缺口] {len(dash.gaps)} 项（已在页面显眼列出）：")
            for gap in dash.gaps:
                typer.echo(f"  - {gap}")
        typer.echo(f"HTML 路径: {out / 'index.html'}")
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层容错
        typer.echo(f"[错误] dashboard 失败：{exc}", err=True)
        raise typer.Exit(code=1)


def _ensure_dashboard(out_dir: Path) -> Optional[Path]:
    """确保默认页 ``index.html`` 存在；不存在则现生成一次大盘看板。

    Returns:
        默认页路径；生成失败返回 ``None``（此时 ``serve`` 仍照常启动，只是没有默认页）。
    """
    index = out_dir / "index.html"
    if index.exists():
        return index
    try:
        from ashare_prober.dashboard.collector import MarketDashboardBuilder
        from ashare_prober.presentation import Render

        provider = None
        try:
            provider = build_default_provider()
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"[提示] 数据门面不可用，看板以空数据 + 缺口呈现：{exc}")

        dash = MarketDashboardBuilder(provider=provider, store=Store()).build()
        Render().render_dashboard(dash, out_dir, "index.html")
        typer.echo(f"[提示] 已生成默认页：{index}")
        return index
    except Exception as exc:  # noqa: BLE001 - 默认页生成失败不应阻断服务启动
        typer.echo(f"[提示] 默认页生成失败：{exc}")
        return None


@app.command(help="按名称/代码模糊搜索标的")
def search(
    query: str = typer.Argument(
        ..., help="股票名称、简称或代码，如 茅台 / 600519 / sh600519"
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="返回条数上限"),
) -> None:
    """模糊搜索 A 股标的；解析有歧义时按此处的候选再传 ``--symbol``。"""
    try:
        from ashare_prober.data.base import SourceError
        from ashare_prober.data.symbols import SymbolResolver

        try:
            hits = SymbolResolver().search(query, limit)
        except SourceError as exc:
            typer.echo(
                f"[错误] 标的名单不可用：{exc}（需联网拉取 A 股名单）", err=True
            )
            raise typer.Exit(code=1)

        if not hits:
            typer.echo(f"未找到匹配「{query}」的标的。")
            return
        typer.echo(f"匹配「{query}」共 {len(hits)} 条：")
        for item in hits:
            typer.echo(f"  {item.code}\t{item.name}\t{item.market}")
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层容错
        typer.echo(f"[错误] search 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="向已配置的通道推送一条消息")
def push(
    title: str = typer.Option(..., "--title", help="消息标题"),
    content: str = typer.Option("", "--content", help="消息正文（markdown/纯文本）"),
    file: Optional[Path] = typer.Option(
        None, "--file", help="从文件读取正文（--content 为空时使用）"
    ),
    level: str = typer.Option("info", "--level", help="级别：info/watch/alert"),
    url: str = typer.Option("", "--url", help="详情链接"),
) -> None:
    """推送到已配置的通道（页面设置或 ``config.toml``，页面覆盖优先）。"""
    try:
        from ashare_prober.data.appsettings import resolve_runtime_settings
        from ashare_prober.data.store import Store
        from ashare_prober.push import PushManager, PushMessage

        body = content
        if not body and file is not None:
            body = Path(file).read_text(encoding="utf-8")

        # 生效配置：页面/库内覆盖 > config.toml（与 run/scheduler 一致）
        effective = resolve_runtime_settings(Store())
        manager = PushManager(settings=effective)
        names = manager.channel_names()
        if not names:
            typer.echo(
                "[提示] 未配置任何推送通道：请在页面设置或 config.toml 的 "
                "[push] 段填写 wecom_webhook / feishu_webhook。",
                err=True,
            )
            raise typer.Exit(code=1)

        results = manager.send(
            PushMessage(title=title, content=body, level=level, url=url)
        )
        for res in results:
            typer.echo(f"  [{'成功' if res.ok else '失败'}] {res.channel}: {res.message}")
        raise typer.Exit(code=0 if PushManager.all_ok(results) else 1)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层容错
        typer.echo(f"[错误] push 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="按事件 ID 检索本地库中的相似历史案例")
def similar(
    event: str = typer.Option(..., "--event", help="异动事件 ID，如 EVT-600519-20260930-001"),
    limit: int = typer.Option(5, "--limit", help="返回条数上限"),
) -> None:
    """在本地历史事件库中检索与指定事件同类、同级别、特征相近的案例。"""
    try:
        from ashare_prober.attribution.similar import SimilarCaseFinder

        cases = SimilarCaseFinder(Store()).find_for_event_id(event, limit)
        if not cases:
            typer.echo(f"未找到与 {event} 相似的历史案例（本地库暂无足够事件）。")
            return
        typer.echo(f"与 {event} 相似的历史案例（Top {len(cases)}）：")
        for c in cases:
            line = f"  {c.date}  {c.symbol}  {c.pattern}  相似度 {c.similarity:.2f}"
            if c.reason:
                line += f"  {c.reason}"
            typer.echo(line)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层容错
        typer.echo(f"[错误] similar 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="历史日线回测复核异动规则（T+1/T+3/T+5/T+10 前向收益）")
def backtest(
    symbol: str = typer.Option(..., "--symbol", help="标的，支持名称 / 代码 / 带前缀代码"),
    start: str = typer.Option("", "--start", help="起始日 YYYY-MM-DD；缺省为终点前推约 240 自然日"),
    end: str = typer.Option("", "--end", help="结束日 YYYY-MM-DD；缺省为今天"),
    out: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--out", help="报告输出目录"),
) -> None:
    """用真实历史日线回放线上同一套异动规则，统计信号前向收益并对照无信号日基线。

    需要「盘中板块数据」的规则无法用日线回放，将如实列入 skipped_rules——严禁假造。
    """
    try:
        from ashare_prober.backtest.engine import run_backtest
        from ashare_prober.backtest.report import write_report

        code = _resolve_symbol(symbol)
        end_day = _parse_date(end) if end else _dt.date.today()
        start_day = _parse_date(start) if start else end_day - _dt.timedelta(days=240)

        result = run_backtest(code, start=start_day, end=end_day)
        path = write_report(result, out)

        typer.echo(
            f"标的: {code}｜区间: {result.start} ~ {result.end}"
            f"｜回放日: {result.n_days}"
        )
        typer.echo(
            f"信号: {result.n_signals} 条｜censored: {result.n_censored}"
            f"｜无信号日基线胜率: {result.baseline_hit_rate:.1%}"
        )
        for rule, n in (result.per_rule or {}).items():
            typer.echo(f"  规则 {rule}: 触发 {n} 次")
        for hz, st in (result.per_horizon or {}).items():
            typer.echo(
                f"  {hz}: n={st.n}｜胜率 {st.hit_rate:.1%}｜均值 {st.avg:+.2%}"
                f"｜中位 {st.median:+.2%}"
            )
        for skipped in result.skipped_rules or []:
            typer.echo(f"  [未纳入回测] {skipped}")
        for gap in result.gaps or []:
            typer.echo(f"  [缺口] {gap}")
        typer.echo(f"HTML 报告: {path}")
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层容错
        typer.echo(f"[错误] backtest 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="定时调度：收盘后自动扫描 watchlist → 推送 → 刷新看板")
def schedule(
    once: bool = typer.Option(
        False, "--once", help="只同步执行一轮作业后退出（不启动定时器）"
    ),
    symbols: str = typer.Option(
        "", "--symbols", help="逗号分隔标的（支持名称/代码）；缺省读 config.toml [watchlist]"
    ),
) -> None:
    """定时（或立即）执行「扫描 watchlist → 推送异动 → 刷新大盘看板」作业。"""
    try:
        from ashare_prober.scheduler.service import SchedulerService

        if symbols.strip():
            sym_list = [_resolve_symbol(s) for s in symbols.split(",") if s.strip()]
        else:
            from ashare_prober.config import get_settings

            settings = get_settings()
            sym_list = [
                _resolve_symbol(s)
                for s in (getattr(settings.watchlist, "symbols", None) or [])
            ]
        if not sym_list:
            typer.echo(
                "[错误] 未指定标的：请用 --symbols 或在 config.toml 设置 "
                "[watchlist].symbols。",
                err=True,
            )
            raise typer.Exit(code=1)

        provider = None
        try:
            provider = build_default_provider()
        except Exception as exc:  # noqa: BLE001 - 逐标的失败已在作业内兜底
            typer.echo(f"[提示] 数据门面不可用，各标的扫描将失败并汇总：{exc}")

        service = SchedulerService(provider=provider, store=Store(), symbols=sym_list)
        if once:
            summary = service.run_once()
            typer.echo(
                f"事件: {summary.get('events', 0)}｜推送: {summary.get('pushed', 0)}"
                f"｜看板刷新: {summary.get('dashboard')}"
            )
            failures = summary.get("failures") or []
            for sym, msg in failures:
                typer.echo(f"  [失败] {sym}: {msg}")
            raise typer.Exit(code=0 if not failures else 1)

        typer.echo(
            f"定时调度已启动（周一至五按配置时刻自动扫描 {len(sym_list)} 个标的，"
            "Ctrl+C 退出）"
        )
        service.start(blocking=True)
    except typer.Exit:
        raise
    except KeyboardInterrupt:  # pragma: no cover - 交互退出
        typer.echo("已停止定时调度。")
    except Exception as exc:  # noqa: BLE001 - 顶层容错
        typer.echo(f"[错误] schedule 失败：{exc}", err=True)
        raise typer.Exit(code=1)


def _resolve_symbol_info(query: str) -> tuple[str, str]:
    """解析标的，返回 ``(code, name)``。

    6 位代码形态直接放行（离线可用，name 为空）；名称/简称走标的表（需联网一次）。
    """
    if _looks_like_code(query):
        return _strip_market(query), ""
    from ashare_prober.data.symbols import SymbolResolver

    info = SymbolResolver().resolve(query)
    return info.code, info.name


@app.command(help="监控名单管理：查看/添加/删除/暂停/恢复（持久化于本地库）")
def watchlist(
    add: str = typer.Option("", "--add", help="添加标的（支持名称/简称/代码）"),
    remove: str = typer.Option("", "--remove", help="移除标的（代码）"),
    pause: str = typer.Option("", "--pause", help="暂停监控（保留在名单，不再扫描）"),
    resume: str = typer.Option("", "--resume", help="恢复监控"),
    active_only: bool = typer.Option(
        False, "--active-only", help="列表只显示监控中的标的"
    ),
) -> None:
    """管理长期监控名单。页面方式：``prober serve`` 后在看板「监控列表」卡片增删。

    名单真源是本地库 ``user_watchlist`` 表；config.toml ``[watchlist]`` 仅在库为空时兜底。
    """
    try:
        store = Store()
        touched = False
        if add.strip():
            code, name = _resolve_symbol_info(add)
            created = store.add_watchlist(code, name=name)
            typer.echo(f"{'已添加' if created else '已更新'}：{code} {name or ''}")
            touched = True
        if remove.strip():
            code = _strip_market(remove)
            typer.echo(
                f"{'已移除' if store.remove_watchlist(code) else '名单中不存在'}：{code}"
            )
            touched = True
        if pause.strip():
            code = _strip_market(pause)
            ok = store.set_watchlist_active(code, False)
            typer.echo(f"{'已暂停' if ok else '名单中不存在'}：{code}")
            touched = True
        if resume.strip():
            code = _strip_market(resume)
            ok = store.set_watchlist_active(code, True)
            typer.echo(f"{'已恢复' if ok else '名单中不存在'}：{code}")
            touched = True

        items = store.list_watchlist(active_only=active_only)
        scope = "监控中" if active_only else "全部"
        typer.echo(f"\n监控名单（{scope}，共 {len(items)} 只）：")
        if not items:
            typer.echo("  （空——用 --add 或在看板页添加第一只票）")
        for it in items:
            status = "监控中" if it.get("active") else "已暂停"
            typer.echo(f"  {it['code']}  {it.get('name') or '—'}  [{status}]")
        if not touched:
            raise typer.Exit(code=0)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层容错
        typer.echo(f"[错误] watchlist 失败：{exc}", err=True)
        raise typer.Exit(code=1)


@app.command(help="本地起 http 服务，浏览 outputs/ 下的 HTML")
def serve(
    port: int = typer.Option(8000, "--port", help="监听端口"),
) -> None:
    """启动本地静态服务，指向 outputs/ 目录；默认页为大盘看板 index.html。"""
    from ashare_prober.presentation import start_viewer

    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 默认页：不存在看板就现生成一个（单项缺失只降级、不阻断）
    _ensure_dashboard(DEFAULT_OUTPUT_DIR)
    typer.echo(
        f"本地查看服务已启动：http://127.0.0.1:{port}/  （默认页＝大盘看板，Ctrl+C 退出）"
    )
    try:
        start_viewer(DEFAULT_OUTPUT_DIR, port)
    except KeyboardInterrupt:  # pragma: no cover - 交互退出
        typer.echo("已停止服务。")


def main() -> None:
    """供 ``python -m ashare_prober.cli`` 调用的入口。"""
    app()


if __name__ == "__main__":
    main()
