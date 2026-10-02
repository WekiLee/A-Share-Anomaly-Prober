"""T16 相似案例检索（历史事件库内的「同类、同级别、特征相近」参照）。

设计要点：
- 特征只取 ``AnomalyEvent`` 上**真实存在**的字段（type / level / scope / ref_code），
  外加一个从 type 后缀派生的 ``direction``（``_up`` / ``_down``）。AnomalyEvent
  目前不携带数值字段，故不做数值维度（函数实现保留数值分支以备扩展）。
- 相似度为确定性加权打分：离散维度精确匹配、数值维度归一化距离，
  按双方**共有维度**的权重归一化，输出 0.0~1.0，无随机。
- 诚实性纪律：库里没有历史事件（或除自身外无候选）→ 返回空列表；
  单个候选特征提取失败（如 type 为空等字段残缺）→ 跳过该候选，不抛异常。
- 输出复用 ``attribution.models.SimilarCase``（严禁重复定义）。字段映射约定：
  - ``date``       := 参照事件的 detected_at 日期；
  - ``symbol``     := 参照事件 ref_code（代码）；
  - ``pattern``    := 参照事件形态 ``<type>(<level>)``；
  - ``outcome``    := 人类可读文本（相似度 + reason + 参照事件 id 与名称）；
  - ``similarity`` := 相似度得分（0.0~1.0，结构化字段）；
  - ``reason``     := 相似原因说明（只含真实匹配的维度，结构化字段）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.attribution.models import SimilarCase

# 各特征维度权重（离散精确匹配 / 数值归一化距离共用同一权重表）。
# 合计为 1.0；实际得分按双方共有维度归一化，故缺失维度不拉低分数。
WEIGHTS: dict[str, float] = {
    "type": 0.35,  # 异动类型（最核心的可比维度）
    "ref_code": 0.20,  # 同标的（同票历史最有参照价值）
    "level": 0.20,  # 告警级别（同级别 = 同幅度量级）
    "direction": 0.15,  # 方向（type 后缀 _up / _down 派生）
    "scope": 0.10,  # 作用域（stock / sector / index / limit_pool）
}


def event_features(event: AnomalyEvent) -> dict[str, Any]:
    """从异动事件的真实字段提取可比特征字典。

    只使用 ``AnomalyEvent`` 上实际存在的字段：
    - ``type``：异动类型（核心维度，为空视为字段残缺，抛 ``ValueError``）；
    - ``level`` / ``scope`` / ``ref_code``：直接取值（ref_code 为空则省略该维度）；
    - ``direction``：从 type 后缀派生（``*_up`` → ``up``、``*_down`` → ``down``），
      无法派生时省略。

    Raises:
        ValueError: ``event.type`` 为空（特征残缺，无法比较）。
    """
    etype = (event.type or "").strip()
    if not etype:
        raise ValueError(f"event {event.id!r} 缺少 type 字段，无法提取可比特征")

    features: dict[str, Any] = {"type": etype}
    if event.level:
        features["level"] = event.level
    if event.scope:
        features["scope"] = event.scope
    if event.ref_code:
        features["ref_code"] = event.ref_code
    direction = _derive_direction(etype)
    if direction is not None:
        features["direction"] = direction
    return features


def _derive_direction(etype: str) -> str | None:
    """从异动类型后缀派生方向；无法派生返回 ``None``。"""
    if etype.endswith("_up"):
        return "up"
    if etype.endswith("_down"):
        return "down"
    return None


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """计算两组特征的相似度，返回 0.0~1.0（确定性、无随机）。

    规则：
    - 只对双方**共有**且在 ``WEIGHTS`` 中的维度打分，并按其权重归一化，
      因此完全一致的特征字典得 1.0，无任何共有维度得 0.0；
    - 离散维度（str）精确匹配：相等记 1.0，否则 0.0；
    - 数值维度（int/float，不含 bool）按归一化距离打分：
      ``1 - min(1, |a-b| / scale)``，``scale = max(|a|, |b|)``（均为 0 时记满分）。
    """
    matched_weight = 0.0
    accumulated = 0.0
    for dim, weight in WEIGHTS.items():
        if dim not in a or dim not in b:
            continue
        va, vb = a[dim], b[dim]
        if isinstance(va, bool) or isinstance(vb, bool):
            continue  # bool 不参与打分，避免与数值维度混淆
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            scale = max(abs(va), abs(vb))
            if scale == 0:
                dim_score = 1.0
            else:
                dim_score = 1.0 - min(1.0, abs(va - vb) / scale)
        elif isinstance(va, str) and isinstance(vb, str):
            dim_score = 1.0 if va == vb else 0.0
        else:
            continue  # 类型不一致的维度跳过，不计入权重
        matched_weight += weight
        accumulated += weight * dim_score
    if matched_weight <= 0.0:
        return 0.0
    return max(0.0, min(1.0, accumulated / matched_weight))


class SimilarCaseFinder:
    """在本地历史事件库中检索与目标事件相似的历史案例。"""

    def __init__(self, store: Any, candidate_limit: int = 200) -> None:
        """初始化检索器。

        Args:
            store: 本地库（需提供 ``list_events`` / ``get_event``）。
            candidate_limit: 每次检索从库里取多少条历史事件做候选池。
        """
        self.store = store
        self.candidate_limit = int(candidate_limit)

    def find(self, event: AnomalyEvent, limit: int = 5) -> list[SimilarCase]:
        """检索与 ``event`` 最相似的历史案例（最多 ``limit`` 条）。

        - 候选 = ``store.list_events(limit=candidate_limit)``，排除事件自身（同 id）；
        - 候选为空（或除自身外为空）→ 返回 ``[]``（业务性空集，诚实不编造）；
        - 单候选特征提取失败 → 跳过该候选，不影响整体；
        - 排序：相似度降序；同分时保持库返回序（detected_at 更近优先）。

        Note:
            仅当候选池查询本身失败（存储层异常）时才向上抛出；
            业务性空集一律返回 ``[]``。
        """
        try:
            query_features = event_features(event)
        except ValueError:
            return []  # 目标事件自身特征残缺，无参照系可言
        candidates = self.store.list_events(limit=self.candidate_limit)

        scored: list[tuple[float, AnomalyEvent]] = []
        for candidate in candidates:
            if candidate.id == event.id:
                continue  # 排除自身
            try:
                candidate_features = event_features(candidate)
            except (ValueError, AttributeError):
                continue  # 字段残缺的候选直接跳过
            score = similarity(query_features, candidate_features)
            scored.append((score, candidate))

        # list_events 按 detected_at 倒序返回，稳定排序保证同分时更近者在前。
        scored.sort(key=lambda pair: -pair[0])

        results: list[SimilarCase] = []
        for score, candidate in scored[: max(0, int(limit))]:
            results.append(self._build_case(query_features, candidate, score))
        return results

    def find_for_event_id(self, event_id: str, limit: int = 5) -> list[SimilarCase]:
        """按事件 ID 检索相似案例的便捷方法；事件不存在返回 ``[]``。"""
        event = self.store.get_event(event_id)
        if event is None:
            return []
        return self.find(event, limit=limit)

    # ------------------------------------------------------------------ #
    # 内部：组装 SimilarCase（复用 attribution.models 的模型）
    # ------------------------------------------------------------------ #
    def _build_case(
        self, query_features: dict[str, Any], candidate: AnomalyEvent, score: float
    ) -> SimilarCase:
        """将参照事件与相似度组装为 ``SimilarCase``。

        字段映射（SimilarCase 仅有 date/symbol/pattern/outcome 四个字段）：
        - ``date``    := 参照事件 detected_at 的日期；
        - ``symbol``  := 参照事件 ref_code；
        - ``pattern`` := ``<type>(<level>)``；
        - ``outcome`` := ``相似度<x.xx>；<reason>；<id> <ref_name>``。
        """
        detected_at = candidate.detected_at
        case_date = detected_at.date() if isinstance(detected_at, datetime) else None

        reason_parts = self._reason_parts(query_features, candidate)
        reason = "；".join(reason_parts)
        ref_name = candidate.ref_name or "（无名称）"
        outcome = (
            f"相似度{score:.2f}；{reason if reason else '特征部分相近'}"
            f"；{candidate.id} {ref_name}"
        )
        return SimilarCase(
            date=case_date,
            symbol=candidate.ref_code,
            pattern=f"{candidate.type}({candidate.level})",
            outcome=outcome,
            similarity=score,
            reason=reason,
        )

    def _reason_parts(
        self, query_features: dict[str, Any], candidate: AnomalyEvent
    ) -> list[str]:
        """生成一句 reason：说明参照事件与目标事件相似在哪里。

        仅在维度确实匹配时才写（诚实性：不凑数）。

        Args:
            query_features: 目标事件的已完成特征提取（调用方保证有效）。
            candidate: 参照事件。
        """
        try:
            candidate_features = event_features(candidate)
        except (ValueError, AttributeError):
            return []

        parts: list[str] = []
        if (
            "type" in query_features
            and query_features.get("type") == candidate_features.get("type")
        ):
            parts.append(f"同类型 {candidate.type}")
        if (
            "ref_code" in query_features
            and query_features.get("ref_code") == candidate_features.get("ref_code")
        ):
            parts.append(f"同标的 {candidate.ref_code}")
        if (
            "level" in query_features
            and query_features.get("level") == candidate_features.get("level")
        ):
            parts.append(f"同级别 {candidate.level}")
        if (
            "direction" in query_features
            and query_features.get("direction") == candidate_features.get("direction")
        ):
            parts.append(f"同方向 {candidate_features['direction']}")
        if (
            "scope" in query_features
            and query_features.get("scope") == candidate_features.get("scope")
        ):
            parts.append(f"同作用域 {candidate.scope}")
        return parts
