import pytest
from eval.metrics import compute_cell_metrics, EvaluationRecord
from eval.report import format_per_turn_table, format_aggregate_summary


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


def test_protected_session_violation_triggers_hard_fail_on_action_and_drift():
    # 动作违规
    rec_action = [
        EvaluationRecord(
            session_id="sess_prot_1",
            cell_id="cell_a",
            turn=1,
            action="rename",
            candidate="Malicious Rewrite",
            pending=False,
            applied_title="Malicious Rewrite",
            durable_subject="Protected Subject",
            initial_title="Protected Subject",
            is_manual_protected=True,
            status="ok",
        )
    ]
    metrics = compute_cell_metrics("cell_a", rec_action)
    assert metrics["protected_violation_count"] == 1
    assert metrics["hard_fail"] is True

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
            acceptable_titles=["Title B", "Subject B"],
            allowed_shift=True,
            intended_shift_turn=4,
            status="ok",
        ),
    ]

    metrics = compute_cell_metrics("cell_a", valid_records)
    # 明确钉住断言：turn 4 触发，turn 5 完成写入有效标题，延迟为 1
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
            acceptable_titles=["Title B", "Subject B"],
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
            applied_title="AutoTitler 架构重构与优化",
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构", "AutoTitler 优化"],
            required_identifiers=["AutoTitler"],
            is_local_usurpation=False,
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
            applied_title="修复某个局部报错",  # 发生局部篡权，丢掉主线与标识符
            durable_subject="AutoTitler 架构重构",
            acceptable_titles=["AutoTitler 架构重构"],
            required_identifiers=["AutoTitler"],
            is_local_usurpation=True,
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
    # 明确验证 Cell ID 列存在
    assert "| `sess_123...` | `baseline` | 1 | Subject | `rename` | Cand | True | **Init** | `ok` | 100/10 |" in table

    metrics = compute_cell_metrics("baseline", records)
    summary = format_aggregate_summary([metrics])
    assert "| `baseline` | 1 / 1 / 0 |" in summary
    assert "Hard Fail" in summary
    assert "Mainline Cov" in summary
