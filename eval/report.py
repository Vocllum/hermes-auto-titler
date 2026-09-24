from __future__ import annotations

from typing import Any, Dict, List
from eval.metrics import EvaluationRecord


def format_per_turn_table(records: List[EvaluationRecord]) -> str:
    """生成逐会话逐轮次标题演化明细表（脱敏），包含 Cell ID 列支持逐 cell 逐轮对照。"""
    lines = [
        "| Session ID | Cell ID | Turn | Durable Subject | Action | Candidate | Pending | Applied Title | Status | Tokens (In/Out) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(records, key=lambda x: (x.session_id, x.cell_id, x.turn)):
        cand_str = r.candidate or "-"
        tok_str = f"{r.input_tokens}/{r.output_tokens}" if r.input_tokens is not None else "unavailable"
        lines.append(
            f"| `{r.session_id[:8]}...` | `{r.cell_id}` | {r.turn} | {r.durable_subject} | `{r.action}` | {cand_str} | {r.pending} | **{r.applied_title}** | `{r.status}` | {tok_str} |"
        )
    return "\n".join(lines)


def format_aggregate_summary(cell_metrics_list: List[Dict[str, Any]]) -> str:
    """生成聚合指标对照表，必须带明确的 eligible/observed/failed 分母及质量维度指标。"""
    lines = [
        "| Cell ID | Eligible / Observed / Failed | Mainline Cov | Usurp Rate | Applied / Pending Renames | Shift Latency | Hard Fail | Avg In / Out Tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in cell_metrics_list:
        denoms = f"{m['eligible_turns']} / {m['observed_turns']} / {m['failed_turns']}"
        cov_str = f"{m['mainline_coverage_rate']:.1%}" if m['observed_turns'] else "N/A"
        usurp_str = f"{m['local_usurpation_rate']:.1%}" if m['observed_turns'] else "N/A"
        renames_str = f"{m['applied_renames']} / {m['pending_renames']}"
        latencies = m.get("shift_latency_turns", [])
        lat_str = f"{sum(latencies)/len(latencies):.1f}t" if latencies else "N/A"
        tokens_info = m["tokens"]
        if tokens_info["token_source"] == "measured":
            tok_str = f"{tokens_info['average_input_tokens']:.1f} / {tokens_info['average_output_tokens']:.1f}"
        else:
            tok_str = "unavailable"
        lines.append(
            f"| `{m['cell_id']}` | {denoms} | {cov_str} | {usurp_str} | {renames_str} | {lat_str} | {m['hard_fail']} | {tok_str} |"
        )
    return "\n".join(lines)
