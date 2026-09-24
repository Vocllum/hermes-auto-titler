from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class MatrixCell:
    cell_id: str
    name: str
    config_diff: Dict[str, Any] = field(default_factory=dict)
    is_baseline: bool = False
    is_interaction: bool = False
    prompt_variant: Optional[str] = None  # harness only
    input_variant: Optional[str] = None   # harness only
    host_title_enabled: Optional[bool] = None

    def effective_config(self, base_config: Dict[str, Any]) -> Dict[str, Any]:
        cfg = dict(base_config)
        cfg.update(self.config_diff)
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "name": self.name,
            "config_diff": self.config_diff,
            "is_baseline": self.is_baseline,
            "is_interaction": self.is_interaction,
            "prompt_variant": self.prompt_variant,
            "input_variant": self.input_variant,
            "host_title_enabled": self.host_title_enabled,
        }


def make_cells(
    base_config: Dict[str, Any],
    *,
    host_title_enabled: Optional[bool] = None,
) -> List[MatrixCell]:
    """生成正交单因素与 3 组预注册交互 cell。"""
    cells: List[MatrixCell] = []

    # 1. Baseline
    cells.append(
        MatrixCell(
            cell_id="baseline",
            name="Production Baseline (DEFAULTS)",
            config_diff={},
            is_baseline=True,
            is_interaction=False,
            host_title_enabled=host_title_enabled,
        )
    )

    # 2. 正交单因素对比 (len(diff) == 1)
    single_factors: List[Tuple[str, str, Dict[str, Any]]] = [
        ("summary_2400", "summary_preview_chars 2400", {"summary_preview_chars": 2400}),
        ("preview_60", "preview_chars 60", {"preview_chars": 60}),
        ("opening_3", "opening_turns 3", {"opening_turns": 3}),
        ("recent_4", "recent_turns 4", {"recent_turns": 4}),
        ("ignore_model_true", "ignore_model_messages True", {"ignore_model_messages": True}),
        ("user_threshold_20", "user_message_threshold 20", {"user_message_threshold": 20}),
        ("strategy_aggressive", "strategy aggressive", {"strategy": "aggressive"}),
        ("style_complete", "title_style complete", {"title_style": "complete"}),
        ("rename_confirm_0", "rename_confirmations 0", {"rename_confirmations": 0}),
        ("min_interval_0", "min_interval_minutes 0", {"min_interval_minutes": 0}),
        ("every_1_turn", "every_n_turns 1", {"every_n_turns": 1}),
    ]

    for cid, name, diff in single_factors:
        cells.append(
            MatrixCell(
                cell_id=cid,
                name=name,
                config_diff=diff,
                is_baseline=False,
                is_interaction=False,
                host_title_enabled=host_title_enabled,
            )
        )

    # 3. 预注册 3 组机制交叉组合
    interactions: List[Tuple[str, str, Dict[str, Any]]] = [
        (
            "summary_x_trim",
            "摘要预算 × 助手消息强剪裁",
            {"summary_preview_chars": 2400, "ignore_model_messages": True},
        ),
        (
            "horizon_x_summary",
            "opening/recent 视野 × 结构化长摘要",
            {"opening_turns": 3, "recent_turns": 4, "summary_preview_chars": 2400},
        ),
        (
            "strategy_x_confirm",
            "激进判定 × 免背书直接写",
            {"strategy": "aggressive", "rename_confirmations": 0},
        ),
    ]

    for cid, name, diff in interactions:
        cells.append(
            MatrixCell(
                cell_id=cid,
                name=name,
                config_diff=diff,
                is_baseline=False,
                is_interaction=True,
                host_title_enabled=host_title_enabled,
            )
        )

    return cells


def validate_cells(cells: List[MatrixCell], base_config: Dict[str, Any]) -> bool:
    """严格校验矩阵 cell 的正交性与完整性。"""
    seen_ids = set()
    for c in cells:
        if c.cell_id in seen_ids:
            raise ValueError(f"duplicate cell_id: {c.cell_id}")
        seen_ids.add(c.cell_id)

        if c.is_baseline:
            if len(c.config_diff) != 0:
                raise ValueError("baseline cell cannot have config_diff")
        elif not c.is_interaction:
            # 单因素必须严格为 1 个差异键，严禁 lean 等隐式改多键
            if len(c.config_diff) != 1:
                raise ValueError(
                    f"orthogonal single factor cell {c.cell_id} diff len must == 1, got {len(c.config_diff)}"
                )

        full_cfg = c.effective_config(base_config)
        for k in base_config:
            if k not in full_cfg:
                raise ValueError(f"cell {c.cell_id} missing base key: {k}")

    return True


def dump_matrix_fixture(path: Union[str, Path], cells: List[MatrixCell]) -> None:
    data = {"schema": 1, "cells": [c.to_dict() for c in cells]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
