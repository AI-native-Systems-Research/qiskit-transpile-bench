"""Coverage selection and its static planning estimate must remain reproducible."""

import copy

import pytest
from compare_to_baseline_benchpress.panel import estimate_budget, select_panel


def entry(name, topology="heavy-hex", *, suite="abstract-qasmbench-small", width=8, **kwargs):
    return {"node_id": f"{suite}/{name}/{topology}", "panel_id": f"{name}-{topology}",
            "input_name": name, "input_hash": name, "input_qubits": width,
            "topology": topology, "suite": suite, "eligible": True, **kwargs}


def test_input_first_selection_is_stable_and_covers_topologies(tmp_path):
    inventory = [entry(f"c{i}", topology) for i in range(8)
                 for topology in ("heavy-hex", "linear", "square", "all-to-all")]
    before = copy.deepcopy(inventory)
    selected, metadata = select_panel(inventory, tmp_path, panel_size=8)
    assert len({row["input_hash"] for row in selected}) == 8
    assert set(metadata["topology_counts"]) == {"heavy-hex", "linear", "square", "all-to-all"}
    assert metadata["selected_logical_inputs"] == 8
    assert metadata["exclusion_counts"] == {"fixed panel-size budget": 24}
    assert select_panel(inventory[::-1], tmp_path, panel_size=8) == (selected, metadata)
    assert inventory == before


def test_repeated_target_round_preserves_device_and_abstract_hamlib(tmp_path):
    ham = {"ham_category": "chemistry", "ham_terms": 100}
    inventory = [entry(f"ham{i}", topology, suite=suite, hamiltonian=ham)
                 for i in range(4)
                 for topology, suite in [("fake_torino", "device-hamlib"),
                                          ("linear", "abstract-hamlib")]]
    inventory += [entry("feynman", "fake_torino", suite="device-feynman")]
    first, _ = select_panel(inventory, tmp_path, panel_size=5)
    assert {row["suite"] for row in first} == {"device-hamlib", "device-feynman"}
    selected, metadata = select_panel(inventory, tmp_path, panel_size=9)
    assert len(selected) == 9
    assert metadata["selected_logical_inputs"] == 5
    assert metadata["suite_counts"]["abstract-hamlib"] == 4


def test_size_caps_are_explicit_and_small_remaining_panel_is_complete(tmp_path):
    (tmp_path / "large.qasm").write_text("x q[0];\n" * 30_001)
    inventory = [entry("valid"), entry("wide", width=101),
                 entry("dense", input_path="large.qasm"),
                 entry("ham", suite="device-hamlib", hamiltonian={
                     "ham_category": "chemistry", "ham_terms": 2001}),
                 entry("too-wide-for-device", eligible=False,
                       exclusion_reason="input exceeds device capacity")]
    selected, metadata = select_panel(inventory, tmp_path)
    assert [row["input_name"] for row in selected] == ["valid"]
    assert metadata["selected_panel_size"] == 1
    assert len(metadata["excluded"]) == 4
    assert set(metadata["exclusion_counts"]) == {
        "more than 100 logical qubits", "more than 30000 QASM statements",
        "more than 2000 Hamiltonian terms", "input exceeds device capacity"}


def test_source_input_cannot_escape_pinned_tree(tmp_path):
    with pytest.raises(ValueError, match="escapes source directory"):
        select_panel([entry("escape", input_path="../elsewhere.qasm")], tmp_path)
    with pytest.raises(FileNotFoundError):
        select_panel([entry("missing", input_path="missing.qasm")], tmp_path)


def test_duplicate_inventory_and_empty_selection_are_errors(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        select_panel([entry("same"), entry("same")], tmp_path)
    with pytest.raises(ValueError, match="No eligible"):
        select_panel([entry("large", width=200)], tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        select_panel([entry("valid")], tmp_path, panel_size=0)


def test_planning_estimate_counts_both_arms_and_overrides():
    default = estimate_budget(400)
    assert default["expected_observations"] == 1600
    assert default["assumptions"]["workers"] == 6
    assert default["estimated_wall_minutes"] == pytest.approx(89.4444444444)
    assert default["assumptions"]["full_panel_measured"] is False
    doubled = estimate_budget(400, seed_count=2)
    preparation = default["assumptions"]["preparation_minutes"]
    assert doubled["estimated_wall_minutes"] - preparation == pytest.approx(
        2 * (default["estimated_wall_minutes"] - preparation)
    )
    fewer_workers = estimate_budget(400, workers=3)
    assert fewer_workers["estimated_wall_minutes"] - preparation == pytest.approx(
        2 * (default["estimated_wall_minutes"] - preparation)
    )
    assert estimate_budget(400, trials=2)["estimated_wall_minutes"] is None
