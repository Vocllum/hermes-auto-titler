from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class EvaluationRecord:
    session_id: str
    cell_id: str
    turn: int
    action: str  # keep, rename, approve, error, skip
    candidate: Optional[str]
    pending: bool
    applied_title: str
    durable_subject: str
    allowed_shift: bool = False
    intended_shift_turn: Optional[int] = None
    is_manual_protected: bool = False
    status: str = "ok"  # ok, rate_limit, timeout, auth_error, parse_error, etc.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    elapsed_ms: Optional[float] = None


def compute_cell_metrics(cell_id: str, records: List[EvaluationRecord]) -> Dict[str, Any]:
    """根据单个 cell 的 prefix 执行记录计算质量、稳定度与资源消耗指标。"""
    # 严格按 (session_id, turn) 排序，防止边界串联
    sorted_records = sorted(records, key=lambda r: (r.session_id, r.turn))

    eligible_turns = len(sorted_records)
    observed_records = [r for r in sorted_records if r.status == "ok"]
    failed_records = [r for r in sorted_records if r.status != "ok"]
    observed_turns = len(observed_records)
    failed_turns = len(failed_records)

    error_breakdown: Dict[str, int] = {}
    for r in failed_records:
        error_breakdown[r.status] = error_breakdown.get(r.status, 0) + 1

    # 统计受保护会话违规写回
    protected_violation_count = 0
    for r in sorted_records:
        if r.is_manual_protected and r.action in ("rename", "approve"):
            protected_violation_count += 1

    hard_fail = protected_violation_count > 0

    # 统计改名行为 (区分 pending 与实际 applied)
    applied_renames = 0
    pending_renames = 0
    shift_latency_turns: List[int] = []

    # 按 session 独立跟踪状态
    session_groups: Dict[str, List[EvaluationRecord]] = {}
    for r in sorted_records:
        session_groups.setdefault(r.session_id, []).append(r)

    for sid, s_recs in session_groups.items():
        prev_title = None
        for r in s_recs:
            if r.status != "ok":
                continue
            if r.pending:
                pending_renames += 1
            if r.action == "approve" or (r.action == "rename" and not r.pending):
                if prev_title is not None and r.applied_title != prev_title:
                    applied_renames += 1
                elif prev_title is None and r.applied_title:
                    # 首次落地
                    pass

                # 计算转向延迟
                if r.allowed_shift and r.intended_shift_turn is not None:
                    latency = r.turn - r.intended_shift_turn
                    shift_latency_turns.append(latency)

            prev_title = r.applied_title

    # 资源消耗统计 (Token 严谨性：任何一条记录缺失 usage 则均值不可用，严禁伪造/均摊)
    has_missing_tokens = any(
        r.input_tokens is None or r.output_tokens is None for r in sorted_records
    )
    if has_missing_tokens or not observed_records:
        tokens_summary = {
            "average_input_tokens": None,
            "average_output_tokens": None,
            "token_source": "unavailable",
        }
    else:
        avg_in = sum(r.input_tokens for r in observed_records if r.input_tokens is not None) / observed_turns
        avg_out = sum(r.output_tokens for r in observed_records if r.output_tokens is not None) / observed_turns
        tokens_summary = {
            "average_input_tokens": avg_in,
            "average_output_tokens": avg_out,
            "token_source": "measured",
        }

    return {
        "cell_id": cell_id,
        "eligible_turns": eligible_turns,
        "observed_turns": observed_turns,
        "failed_turns": failed_turns,
        "error_breakdown": error_breakdown,
        "applied_renames": applied_renames,
        "pending_renames": pending_renames,
        "protected_violation_count": protected_violation_count,
        "hard_fail": hard_fail,
        "shift_latency_turns": shift_latency_turns,
        "tokens": tokens_summary,
    }
