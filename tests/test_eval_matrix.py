import pytest
from eval.matrix import make_cells, validate_cells, MatrixCell
from hermes_auto_titler.config import DEFAULTS


def test_make_cells_baseline_and_orthogonality():
    cells = make_cells(DEFAULTS)
    assert len(cells) > 0

    base_cell = next(c for c in cells if c.cell_id == "baseline")
    assert base_cell.is_baseline is True
    assert base_cell.config_diff == {}

    # 检查单因素 cell
    single_factor_cells = [c for c in cells if not c.is_baseline and not c.is_interaction]
    assert len(single_factor_cells) >= 11

    for c in single_factor_cells:
        assert len(c.config_diff) == 1, f"cell {c.cell_id} diff must be exactly 1, got {c.config_diff}"
        diff_key = next(iter(c.config_diff))
        assert diff_key in DEFAULTS or diff_key in ("prompt_variant", "input_variant")

    # 检查 3 组预注册交互 cell
    interaction_cells = [c for c in cells if c.is_interaction]
    assert len(interaction_cells) == 3
    interaction_ids = {c.cell_id for c in interaction_cells}
    assert "summary_x_trim" in interaction_ids
    assert "horizon_x_summary" in interaction_ids
    assert "strategy_x_confirm" in interaction_ids


def test_validate_cells_rejects_lean_alias():
    # lean 别名一次改多个键且未声明为 interaction
    bad_cell = MatrixCell(
        cell_id="lean",
        name="lean alias",
        config_diff={"opening_turns": 1, "preview_chars": 60, "include_all_user_messages": False},
        is_baseline=False,
        is_interaction=False,
    )
    with pytest.raises(ValueError, match="orthogonal single factor.*len.*== 1"):
        validate_cells([bad_cell], base_config=DEFAULTS)


def test_production_fixture_matrix():
    from pathlib import Path
    import json
    from eval.matrix import dump_matrix_fixture
    fixture_path = Path(__file__).parent.parent / "eval" / "fixtures" / "matrix.json"
    cells = make_cells(DEFAULTS, host_title_enabled=False)
    dump_matrix_fixture(fixture_path, cells)
    assert fixture_path.exists()
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("schema") == 1
    assert len(data.get("cells", [])) == len(cells)
