import pytest
from eval.metrics import (
    compute_cell_metrics,
    is_acceptable_title,
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
    assert is_acceptable_title("Title B", None, "Title B") is True
    assert is_acceptable_title("  title b  ", ["Title B"], "durable subject") is True
    assert is_acceptable_title("AutoTitler 架构重构", ["AutoTitler 架构重构"], "AutoTitler 架构") is True


def test_is_usurped_title_mechanical_evaluation():
    # 1. 命中次主题/排错/噪声标签
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

    # 2. 禁止转向时脱离主线
    assert (
        is_usurped_title(
            title="无关临时分支",
            acceptable_titles=["AutoTitler 架构重构"],
            durable_subject="AutoTitler 架构重构",
            allowed_shift=False,
        )
        is True
    )

    # 3. 正常主线或允许转向时不属于篡权
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
    assert "Mainline Cov" in summary
