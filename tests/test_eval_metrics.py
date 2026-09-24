import pytest
from eval.metrics import compute_cell_metrics, EvaluationRecord


def test_rename_counts_and_pending_distinction():
    # 模拟单个 session 在同一个 cell 下的 prefix 时序
    records = [
        # turn 1: 首次命名
        EvaluationRecord(
            session_id="sess_1",
            cell_id="cell_a",
            turn=1,
            action="rename",
            candidate="Title Turn 1",
            pending=True,  # rename_confirmations=1，首次为 pending
            applied_title="Initial Title",
            durable_subject="Subject 1",
            allowed_shift=False,
            status="ok",
        ),
        # turn 2: 确认并落库
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
        # turn 3: keep 稳定
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
    # applied_title 只发生了一次实际改名 (approve)
    assert metrics["applied_renames"] == 1
    assert metrics["pending_renames"] == 1
    assert metrics["eligible_turns"] == 3
    assert metrics["observed_turns"] == 3
    assert metrics["failed_turns"] == 0


def test_protected_session_violation_triggers_hard_fail():
    records = [
        EvaluationRecord(
            session_id="sess_protected",
            cell_id="cell_a",
            turn=1,
            action="rename",
            candidate="Malicious Rewrite",
            pending=False,
            applied_title="Malicious Rewrite",
            durable_subject="Protected Subject",
            allowed_shift=False,
            is_manual_protected=True,
            status="ok",
        )
    ]
    metrics = compute_cell_metrics("cell_a", records)
    assert metrics["protected_violation_count"] == 1
    assert metrics["hard_fail"] is True


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
            input_tokens=None,  # missing usage
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
    # 存在 missing usage，平均 token 标记为 unavailable
    assert metrics["tokens"]["average_input_tokens"] is None
    assert metrics["tokens"]["token_source"] == "unavailable"


def test_shift_latency_computation():
    records = [
        # turn 1: 主题 A
        EvaluationRecord(
            session_id="sess_shift",
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
        # turn 4: 发生合法意图转向 (Subject B)
        EvaluationRecord(
            session_id="sess_shift",
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
        # turn 5: approve 写库
        EvaluationRecord(
            session_id="sess_shift",
            cell_id="cell_a",
            turn=5,
            action="approve",
            candidate="Title B",
            pending=False,
            applied_title="Title B",
            durable_subject="Subject B",
            allowed_shift=True,
            intended_shift_turn=4,
            status="ok",
        ),
    ]

def test_report_formatting_with_denominators():
    from eval.report import format_per_turn_table, format_aggregate_summary

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
    assert "| `sess_123...` | 1 | Subject | `rename` | Cand | True | **Init** | `ok` | 100/10 |" in table

    metrics = compute_cell_metrics("baseline", records)
    summary = format_aggregate_summary([metrics])
    assert "| `baseline` | 1 / 1 / 0 | 0 | 1 | False | 100.0 / 10.0 |" in summary
