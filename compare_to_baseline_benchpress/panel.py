"""A broad, deterministic Benchpress panel with a static collection budget.

Selection is input-first: see each bounded circuit before adding another target
for any circuit. The estimate is planning evidence, never a runtime acceptance
check, a calibration pass, or a deadline that truncates the selected panel.
"""

from collections import Counter, defaultdict, deque
from pathlib import Path

DEFAULT_PANEL_SIZE = 400
MAX_INPUT_QUBITS = 100
MAX_QASM_STATEMENTS = 30_000
MAX_HAMILTONIAN_TERMS = 2_000
TOPOLOGIES = ("heavy-hex", "linear", "square", "all-to-all")

# Existing local release comparison, with sequential, isolated pytest invocations.
# Includes source/environment preparation and Python/pytest startup, not just pm.run.
HISTORICAL_REFERENCE = {
    "session": "swap-absorption-full-20261028-151026",
    "evidence": "stages/benchpress/state.json and summary.json",
    "machine": "Apple M1 Max, 10 cores, arm64, Python 3.11.11",
    "panel_size": 50,
    "optimization_levels": [2, 3],
    "seeds_per_arm": 3,
    "observations": 600,
    "wall_seconds": 2050.122127166949,
    "summed_compile_seconds": 679.9158718949184,
    "date": "2026-10-28",
}


def estimate_budget(panel_size, *, seed_count=1, workers=6, levels=(2, 3), trials=20):
    """Estimate scheduling capacity from a provisional per-observation allowance.

    Current-worker smoke tests exposed more startup overhead than the historical
    collector. Nineteen seconds of worker occupancy is a planning assumption, not
    a measured full-panel mean. The six-worker default allows five extra minutes
    for preparation. No timing/calibration pass runs as part of this calculation.
    """
    observations = panel_size * len(levels) * seed_count * 2
    occupancy_seconds = 19
    preparation_minutes = 5
    estimated = (
        preparation_minutes + observations * occupancy_seconds / workers / 60
        if trials == 20 else None
    )
    return {
        "target_minutes": 90,
        "estimated_wall_minutes": estimated,
        "estimated_serial_minutes": None,
        "expected_observations": observations,
        "reference": {
            "date": "2026-11-07",
            "machine": "local Apple M1 Max",
            "serial_qft_pytest_seconds": [6.92, 7.09],
            "serial_qft_trials": 20,
            "serial_qft_process_seconds_approx": [8, 8],
            "three_worker_pytest_seconds": [14.20, 16.15, 17.18],
            "three_worker_trials": 1,
            "three_worker_inputs": ["HamLib 8-qubit", "circSU2 89-qubit", "QV 100-qubit"],
            "evidence": (
                "tmp/benchpress_worker_integration/logs and "
                "tmp/benchpress_worker_extra_nodes/sample-*.log"
            ),
        },
        "historical_reference": dict(HISTORICAL_REFERENCE),
        "method": "preparation allowance + observations × assumed worker occupancy / workers",
        "assumptions": {
            "seed_count": seed_count,
            "workers": workers,
            "optimization_levels": list(levels),
            "trials": trials,
            "both_arms": True,
            "release_builds": True,
            "one_native_thread_per_worker": True,
            "observation_worker_occupancy_seconds": occupancy_seconds,
            "preparation_minutes": preparation_minutes,
            "full_panel_measured": False,
        },
        "qualification": (
            "Provisional planning estimate, not a full-panel measurement. Current-worker "
            "smokes inform an assumed 19 seconds of worker occupancy; the smoke sample is "
            "too small to establish the corpus mean or six-worker scaling. Allow roughly "
            "60–120 minutes for the six-worker default, with possible outliers; this is a "
            "planning allowance, not a statistical interval. Preparation, host load, caches "
            "and compilation complexity vary. Other worker counts use the same unverified "
            "occupancy assumption; changing trials removes the estimate. The historical "
            "collector's faster throughput is context only. No runtime metric, runtime "
            "comparison, calibration, or 90-minute stop is performed during collection."
        ),
    }


def _family(row):
    if "hamlib" in row["suite"]:
        return "hamlib/" + row["hamiltonian"]["ham_category"]
    return row["suite"]


def _input_key(row):
    # Device/abstract HamLib tests are two targets for the same logical input.
    # Preserve other suites separately, including identical QASM from different corpora.
    return _family(row), row["input_hash"]


def _exclusion(row, source, statement_counts):
    if not row["eligible"]:
        return row.get("exclusion_reason") or "input exceeds device capacity"
    if row["input_qubits"] > MAX_INPUT_QUBITS:
        return f"more than {MAX_INPUT_QUBITS} logical qubits"
    if "hamiltonian" in row:
        if row["hamiltonian"]["ham_terms"] > MAX_HAMILTONIAN_TERMS:
            return f"more than {MAX_HAMILTONIAN_TERMS} Hamiltonian terms"
    if "input_path" in row:
        relative = row["input_path"]
        if relative not in statement_counts:
            path = (source / relative).resolve()
            if not path.is_relative_to(source):
                raise ValueError(f"Benchpress input escapes source directory: {relative}")
            # A conservative, inexpensive source-size proxy. Custom gate-body
            # statements also count; this is not a claim about compiled gate count.
            statement_counts[relative] = path.read_text().count(";")
        if statement_counts[relative] > MAX_QASM_STATEMENTS:
            return f"more than {MAX_QASM_STATEMENTS} QASM statements"
    return None


def select_panel(inventory, source, *, panel_size=DEFAULT_PANEL_SIZE):
    """Return ``(selected_rows, planning_and_selection_metadata)``.

    ``inventory`` uses ``qtb.benchpress.panel.collected_entry``'s schema;
    ``source`` is the pinned Benchpress source root. All selection inputs are
    static. Missing files or duplicate node IDs are errors, never silent skips.
    """
    if isinstance(panel_size, bool) or not isinstance(panel_size, int) or panel_size < 1:
        raise ValueError("panel_size must be a positive integer")
    source = Path(source).resolve()
    if len({row["node_id"] for row in inventory}) != len(inventory):
        raise ValueError("Duplicate collected node IDs")
    statement_counts, groups, excluded = {}, defaultdict(list), []
    for row in sorted(inventory, key=lambda row: row["node_id"]):
        reason = _exclusion(row, source, statement_counts)
        if reason:
            excluded.append({"node_id": row["node_id"], "reason": reason})
        else:
            groups[_input_key(row)].append(row)
    if not groups:
        raise ValueError("No eligible Benchpress inputs remain after the static size limits")

    # Rotate the preferred topology between inputs so even the first coverage
    # round includes all abstract topologies. Keep each device corpus represented.
    families = defaultdict(list)
    for key, rows in groups.items():
        families[key[0]].append(rows)
    queues = {}
    for family, inputs in sorted(families.items()):
        inputs.sort(key=lambda rows: (rows[0]["input_qubits"], rows[0]["input_name"],
                                     rows[0]["input_hash"]))
        queues[family] = []
        for index, rows in enumerate(inputs):
            rotated = TOPOLOGIES[index % len(TOPOLOGIES):] + TOPOLOGIES[:index % len(TOPOLOGIES)]
            preference = {name: rank for rank, name in enumerate(("fake_torino", *rotated))}
            queues[family].append(deque(sorted(
                rows, key=lambda row: (preference.get(row["topology"], 99), row["node_id"])
            )))

    selected = []
    # One target per input per round, interleaving suites/categories. Thus a
    # reduced panel also samples several families instead of an alphabetical prefix.
    while len(selected) < panel_size:
        round_queues = {family: deque(queue for queue in inputs if queue)
                        for family, inputs in queues.items()}
        if not any(round_queues.values()):
            break
        while any(round_queues.values()) and len(selected) < panel_size:
            for family in sorted(round_queues):
                if round_queues[family] and len(selected) < panel_size:
                    row = dict(round_queues[family].popleft().popleft())
                    row["selection_reason"] = (
                        "breadth-first bounded corpus; every logical input before repeated "
                        "targets, with rotating abstract topology coverage"
                    )
                    if "input_path" in row:
                        row["input_qasm_statements"] = statement_counts[row["input_path"]]
                    selected.append(row)

    selected_ids = {row["node_id"] for row in selected}
    for rows in groups.values():
        excluded.extend({"node_id": row["node_id"], "reason": "fixed panel-size budget"}
                        for row in rows if row["node_id"] not in selected_ids)
    metadata = {
        **estimate_budget(len(selected)),
        "requested_panel_size": panel_size,
        "selected_panel_size": len(selected),
        "inventory_size": len(inventory),
        "bounded_eligible_nodes": sum(len(rows) for rows in groups.values()),
        "bounded_logical_inputs": len(groups),
        "selected_logical_inputs": len({_input_key(row) for row in selected}),
        "limits": {"max_input_qubits": MAX_INPUT_QUBITS,
                   "max_qasm_statements": MAX_QASM_STATEMENTS,
                   "max_hamiltonian_terms": MAX_HAMILTONIAN_TERMS},
        "suite_counts": dict(sorted(Counter(row["suite"] for row in selected).items())),
        "topology_counts": dict(sorted(Counter(row["topology"] for row in selected).items())),
        "family_counts": dict(sorted(Counter(_family(row) for row in selected).items())),
        "exclusion_counts": dict(sorted(Counter(row["reason"] for row in excluded).items())),
        "excluded": sorted(excluded, key=lambda row: row["node_id"]),
    }
    return sorted(selected, key=lambda row: row["node_id"]), metadata
