"""Injected-transport prompt comparisons; never opens a live Hermes database.

Frozen decisions compare identical incumbents and evidence. Evolution uses the
production synchronous evaluate/write path in a disposable SQLite directory,
not host hooks or a full-fidelity compaction database. No model calls happen
unless the caller supplies an LLM client.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
from typing import Any, Iterable

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.messages import load_context_with_summary
from hermes_auto_titler.policy import AutoTitler
from hermes_auto_titler.titler import _parse_decision

from .corpus import count_human_turns
from .replay import SandboxSqliteSessionDB, slice_prefix
from .trace import TraceLLM


VARIANTS = ("minimal", "concise", "detailed")
STYLES = ("concise", "complete")
STRATEGIES = ("conservative", "aggressive")
TRANSPORT_KINDS = ("real_http", "mock", "recorded_replay")
LANGUAGE_RULE = "Write the title in the primary natural language of the user's messages."
_BUDGET_KEYS = (
    "opening_turns", "recent_turns", "preview_chars", "include_all_user_messages",
    "ignore_model_messages", "user_message_threshold", "user_message_preview_chars",
    "summary_preview_chars", "max_title_length", "max_display_width",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PromptCell:
    variant: str
    style: str
    strategy: str

    def __post_init__(self) -> None:
        if self.variant not in VARIANTS or self.style not in STYLES or self.strategy not in STRATEGIES:
            raise ValueError("unknown prompt cell dimension")

    @property
    def cell_id(self) -> str:
        return f"{self.variant}/{self.style}/{self.strategy}"


def make_prompt_cells() -> list[PromptCell]:
    return [PromptCell(*values) for values in itertools.product(VARIANTS, STYLES, STRATEGIES)]


@dataclass(frozen=True)
class FrozenCase:
    case_id: str
    events: list[dict[str, Any]]
    prefix: int
    current_title: str | None = None
    proposed_title: str | None = None
    provenance: str = "synthetic"
    category: str = "unspecified"
    initial_title_source: str | None = "llm"

    def __post_init__(self) -> None:
        if not self.case_id or self.provenance not in ("real_redacted", "synthetic", "synthetic_adversarial"):
            raise ValueError("case requires an ID and explicit supported provenance")
        if (not isinstance(self.prefix, int) or isinstance(self.prefix, bool)
                or self.prefix < 1 or self.prefix > count_human_turns(self.events)):
            raise ValueError("prefix must be within the actual human-turn count")
        if self.initial_title_source not in (None, "llm", "derived", "user"):
            raise ValueError("unknown initial title source")


def load_cases(path: str | Path) -> list[FrozenCase]:
    """Read an already-redacted JSON array; this is not a redaction service."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("case file must be an array")
    cases = [FrozenCase(**row) for row in data]
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("duplicate case ID")
    return cases


class _EvidenceDB:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = copy.deepcopy(events)

    def get_messages_as_conversation(self, session_id: str, *, include_compacted: bool = False,
                                     **kwargs: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(event) for event in self.events
                if event.get("active", 1) or (include_compacted and event.get("compacted", 0))]


class _AtomicSandbox(SandboxSqliteSessionDB):
    def _execute_write(self, operation: Any) -> Any:
        # Match the production CAS boundary instead of the legacy two-step fallback.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result = operation(self.conn)
            self.conn.execute("COMMIT")
            return result
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise


class _LedgerLlm:
    def __init__(self, llm: Any, *, transport_kind: str) -> None:
        if transport_kind not in TRANSPORT_KINDS:
            raise ValueError("transport kind must distinguish HTTP, mock and recorded replay")
        self.records: list[dict[str, Any]] = []
        self.transport_kind = transport_kind
        self.tracer = TraceLLM(llm, self.records)

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        parameters = {key: kwargs[key] for key in (
            "temperature", "max_tokens", "timeout", "purpose", "task", "model", "provider"
        ) if key in kwargs}
        try:
            response = self.tracer.complete(messages, **kwargs)
        except Exception:
            if self.records:
                self.records[-1].update({"transport_kind": self.transport_kind,
                                        "parser_valid": False, "attempt": len(self.records),
                                        "request_parameters": parameters})
            raise
        record = self.records[-1]
        text = str(getattr(response, "text", "") or "")
        action, title = _parse_decision(text)
        record.update({
            "transport_kind": self.transport_kind,
            "response_hash": _digest(text),
            "parser_valid": action != "error",
            "model_action": action,
            "model_title": title,
            "attempt": len(self.records),
            "request_parameters": parameters,
        })
        return response


def _config(cell: PromptCell, config: dict[str, Any] | None) -> dict[str, Any]:
    result = {**DEFAULTS, **(config or {})}
    result.update(prompt_variant=cell.variant, title_style=cell.style, strategy=cell.strategy)
    return result


def _header(case: FrozenCase, cell: PromptCell, revision: str, cfg: dict[str, Any],
            language_rule: str, prefix: int) -> dict[str, Any]:
    return {
        "case_id": case.case_id, "prefix": prefix, "provenance": case.provenance,
        "category": case.category, "cell_id": cell.cell_id,
        "variant": cell.variant, "style": cell.style, "strategy": cell.strategy,
        "revision": revision, "evidence_hash": _digest(slice_prefix(case.events, prefix)),
        "context_budget": {key: cfg.get(key) for key in _BUDGET_KEYS},
        "language_rule": language_rule,
    }


def run_frozen(case: FrozenCase, cell: PromptCell, llm: Any, *, revision: str,
               config: dict[str, Any] | None = None, max_attempts: int = 1,
               transport_kind: str = "mock", titler_class: Any = AutoTitler,
               language_rule: str | None = None) -> dict[str, Any]:
    """Counterfactual decision on a fixed incumbent, using production sampling.

    Each call owns its tracer and may be scheduled independently by the caller.
    Retries are explicit; clients with hidden retries cannot provide a complete
    network-attempt ledger and should disable those retries.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    cfg = _config(cell, config)
    rule = language_rule or LANGUAGE_RULE
    ledger = _LedgerLlm(llm, transport_kind=transport_kind)
    db = _EvidenceDB(slice_prefix(case.events, case.prefix))
    recent, users, opening, summary, timeline = load_context_with_summary(
        db, case.case_id, cfg["recent_turns"], cfg["include_all_user_messages"],
        cfg["opening_turns"], cfg["ignore_model_messages"], cfg["preview_chars"],
        cfg["user_message_threshold"], cfg["user_message_preview_chars"],
        summary_chars=cfg["summary_preview_chars"], include_timeline=True,
    )
    titler = titler_class(SimpleNamespace(llm=ledger), cfg, db=db)
    titler._language_rule = lambda: rule
    ledger.tracer.set_context(case.case_id, case.prefix, cell.cell_id)
    action, title = "error", None
    for _ in range(max_attempts):
        action, title = titler._generate(
            case.current_title, recent, users, opening, proposed=case.proposed_title,
            force_rename=not case.current_title or case.initial_title_source == "derived",
            earlier_summary=summary, timeline=timeline,
        )
        if action != "error":
            break
    applied = case.current_title
    if action == "rename" and title:
        applied = titler._prepare_candidate(title)
    elif action == "approve":
        applied = titler._prepare_candidate(case.proposed_title) if case.proposed_title else None
    valid = (
        (action == "keep" and bool(case.current_title))
        or (action in ("rename", "approve") and bool(applied))
    )
    return {
        **_header(case, cell, revision, cfg, rule, case.prefix), "mode": "frozen_decision",
        "status": "ok" if valid else "failed",
        "model_action": action, "candidate": title,
        "title_before": case.current_title, "title_after": applied,
        "changed": valid and applied != case.current_title,
        "attempts": ledger.records,
    }


def run_evolution(case: FrozenCase, cell: PromptCell, llm: Any, workdir: str | Path, *,
                  revision: str, prefixes: Iterable[int] | None = None,
                  config: dict[str, Any] | None = None, transport_kind: str = "mock",
                  titler_class: Any = AutoTitler,
                  language_rule: str | None = None) -> list[dict[str, Any]]:
    """Serial evaluate/CAS/write/review replay; no host hook or scheduler claims.

    Evaluations are forced synchronously, intentionally excluding cooldown and
    every_n_turns gates. Pending confirmations and candidate cleaning stay real.
    The basic sandbox cannot recreate inactive/compacted provenance accurately.
    """
    points = list(prefixes) if prefixes is not None else [case.prefix]
    total = count_human_turns(case.events)
    if (not points or any(not isinstance(point, int) or isinstance(point, bool) for point in points)
            or points != sorted(set(points)) or points[0] < 1 or points[-1] > total):
        raise ValueError("evolution prefixes must be unique, increasing, valid human turns")
    if any(not event.get("active", 1) for event in case.events):
        raise ValueError("basic evolution sandbox cannot replay inactive/compacted rows")
    cfg = _config(cell, config)
    rule = language_rule or LANGUAGE_RULE
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    ledger = _LedgerLlm(llm, transport_kind=transport_kind)
    sandbox_dir = Path(tempfile.mkdtemp(prefix="paired-", dir=root))
    db = _AtomicSandbox(sandbox_dir / "state.db")
    sid = "paired-session"
    titler = titler_class(SimpleNamespace(llm=ledger), cfg, db=db)
    titler._state_path = sandbox_dir / "state.json"
    titler._language_rule = lambda: rule
    transitions = []
    try:
        db.create_session(sid)
        if case.current_title is not None:
            db.set_session_title(sid, case.current_title)
            if case.initial_title_source is not None:
                db.set_session_title_source(sid, case.initial_title_source)
        if case.current_title and hasattr(titler, "_managed_titles"):
            titler._managed_titles[sid] = case.current_title
        previous_count = 0
        for prefix in points:
            events = slice_prefix(case.events, prefix)
            db.append_messages_batch(sid, events[previous_count:])
            previous_count = len(events)
            before = db.get_session_title(sid)
            first_attempt = len(ledger.records)
            ledger.tracer.set_context(case.case_id, prefix, cell.cell_id)
            result = titler.evaluate(sid, force=True)
            after = db.get_session_title(sid)
            attempts = copy.deepcopy(ledger.records[first_attempt:])
            transitions.append({
                **_header(case, cell, revision, cfg, rule, prefix),
                "mode": "synchronous_evolution", "scheduler_gates": "bypassed",
                "status": "failed" if result.get("action") == "failed" else "ok",
                "state_action": result.get("action"), "state_result": result,
                "model_action": attempts[-1].get("model_action") if attempts else None,
                "candidate": result.get("candidate") or result.get("title"),
                "title_before": before, "title_after": after, "changed": before != after,
                "pending": copy.deepcopy(titler._pending.get(sid)),
                "title_source": db.get_session_title_source(sid),
                "attempts": attempts, "sandbox_path": str(sandbox_dir),
            })
        return transitions
    finally:
        db.close()


def _private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def export_blind(records: Iterable[dict[str, Any]], review_path: str | Path,
                 mapping_path: str | Path, *, seed: int = 17,
                 cases: Iterable[FrozenCase] | None = None) -> dict[str, int]:
    """Write private, blinded candidate packets; not safe for public uploading.

    Reviewer packets hide route/cell/revision. Optional case evidence is already
    redacted by the caller and never includes a case ID or original title.
    """
    review_file, mapping_file = Path(review_path), Path(mapping_path)
    if review_file.resolve() == mapping_file.resolve():
        raise ValueError("review and private mapping paths must differ")
    case_map = {case.case_id: case for case in (cases or [])}
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    excluded = 0
    for record in records:
        if record.get("status") != "ok" or not record.get("title_after"):
            excluded += 1
            continue
        groups.setdefault((record["case_id"], record["prefix"]), []).append(record)
    rng = random.Random(seed)
    keys = list(groups)
    rng.shuffle(keys)
    packets, mappings = [], {}
    for index, key in enumerate(keys, 1):
        review_id = f"review-{index:04d}"
        shuffled = list(groups[key])
        rng.shuffle(shuffled)
        candidates = []
        for number, record in enumerate(shuffled, 1):
            candidate_id = f"{review_id}-{number:03d}"
            candidates.append({"candidate_id": candidate_id, "title": record["title_after"]})
            mappings[candidate_id] = copy.deepcopy(record)
        packet: dict[str, Any] = {"review_id": review_id, "candidates": candidates}
        if key[0] in case_map:
            packet["evidence"] = [
                {"role": event.get("role"), "content": event.get("content")}
                for event in slice_prefix(case_map[key[0]].events, key[1])
            ]
        packets.append(packet)
    _private_json(review_file, packets)
    _private_json(mapping_file, mappings)
    return {"groups": len(packets), "candidates": len(mappings), "excluded": excluded}
