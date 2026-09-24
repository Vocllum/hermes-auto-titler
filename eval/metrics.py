from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import unicodedata


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
    initial_title: Optional[str] = None
    acceptable_titles: Optional[List[str]] = None
    secondary_topics: Optional[List[str]] = None
    required_identifiers: Optional[List[str]] = None
    allowed_shift: bool = False
    intended_shift_turn: Optional[int] = None
    is_manual_protected: bool = False
    status: str = "ok"  # ok, rate_limit, timeout, auth_error, parse_error, etc.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    elapsed_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if self.is_manual_protected and not (self.initial_title and self.initial_title.strip()):
            raise ValueError(
                f"session {self.session_id} turn {self.turn} is manual protected: "
                f"initial_title must be explicitly provided and non-empty"
            )


def normalize_title(t: str) -> str:
    """Unicode NFKC 归一化，去除首尾空白，小写折叠。"""
    if not t:
        return ""
    return unicodedata.normalize("NFKC", t).strip().lower()


def is_acceptable_title(
    title: str,
    acceptable_titles: Optional[List[str]],
    durable_subject: str,
) -> bool:
    """判定标题是否命中可接受标题集合或主线主题（全词等值精确匹配，禁止子串误判）。"""
    if not title:
        return False
    t_norm = normalize_title(title)
    if not t_norm:
        return False
    if acceptable_titles:
        return t_norm in {normalize_title(acc) for acc in acceptable_titles if acc and acc.strip()}
    return t_norm == normalize_title(durable_subject)


def is_usurped_title(
    title: str,
    acceptable_titles: Optional[List[str]],
    durable_subject: str,
    allowed_shift: bool,
    secondary_topics: Optional[List[str]] = None,
) -> bool:
    """判定标题是否被局部噪声、排错或未授权次主题篡权（机械判定，非人工透传旗标）。"""
    if not title:
        return False
    t_norm = normalize_title(title)
    if not t_norm:
        return False

    # 1. 标题等值命中了已声明的次级/局部排错/噪声主题
    if secondary_topics:
        if t_norm in {normalize_title(sec) for sec in secondary_topics if sec and sec.strip()}:
            return True

    # 2. 在明确禁止转向的前提下，标题脱离了可接受主线集合，判定为主线被局部篡权
    if not allowed_shift:
        if not is_acceptable_title(title, acceptable_titles, durable_subject):
            return True

    return False


def compute_cell_metrics(cell_id: str, records: List[EvaluationRecord]) -> Dict[str, Any]:
    """根据单个 cell 的 prefix 执行记录计算质量、稳定度、主线覆盖与资源消耗指标。"""
    sorted_records = sorted(records, key=lambda r: (r.session_id, r.turn))

    eligible_turns = len(sorted_records)
    observed_records = [r for r in sorted_records if r.status == "ok"]
    failed_records = [r for r in sorted_records if r.status != "ok"]
    observed_turns = len(observed_records)
    failed_turns = len(failed_records)

    error_breakdown: Dict[str, int] = {}
    for r in failed_records:
        error_breakdown[r.status] = error_breakdown.get(r.status, 0) + 1

    # 统计受保护会话违规写回与漂移
    protected_violation_count = 0
    for r in sorted_records:
        if r.is_manual_protected:
            has_action_violation = r.action in ("rename", "approve")
            has_title_drift = (
                r.initial_title is None
                or normalize_title(r.applied_title) != normalize_title(r.initial_title)
            )
            if has_action_violation or has_title_drift:
                protected_violation_count += 1

    hard_fail = protected_violation_count > 0

    # 统计改名行为、意图转向延迟与主线覆盖
    applied_renames = 0
    pending_renames = 0
    shift_latency_turns: List[int] = []
    failed_shifts = 0
    missed_renames = 0
    mainline_covered_turns = 0
    local_usurped_turns = 0
    identifiers_preserved_turns = 0
    identifiers_eligible_turns = 0

    # 按 session 独立跟踪状态
    session_groups: Dict[str, List[EvaluationRecord]] = {}
    for r in sorted_records:
        session_groups.setdefault(r.session_id, []).append(r)

    for sid, s_recs in session_groups.items():
        prev_title = None
        shift_resolved_for_session = False
        shift_targeted = any(r.allowed_shift and r.intended_shift_turn is not None for r in s_recs)

        for r in s_recs:
            if r.status != "ok":
                continue

            # 主线覆盖评估（全词等值匹配）
            if is_acceptable_title(r.applied_title, r.acceptable_titles, r.durable_subject):
                mainline_covered_turns += 1

            # 局部篡权评估（机械逻辑判定）
            if is_usurped_title(
                r.applied_title,
                r.acceptable_titles,
                r.durable_subject,
                r.allowed_shift,
                r.secondary_topics,
            ):
                local_usurped_turns += 1

            # 标识符保真度评估（精确大小写与字符匹配）
            if r.required_identifiers:
                identifiers_eligible_turns += 1
                if all(ident in r.applied_title for ident in r.required_identifiers):
                    identifiers_preserved_turns += 1

            if r.pending:
                pending_renames += 1

            # 实际落库改名
            if r.action == "approve" or (r.action == "rename" and not r.pending):
                if prev_title is not None and r.applied_title != prev_title:
                    applied_renames += 1

                # 转向判定：必须写进可接受/正确的标题才计入有效转向延迟
                if r.allowed_shift and r.intended_shift_turn is not None and not shift_resolved_for_session:
                    if is_acceptable_title(r.applied_title, r.acceptable_titles, r.durable_subject):
                        latency = r.turn - r.intended_shift_turn
                        shift_latency_turns.append(latency)
                        shift_resolved_for_session = True

            # 检查漏改：已经进入转向阶段且非 pending，但动作仍为 keep 且标题仍是旧主题
            if (
                r.allowed_shift
                and r.intended_shift_turn is not None
                and r.turn >= r.intended_shift_turn
                and r.action == "keep"
                and not is_acceptable_title(r.applied_title, r.acceptable_titles, r.durable_subject)
            ):
                missed_renames += 1

            prev_title = r.applied_title

        # 如果会话有转向需求，但整场下来未成功转向
        if shift_targeted and not shift_resolved_for_session:
            failed_shifts += 1

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

    mainline_cov_rate = (mainline_covered_turns / observed_turns) if observed_turns else 0.0
    local_usurp_rate = (local_usurped_turns / observed_turns) if observed_turns else 0.0
    ident_preserve_rate = (
        (identifiers_preserved_turns / identifiers_eligible_turns)
        if identifiers_eligible_turns
        else 1.0
    )

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
        "failed_shifts": failed_shifts,
        "missed_renames": missed_renames,
        "mainline_covered_turns": mainline_covered_turns,
        "mainline_coverage_rate": mainline_cov_rate,
        "local_usurped_turns": local_usurped_turns,
        "local_usurpation_rate": local_usurp_rate,
        "identifiers_preserved_turns": identifiers_preserved_turns,
        "identifiers_eligible_turns": identifiers_eligible_turns,
        "identifiers_preserved_rate": ident_preserve_rate,
        "tokens": tokens_summary,
    }
