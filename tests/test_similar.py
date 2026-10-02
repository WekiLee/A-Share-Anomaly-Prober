"""T16 相似案例检索测试（全部离线，tmp_path 建真实 Store 灌合成事件）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from ashare_prober.anomaly.models import AnomalyEvent
from ashare_prober.attribution.models import SimilarCase
from ashare_prober.attribution.similar import (
    SimilarCaseFinder,
    event_features,
    similarity,
)
from ashare_prober.data.store import Store


def make_event(
    event_id: str,
    *,
    ref_code: str = "600000",
    ref_name: str = "浦发银行",
    type: str = "volume_break_up",
    level: str = "alert",
    detected_at: datetime | None = None,
    scope: str = "stock",
) -> AnomalyEvent:
    """构造合成异动事件（字段全部来自 AnomalyEvent 真实字段）。"""
    return AnomalyEvent(
        id=event_id,
        scope=scope,
        ref_code=ref_code,
        ref_name=ref_name,
        type=type,
        level=level,
        detected_at=detected_at or datetime(2024, 6, 1, 10, 0, 0),
    )


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(db_path=tmp_path / "prober.db")


# --------------------------------------------------------------------- #
# event_features
# --------------------------------------------------------------------- #
class TestEventFeatures:
    def test_only_real_fields(self) -> None:
        """特征 key 必须来自 AnomalyEvent 真实字段，唯一例外是派生的 direction。"""
        event = make_event("EVT-1")
        features = event_features(event)
        assert features, "正常事件不应得到空特征"
        for key in features:
            assert key == "direction" or key in AnomalyEvent.model_fields

    def test_direction_derived_from_type_suffix(self) -> None:
        up = event_features(make_event("EVT-1", type="volume_break_up"))
        down = event_features(make_event("EVT-2", type="volume_break_down"))
        plain = event_features(make_event("EVT-3", type="sector_rank_jump"))
        assert up["direction"] == "up"
        assert down["direction"] == "down"
        assert "direction" not in plain  # 无法派生时省略，绝不填假值

    def test_missing_type_raises(self) -> None:
        """type 为空 = 字段残缺，应显式失败（由 find 捕获跳过）。"""
        event = make_event("EVT-BAD", type="")
        with pytest.raises(ValueError):
            event_features(event)

    def test_empty_ref_code_dimension_omitted(self) -> None:
        features = event_features(make_event("EVT-1", ref_code=""))
        assert "ref_code" not in features


# --------------------------------------------------------------------- #
# similarity
# --------------------------------------------------------------------- #
class TestSimilarity:
    def test_identical_features_score_one(self) -> None:
        """设计约定：完全一致的特征字典得满分 1.0（按共有维度归一化）。"""
        features = event_features(make_event("EVT-1"))
        assert similarity(features, dict(features)) == pytest.approx(1.0)

    def test_same_type_beats_different_type(self) -> None:
        base = event_features(
            make_event("EVT-1", type="volume_break_up", level="alert")
        )
        same_type = event_features(
            make_event("EVT-2", type="volume_break_up", level="watch")
        )
        diff_type = event_features(
            make_event("EVT-3", type="sector_flow_flip", level="alert")
        )
        assert similarity(base, same_type) > similarity(base, diff_type)

    def test_numeric_dimensions_closer_scores_higher(self, monkeypatch) -> None:
        """数值维度按归一化距离打分：越接近分越高（扩展性分支）。

        当前 AnomalyEvent 无数值维度，故临时给 WEIGHTS 增加一个数值维度验证
        归一化距离逻辑本身（不改生产权重表）。
        """
        from ashare_prober.attribution import similar as similar_module

        monkeypatch.setattr(
            similar_module, "WEIGHTS", {**similar_module.WEIGHTS, "amount": 1.0}
        )
        a = {"amount": 100.0}
        near = {"amount": 110.0}
        far = {"amount": 300.0}
        assert similarity(a, near) > similarity(a, far)
        assert similarity({"amount": 5.0}, {"amount": 5.0}) == pytest.approx(1.0)

    def test_no_common_dimensions_scores_zero(self) -> None:
        assert similarity({"type": "a"}, {"level": "alert"}) == 0.0

    def test_range_and_deterministic(self) -> None:
        a = event_features(make_event("EVT-1", type="volume_break_up"))
        b = event_features(make_event("EVT-2", type="sector_flow_flip"))
        s1 = similarity(a, b)
        assert 0.0 <= s1 <= 1.0
        assert s1 == similarity(a, b)  # 确定性：重复调用结果一致


# --------------------------------------------------------------------- #
# SimilarCaseFinder.find
# --------------------------------------------------------------------- #
class TestFind:
    def test_ranked_desc_and_excludes_self(self, store: Store) -> None:
        query = make_event("EVT-Q", detected_at=datetime(2024, 6, 10, 10, 0, 0))
        # 与 query 完全一致（同票/同类型/同级别）→ 最高分
        twin = make_event("EVT-TWIN", detected_at=datetime(2024, 6, 5, 10, 0, 0))
        # 同类型但不同票、不同级别 → 中等分
        mid = make_event(
            "EVT-MID",
            ref_code="000001",
            ref_name="平安银行",
            level="watch",
            detected_at=datetime(2024, 6, 4, 10, 0, 0),
        )
        # 类型、级别、票全不同 → 低分
        low = make_event(
            "EVT-LOW",
            ref_code="300750",
            ref_name="宁德时代",
            type="sector_flow_flip",
            level="info",
            detected_at=datetime(2024, 6, 3, 10, 0, 0),
        )
        for ev in (query, twin, mid, low):
            store.save_event(ev)

        cases = SimilarCaseFinder(store).find(query, limit=5)

        assert [c.pattern for c in cases], "应至少返回一条相似案例"
        ids = {c.outcome.split("；")[-1].split(" ")[0] for c in cases}
        assert "EVT-Q" not in ids, "必须排除事件自身"
        assert len(cases) == 3
        # 排序：完全一致 > 同类型异票 > 全不同
        assert "EVT-TWIN" in cases[0].outcome
        assert "EVT-MID" in cases[1].outcome
        assert "EVT-LOW" in cases[2].outcome

    def test_empty_store_returns_empty_list(self, store: Store) -> None:
        query = make_event("EVT-Q")
        assert SimilarCaseFinder(store).find(query) == []  # 诚实空集，不抛异常

    def test_only_self_in_store_returns_empty_list(self, store: Store) -> None:
        query = make_event("EVT-Q")
        store.save_event(query)
        assert SimilarCaseFinder(store).find(query) == []

    def test_limit_effective(self, store: Store) -> None:
        query = make_event("EVT-Q", detected_at=datetime(2024, 6, 10, 10, 0, 0))
        for i in range(5):
            store.save_event(
                make_event(f"EVT-{i}", detected_at=datetime(2024, 6, 1 + i, 10, 0, 0))
            )
        cases = SimilarCaseFinder(store).find(query, limit=2)
        assert len(cases) == 2

    def test_tie_break_prefers_newer_event(self, store: Store) -> None:
        """同分时 detected_at 更近者优先（库本身按 detected_at 倒序返回）。"""
        query = make_event("EVT-Q", detected_at=datetime(2024, 6, 10, 10, 0, 0))
        older = make_event("EVT-OLD", detected_at=datetime(2024, 6, 1, 10, 0, 0))
        newer = make_event("EVT-NEW", detected_at=datetime(2024, 6, 9, 10, 0, 0))
        for ev in (older, newer, query):
            store.save_event(ev)

        cases = SimilarCaseFinder(store).find(query, limit=5)
        assert len(cases) == 2
        assert "EVT-NEW" in cases[0].outcome
        assert "EVT-OLD" in cases[1].outcome

    def test_broken_candidate_skipped_not_crash(self, store: Store) -> None:
        """候选池中混入字段残缺（type 为空）的事件 → 跳过且不崩。"""
        query = make_event("EVT-Q", detected_at=datetime(2024, 6, 10, 10, 0, 0))
        broken = make_event("EVT-BROKEN", type="", detected_at=datetime(2024, 6, 2, 10, 0, 0))
        good = make_event("EVT-GOOD", detected_at=datetime(2024, 6, 3, 10, 0, 0))
        for ev in (broken, good, query):
            store.save_event(ev)

        cases = SimilarCaseFinder(store).find(query, limit=5)
        assert len(cases) == 1
        assert "EVT-GOOD" in cases[0].outcome

    def test_query_event_with_broken_features_returns_empty(self, store: Store) -> None:
        """目标事件自身特征残缺 → 无参照系，返回空集且不抛。"""
        query = make_event("EVT-Q", type="")
        store.save_event(make_event("EVT-GOOD"))
        assert SimilarCaseFinder(store).find(query) == []

    def test_candidate_limit_respected(self, store: Store) -> None:
        """candidate_limit 限制候选池大小：池外事件不参与检索。"""
        query = make_event("EVT-Q", detected_at=datetime(2024, 6, 20, 10, 0, 0))
        # 3 条历史事件 + query 自身；candidate_limit=2 时只取最近 2 条做候选
        for i, day in enumerate((5, 6, 7), start=1):
            store.save_event(
                make_event(f"EVT-{i}", detected_at=datetime(2024, 6, day, 10, 0, 0))
            )
        store.save_event(query)

        finder = SimilarCaseFinder(store, candidate_limit=2)
        # 库按 detected_at 倒序取 2 条候选：EVT-Q（自身，排除）+ EVT-3
        cases = finder.find(query, limit=5)
        assert len(cases) == 1
        assert "EVT-3" in cases[0].outcome


# --------------------------------------------------------------------- #
# find_for_event_id
# --------------------------------------------------------------------- #
class TestFindForEventId:
    def test_missing_event_returns_empty(self, store: Store) -> None:
        assert SimilarCaseFinder(store).find_for_event_id("EVT-NOPE") == []

    def test_existing_event_delegates_to_find(self, store: Store) -> None:
        target = make_event("EVT-Q", detected_at=datetime(2024, 6, 10, 10, 0, 0))
        ref = make_event("EVT-REF", detected_at=datetime(2024, 6, 2, 10, 0, 0))
        store.save_event(target)
        store.save_event(ref)

        cases = SimilarCaseFinder(store).find_for_event_id("EVT-Q", limit=3)
        assert len(cases) == 1
        assert "EVT-REF" in cases[0].outcome


# --------------------------------------------------------------------- #
# SimilarCase 复用与字段映射
# --------------------------------------------------------------------- #
class TestSimilarCaseMapping:
    def test_reuses_attribution_models_similar_case(self, store: Store) -> None:
        """输出必须复用 attribution.models.SimilarCase（来源断言）。"""
        query = make_event("EVT-Q", detected_at=datetime(2024, 6, 10, 10, 0, 0))
        ref = make_event("EVT-REF", detected_at=datetime(2024, 6, 2, 9, 30, 0))
        store.save_event(query)
        store.save_event(ref)

        cases = SimilarCaseFinder(store).find(query)
        assert len(cases) == 1
        case = cases[0]
        assert isinstance(case, SimilarCase)
        assert type(case).__module__ == "ashare_prober.attribution.models"

        # 字段映射约定：date/symbol/pattern/outcome + 结构化 similarity/reason
        assert case.date == datetime(2024, 6, 2).date()
        assert case.symbol == "600000"  # 参照事件代码
        assert case.pattern == "volume_break_up(alert)"  # 形态 = type(level)
        assert "相似度" in case.outcome
        assert "EVT-REF" in case.outcome  # 参照事件 id
        assert "浦发银行" in case.outcome  # 参照事件名称
        assert "同类型" in case.outcome  # reason：相似在哪

        # 结构化字段：query 与 ref 特征完全一致 → 相似度 1.0
        assert case.similarity == pytest.approx(1.0)
        assert 0.0 <= case.similarity <= 1.0
        assert "同类型" in case.reason  # reason 只含真实匹配维度
        assert "同标的" in case.reason
        assert "同级别" in case.reason

    def test_structured_similarity_varies_with_candidates(self, store: Store) -> None:
        """结构化 similarity 与候选实际匹配程度一致（非恒定值）。"""
        query = make_event("EVT-Q", detected_at=datetime(2024, 6, 10, 10, 0, 0))
        twin = make_event("EVT-TWIN", detected_at=datetime(2024, 6, 5, 10, 0, 0))
        mid = make_event(
            "EVT-MID",
            ref_code="000001",
            ref_name="平安银行",
            level="watch",
            detected_at=datetime(2024, 6, 4, 10, 0, 0),
        )
        for ev in (query, twin, mid):
            store.save_event(ev)

        cases = SimilarCaseFinder(store).find(query, limit=5)
        by_id = {c.outcome.split("；")[-1].split(" ")[0]: c for c in cases}
        assert by_id["EVT-TWIN"].similarity > by_id["EVT-MID"].similarity
        # MID 与 query 不同票不同级别 → reason 只剩真实匹配的维度
        assert "同类型" in by_id["EVT-MID"].reason
        assert "同标的" not in by_id["EVT-MID"].reason


class TestSimilarCaseBackwardCompat:
    def test_old_payload_without_new_fields_loads_with_defaults(self) -> None:
        """向后兼容护栏：只含旧 4 字段的 dossier JSON 必须照常加载，
        新字段取默认值（防止未来有人去掉默认值破坏旧数据）。"""
        legacy = {
            "date": "2024-06-02",
            "symbol": "600000",
            "pattern": "volume_break_up(alert)",
            "outcome": "相似度0.87；同类型 volume_break_up；EVT-X 浦发银行",
        }
        case = SimilarCase.model_validate(legacy)
        assert case.symbol == "600000"
        assert case.pattern == "volume_break_up(alert)"
        assert case.similarity == 0.0  # 新字段默认值
        assert case.reason == ""  # 新字段默认值

    def test_round_trip_preserves_new_fields(self) -> None:
        """新字段序列化/反序列化往返无损。"""
        case = SimilarCase(
            symbol="600000",
            pattern="volume_break_up(alert)",
            outcome="相似度1.00；同类型 volume_break_up",
            similarity=1.0,
            reason="同类型 volume_break_up",
        )
        restored = SimilarCase.model_validate_json(case.model_dump_json())
        assert restored.similarity == pytest.approx(1.0)
        assert restored.reason == "同类型 volume_break_up"
