import pytest
from eval.metrics import (
    compute_cell_metrics,
    is_acceptable_title,
    is_strict_whitelist_hit,
    classify_usurpation,
    is_usurped_title,
    EvaluationRecord,
)
from eval.report import format_per_turn_table, format_aggregate_summary


def test_is_acceptable_title_exact_normalized_match():
    # 严格杜绝双向子串误判：全词等值匹配
    assert is_acceptable_title("cat", ["catalog"], "durable subject") is False
    assert is_acceptable_title("API", ["API rate limiter"], "durable subject") is False
    assert is_acceptable_title("B", None, "Title B") is False

    # 规范化后全词等值匹配
    assert is_strict_whitelist_hit("Title B", None, "Title B") is True
    assert is_acceptable_title("Title B", None, "Title B") is True
    assert is_acceptable_title("  title b  ", ["Title B"], "durable subject") is True
    assert is_acceptable_title("AutoTitler 架构重构", ["AutoTitler 架构重构"], "AutoTitler 架构") is True


def test_is_usurped_title_mechanical_evaluation():
    # 1. 命中次主题/排错/噪声标签：具备独立正向证据，判定为篡权
    assert (
        is_usurped_title(
            title="修复局部报错",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
            secondary_topics=["修复局部报错", "构建脚本调试"],
        )
        is True
    )
    assert (
        classify_usurpation(
            title="修复局部报错",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
            secondary_topics=["修复局部报错", "构建脚本调试"],
        )
        == "usurped"
    )

    # 2. 禁止转向时脱离短词表，但无篡权证据且缺失标识符：判定为 undetermined，绝不误判为 100% 严重篡权
    assert (
        is_usurped_title(
            title="无关临时分支",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
        )
        is False
    )
    assert (
        classify_usurpation(
            title="无关临时分支",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
        )
        == "undetermined"
    )

    # 3. 保留必要标识符但脱离短白名单：仅能判定未定，实体正确不保证目标正确
    assert (
        is_usurped_title(
            title="AutoTitler 提示词调优",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
            secondary_topics=["修复局部报错"],
            required_identifiers=["AutoTitler"],
        )
        is False
    )
    assert (
        classify_usurpation(
            title="AutoTitler 提示词调优",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
            secondary_topics=["修复局部报错"],
            required_identifiers=["AutoTitler"],
        )
        == "undetermined"
    )

    # 4. 正常主线或允许转向时不属于篡权
    assert (
        is_usurped_title(
            title="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
        )
        is False
    )
    assert (
        is_usurped_title(
            title="新主线主题",
            acceptable_titles=["新主线主题"],
            durable_subject="旧主线主题",
            allowed_shift=True,
        )
        is False
    )


def test_identifier_or_allowed_shift_alone_does_not_prove_mainline_quality():
    assert classify_usurpation(
        title="AutoTitler 断网排查",
        acceptable_titles=["AutoTitler 会话标题治理"],
        durable_subject="AutoTitler 会话标题治理",
        allowed_shift=False,
        secondary_topics=["断网排查"],
        required_identifiers=["AutoTitler"],
    ) == "undetermined"
    assert classify_usurpation(
        title="完全无关的话题",
        acceptable_titles=["原主线"],
        durable_subject="原主线",
        allowed_shift=True,
    ) == "undetermined"


def test_protected_session_requires_initial_title_and_detects_drift():
    # 缺少 initial_title 抛出 ValueError
    with pytest.raises(ValueError, match="initial_title must be explicitly provided"):
        EvaluationRecord(
            session_id="sess_prot_err",
            cell_id="cell_a",
            turn=1,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="Title",
            durable_subject="Subject",
            initial_title=None,
            is_manual_protected=True,
            status="ok",
        )

    # 动作虽为 keep 但 applied_title 与 initial_title 发生漂移
    rec_drift = [
        EvaluationRecord(
            session_id="sess_prot_2",
            cell_id="cell_a",
            turn=1,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="Drifted Title",
            durable_subject="Protected Subject",
            initial_title="Original Protected Title",
            is_manual_protected=True,
            status="ok",
        )
    ]
    metrics_drift = compute_cell_metrics("cell_a", rec_drift)
    assert metrics_drift["protected_violation_count"] == 1
    assert metrics_drift["hard_fail"] is True


def test_rename_counts_and_pending_distinction():
    records = [
        EvaluationRecord(
            session_id="sess_1",
            cell_id="cell_a",
            turn=1,
            action="rename",
            candidate="Title Turn 1",
            pending=True,
            applied_title="Initial Title",
            durable_subject="Subject 1",
            allowed_shift=False,
            status="ok",
        ),
        EvaluationRecord(
            session_id="sess_1",
            cell_id="cell_a",
            turn=2,
            action="approve",
            candidate="Title Turn 1",
            pending=False,
            applied_title="Title Turn 1",
            durable_subject="Subject 1",
            allowed_shift=False,
            status="ok",
        ),
        EvaluationRecord(
            session_id="sess_1",
            cell_id="cell_a",
            turn=3,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="Title Turn 1",
            durable_subject="Subject 1",
            allowed_shift=False,
            status="ok",
        ),
    ]

    metrics = compute_cell_metrics("cell_a", records)
    assert metrics["applied_renames"] == 1
    assert metrics["pending_renames"] == 1
    assert metrics["eligible_turns"] == 3
    assert metrics["observed_turns"] == 3
    assert metrics["failed_turns"] == 0


def test_errors_excluded_from_quality_denominator_and_token_availability():
    records = [
        EvaluationRecord(
            session_id="sess_1",
            cell_id="cell_a",
            turn=1,
            action="error",
            candidate=None,
            pending=False,
            applied_title="Initial",
            durable_subject="Subj",
            allowed_shift=False,
            status="rate_limit",
            input_tokens=None,
        ),
        EvaluationRecord(
            session_id="sess_1",
            cell_id="cell_a",
            turn=2,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="Initial",
            durable_subject="Subj",
            allowed_shift=False,
            status="ok",
            input_tokens=150,
            output_tokens=15,
        ),
    ]

    metrics = compute_cell_metrics("cell_a", records)
    assert metrics["eligible_turns"] == 2
    assert metrics["observed_turns"] == 1
    assert metrics["failed_turns"] == 1
    assert metrics["error_breakdown"]["rate_limit"] == 1
    assert metrics["tokens"]["average_input_tokens"] is None
    assert metrics["tokens"]["token_source"] == "unavailable"


def test_shift_latency_verified_against_acceptable_titles():
    # 合法转向命中可接受标题
    valid_records = [
        EvaluationRecord(
            session_id="sess_shift_ok",
            cell_id="cell_a",
            turn=1,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="Title A",
            durable_subject="Subject A",
            acceptable_titles=["Title A"],
            allowed_shift=False,
            status="ok",
        ),
        EvaluationRecord(
            session_id="sess_shift_ok",
            cell_id="cell_a",
            turn=4,
            action="rename",
            candidate="Title B",
            pending=True,
            applied_title="Title A",
            durable_subject="Subject B",
            acceptable_titles=["Title B"],
            allowed_shift=True,
            intended_shift_turn=4,
            status="ok",
        ),
        EvaluationRecord(
            session_id="sess_shift_ok",
            cell_id="cell_a",
            turn=5,
            action="approve",
            candidate="Title B",
            pending=False,
            applied_title="Title B",
            durable_subject="Subject B",
            acceptable_titles=["Title B"],
            allowed_shift=True,
            intended_shift_turn=4,
            status="ok",
        ),
    ]

    metrics = compute_cell_metrics("cell_a", valid_records)
    assert metrics["shift_latency_turns"] == [1]
    assert metrics["failed_shifts"] == 0

    # 转向写入无关错误标题 -> 严禁记入有效延迟
    unrelated_records = [
        EvaluationRecord(
            session_id="sess_shift_bad",
            cell_id="cell_a",
            turn=4,
            action="approve",
            candidate="UNRELATED",
            pending=False,
            applied_title="UNRELATED",
            durable_subject="Subject B",
            acceptable_titles=["Title B"],
            allowed_shift=True,
            intended_shift_turn=4,
            status="ok",
        )
    ]
    bad_metrics = compute_cell_metrics("cell_a", unrelated_records)
    assert bad_metrics["shift_latency_turns"] == []
    assert bad_metrics["failed_shifts"] == 1


def test_mainline_coverage_usurpation_and_identifiers():
    records = [
        EvaluationRecord(
            session_id="sess_cov",
            cell_id="cell_a",
            turn=1,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="AutoTitler 架构重构",
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            required_identifiers=["AutoTitler"],
            allowed_shift=False,
            status="ok",
            input_tokens=100,
            output_tokens=10,
        ),
        EvaluationRecord(
            session_id="sess_cov",
            cell_id="cell_a",
            turn=2,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="修复某个局部报错",  # 发生局部篡权，偏离主线
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            secondary_topics=["修复某个局部报错"],
            required_identifiers=["AutoTitler"],
            allowed_shift=False,
            status="ok",
            input_tokens=120,
            output_tokens=12,
        ),
    ]

    metrics = compute_cell_metrics("cell_a", records)
    assert metrics["observed_turns"] == 2
    assert metrics["mainline_covered_turns"] == 1
    assert metrics["mainline_coverage_rate"] == 0.5
    assert metrics["local_usurped_turns"] == 1
    assert metrics["local_usurpation_rate"] == 0.5
    assert metrics["identifiers_preserved_turns"] == 1
    assert metrics["identifiers_eligible_turns"] == 2
    assert metrics["identifiers_preserved_rate"] == 0.5


def test_report_formatting_includes_cell_id_and_all_metrics():
    records = [
        EvaluationRecord(
            session_id="sess_123456789",
            cell_id="baseline",
            turn=1,
            action="rename",
            candidate="Cand",
            pending=True,
            applied_title="Init",
            durable_subject="Subject",
            status="ok",
            input_tokens=100,
            output_tokens=10,
        )
    ]
    table = format_per_turn_table(records)
    assert "| `sess_123...` | `baseline` | 1 | Subject | `rename` | Cand | True | **Init** | `ok` | 100/10 |" in table

    metrics = compute_cell_metrics("baseline", records)
    summary = format_aggregate_summary([metrics])
    assert "| `baseline` | 1 / 1 / 0 |" in summary
    assert "Hard Fail" in summary
    assert "Exact Label Hit" in summary
    assert "Undetermined" in summary
    assert "Confirmed Usurp" in summary
    assert "Mainline Cov" not in summary


def test_conservative_scoring_and_undetermined_bounds():
    """验证保守、诚实的可验证计分：

    - 轮次 1：严格白名单命中（严格命中白名单，未被篡权）
    - 轮次 2：确诊篡权（脱离主线，具备正向证据命中 secondary_topics）
    - 轮次 3：仅标识符命中，主线语义未定
    - 轮次 4：标识符也丢失，主线语义未定
    """
    records = [
        EvaluationRecord(
            session_id="sess_bounds",
            cell_id="cell_b",
            turn=1,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="AutoTitler 架构重构",
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            secondary_topics=["测试报错排查"],
            required_identifiers=["AutoTitler"],
            allowed_shift=False,
            status="ok",
            input_tokens=100,
            output_tokens=10,
        ),
        EvaluationRecord(
            session_id="sess_bounds",
            cell_id="cell_b",
            turn=2,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="测试报错排查",
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            secondary_topics=["测试报错排查"],
            required_identifiers=["AutoTitler"],
            allowed_shift=False,
            status="ok",
            input_tokens=100,
            output_tokens=10,
        ),
        EvaluationRecord(
            session_id="sess_bounds",
            cell_id="cell_b",
            turn=3,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="AutoTitler 提示词调优",
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            secondary_topics=["测试报错排查"],
            required_identifiers=["AutoTitler"],
            allowed_shift=False,
            status="ok",
            input_tokens=100,
            output_tokens=10,
        ),
        EvaluationRecord(
            session_id="sess_bounds",
            cell_id="cell_b",
            turn=4,
            action="keep",
            candidate=None,
            pending=False,
            applied_title="无关临时分支",
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            secondary_topics=["测试报错排查"],
            required_identifiers=["AutoTitler"],
            allowed_shift=False,
            status="ok",
            input_tokens=100,
            output_tokens=10,
        ),
    ]

    metrics = compute_cell_metrics("cell_b", records)
    assert metrics["observed_turns"] == 4
    # 严格白名单命中诊断（仅轮次 1）
    assert metrics["strict_whitelist_hit_turns"] == 1
    assert metrics["strict_whitelist_hit_rate"] == 0.25
    assert metrics["mainline_covered_turns"] == 1
    assert metrics["mainline_coverage_rate"] == 0.25

    # 确诊局部篡权（仅轮次 2 命中 secondary_topics）
    assert metrics["local_usurped_turns"] == 1
    assert metrics["local_usurpation_rate"] == 0.25

    # 两轮脱离短词表均未获语义审查，标识符正确也不能判作成功。
    assert metrics["undetermined_turns"] == 2
    assert metrics["undetermined_rate"] == 0.5

    # 唯有精确标注命中可判为明确未篡权。
    assert metrics["safe_mainline_turns"] == 1
    assert metrics["safe_mainline_rate"] == 0.25

    # 标识符保真度（轮次 1 与轮次 3）
    assert metrics["identifiers_preserved_turns"] == 2
    assert metrics["identifiers_eligible_turns"] == 4
    assert metrics["identifiers_preserved_rate"] == 0.5
