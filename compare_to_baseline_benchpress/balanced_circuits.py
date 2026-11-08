"""Deterministic, exact-width logical inputs for the balanced scaling panels.

Qiskit is imported only inside functions, after the worker selects its frozen
build. Input generation is separate from transpiler randomness. Each recipe is
serialized once and the resulting input is reused by both arms and all targets.
"""

from __future__ import annotations

import copy
import hashlib
import math

WIDTHS = (8, 16, 32, 64, 100)
GENERATOR_VERSION = "balanced-v1"
RECTANGLES = {8: (2, 4), 16: (4, 4), 32: (4, 8), 64: (8, 8), 100: (10, 10)}
FAMILIES = (
    ("F1", "Fourier and phase estimation", (
        ("qft_full", "Full QFT"),
        ("qft_truncated_d3", "Truncated QFT, rotation distance <= 3"),
        ("phase_estimation", "Phase estimation"),
    )),
    ("F2", "Register arithmetic", (
        ("modular_adder", "Modular ripple adder"),
        ("cumulative_multiplier", "Cumulative multiplier"),
        ("qft_adder", "QFT adder"),
    )),
    ("F3", "Boolean and reversible oracles", (
        ("mcx_clean", "MCX with one clean workspace qubit"),
        ("threshold_comparator", "Integer threshold comparator"),
        ("toffoli_network", "Seeded Toffoli network"),
    )),
    ("F4", "Variational ansatz circuits", (
        ("su2_linear", "SU2 linear"),
        ("su2_circular", "SU2 circular"),
        ("su2_full", "SU2 full entanglement"),
    )),
    ("F5", "Combinatorial optimization", (
        ("qaoa_regular3", "QAOA connected 3-regular MaxCut"),
        ("qaoa_ba2", "QAOA BA MaxCut"),
        ("qaoa_complete", "QAOA complete MaxCut"),
    )),
    ("F6", "Spin-model simulation", (
        ("heisenberg_chain", "Heisenberg chain"),
        ("heisenberg_grid", "Heisenberg rectangle"),
        ("tfim_grid", "Transverse-field Ising rectangle"),
    )),
    ("F7", "Random circuits", (
        ("quantum_volume", "Quantum volume"),
        ("su4_shallow", "Shallow random SU(4) pairing layers"),
        ("clifford_layers", "Random Clifford layers"),
    )),
    ("F8", "Structured state preparation and fanout", (
        ("bv_all_ones", "BV all-ones"),
        ("ghz_tree", "Binary-tree GHZ"),
        ("w_ladder", "W-state ladder"),
    )),
)


def generation_seed(template, width):
    text = f"{GENERATOR_VERSION}/input/{template}/{width}/20261108".encode()
    return int.from_bytes(hashlib.sha256(text).digest()[:8], "big")


def specifications():
    """Return the complete 8-family x 5-width x 3-template frozen design."""
    return [
        {
            "input_id": f"{GENERATOR_VERSION}/{family_id}/{template}_n{n}",
            "family_id": family_id,
            "family": family,
            "template_id": template,
            "logical_qubits": n,
            "label": label,
            "recipe": {
                "generator_version": GENERATOR_VERSION,
                "generation_seed": generation_seed(template, n),
                "repetitions": 3 if family_id in ("F4", "F5", "F6") else None,
                "qpe_phase": [173, 104729] if template == "phase_estimation" else None,
            },
        }
        for family_id, family, templates in FAMILIES
        for n in WIDTHS
        for template, label in templates
    ]


def _rng(template, n):
    import numpy as np

    return np.random.default_rng(generation_seed(template, n))


def _prepare(circuit, wires, rng):
    """Use nontrivial data states without disturbing required clean workspace."""
    for wire in wires:
        circuit.ry(float(rng.uniform(0.23, 2.73)), int(wire))
        circuit.rz(float(rng.uniform(-2.43, 2.43)), int(wire))


def _qft(n, max_distance=None):
    from qiskit import QuantumCircuit

    circuit = QuantumCircuit(n)
    for j in reversed(range(n)):
        circuit.h(j)
        for k in reversed(range(j)):
            if max_distance is None or j - k <= max_distance:
                circuit.cp(math.pi / 2 ** (j - k), j, k)
    for j in range(n // 2):
        circuit.swap(j, n - j - 1)
    return circuit


def _connected(n, edges):
    neighbors = [set() for _ in range(n)]
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    seen, stack = {0}, [0]
    while stack:
        for vertex in neighbors[stack.pop()] - seen:
            seen.add(vertex)
            stack.append(vertex)
    return len(seen) == n


def _regular_graph(n, rng):
    # Configuration-model rejection is deterministic for this recipe seed.
    # Reject loops, duplicate edges, and disconnected graphs before freezing.
    for _ in range(100_000):
        stubs = rng.permutation([vertex for vertex in range(n) for _ in range(3)])
        edges = {tuple(sorted(map(int, pair))) for pair in stubs.reshape(-1, 2)}
        if len(edges) == 3 * n // 2 and all(a != b for a, b in edges):
            if _connected(n, edges):
                return sorted(edges)
    raise RuntimeError("Failed deterministic connected 3-regular graph generation")


def _ba_graph(n, rng):
    # Start with K3, then add each vertex with two distinct degree-weighted links.
    edges = {(0, 1), (0, 2), (1, 2)}
    degrees = [2, 2, 2]
    for vertex in range(3, n):
        import numpy as np

        probabilities = np.asarray(degrees, dtype=float)
        probabilities /= probabilities.sum()
        targets = rng.choice(vertex, 2, replace=False, p=probabilities)
        for target in map(int, targets):
            edges.add((target, vertex))
            degrees[target] += 1
        degrees.append(2)
    return sorted(edges)


def _grid_edges(n):
    rows, columns = RECTANGLES[n]
    edges = []
    for row in range(rows):
        for column in range(columns):
            index = row * columns + column
            if column + 1 < columns:
                edges.append((index, index + 1))
            if row + 1 < rows:
                edges.append((index, index + columns))
    return sorted(edges)


def _haar_su4(rng):
    import numpy as np

    matrix = rng.normal(size=(4, 4)) + 1j * rng.normal(size=(4, 4))
    unitary, triangular = np.linalg.qr(matrix)
    diagonal = np.diag(triangular)
    unitary *= diagonal / np.abs(diagonal)
    return unitary * np.exp(-0.25j * np.angle(np.linalg.det(unitary)))


def _concrete_multiplier(width):
    """Resolve H18's annotated controlled adders once during input construction."""
    from qiskit import QuantumCircuit
    from qiskit.circuit import AnnotatedOperation, ControlModifier
    from qiskit.synthesis import multiplier_cumulative_h18

    source = multiplier_cumulative_h18(width)
    result = QuantumCircuit(source.num_qubits)
    result.global_phase = source.global_phase
    for instruction in source.data:
        op = instruction.operation
        wires = [source.find_bit(q).index for q in instruction.qubits]
        if not isinstance(op, AnnotatedOperation):
            result.append(op, wires)
            continue
        if len(op.modifiers) != 1 or not isinstance(op.modifiers[0], ControlModifier):
            raise ValueError("Unexpected cumulative-multiplier annotation")
        modifier = op.modifiers[0]
        definition = op.base_op.definition
        if definition is None or float(definition.global_phase) != 0:
            raise ValueError("Require an explicit zero-global-phase adder definition")
        controls = wires[:modifier.num_ctrl_qubits]
        operands = wires[modifier.num_ctrl_qubits:]
        for item in definition.data:
            controlled = item.operation.control(
                modifier.num_ctrl_qubits, ctrl_state=modifier.ctrl_state, annotated=False
            )
            result.append(controlled, controls + [
                operands[definition.find_bit(q).index] for q in item.qubits
            ])
    return result


def _spin(circuit, template, n):
    edges = [(i, i + 1) for i in range(n - 1)] if template.endswith("chain") else _grid_edges(n)
    if template == "tfim_grid":
        for q in range(n):
            circuit.ry(math.pi / 5, q)
        terms = [("zz", a, b, -1.0) for a, b in edges]
        terms += [("x", q, None, -0.7) for q in range(n)]
    else:
        for q in range(1, n, 2):
            circuit.x(q)
        terms = [(axis, a, b, 1.0) for axis in ("xx", "yy", "zz") for a, b in edges]
    # exp(-i dt H) second-order symmetric product, three steps. Half-step
    # rotations have angle dt*coefficient, since RPP(theta)=exp(-i theta PP/2).
    for _ in range(3):
        for axis, a, b, coefficient in terms + terms[::-1]:
            angle = (0.4 / 3) * coefficient
            if axis == "x":
                circuit.rx(angle, a)
            else:
                getattr(circuit, "r" + axis)(angle, a, b)
    return edges


def build_circuit(spec):
    """Construct one exact-width bound circuit; never run a transpiler here."""
    import numpy as np
    from qiskit import QuantumCircuit

    template, n = spec["template_id"], spec["logical_qubits"]
    valid_templates = {t for _, _, variants in FAMILIES for t, _ in variants}
    if n not in WIDTHS or template not in valid_templates:
        raise ValueError(f"Unknown balanced circuit: {template}, width={n}")
    rng = _rng(template, n)
    circuit = QuantumCircuit(n, name=f"{template}_n{n}")
    metadata = {
        "generator_version": GENERATOR_VERSION,
        "generation_seed": generation_seed(template, n),
        "data_qubits": n,
        "work_ancillas": 0,
        "output_qubits": 0,
    }
    if template in ("qft_full", "qft_truncated_d3"):
        _prepare(circuit, range(n), rng)
        circuit.compose(_qft(n, 3 if template == "qft_truncated_d3" else None), inplace=True)
    elif template == "phase_estimation":
        circuit.h(range(n))
        target = n - 1
        for k in range(n - 1):
            phase = 2 * math.pi * ((173 * pow(2, k, 104729)) % 104729) / 104729
            # U^(2^k) = V diag(1, exp(i phase)) V-dagger. Keeping Phase,
            # rather than RZ alone, preserves the controlled global phase.
            circuit.ry(-math.pi / 7, target)
            circuit.cp(phase, k, target)
            circuit.ry(math.pi / 7, target)
        circuit.compose(_qft(n - 1).inverse(), qubits=list(range(n - 1)), inplace=True)
        metadata.update(data_qubits=1, output_qubits=n - 1)
    elif template in ("modular_adder", "cumulative_multiplier", "qft_adder"):
        from qiskit.synthesis import (
            adder_modular_v17,
            adder_qft_d00,
        )

        if template == "modular_adder":
            arithmetic = adder_modular_v17(n // 2)
        elif template == "qft_adder":
            arithmetic = adder_qft_d00(n // 2, kind="fixed")
        else:
            arithmetic = _concrete_multiplier(n // 4)
            metadata.update(data_qubits=n // 2, output_qubits=n // 2)
        _prepare(circuit, range(metadata["data_qubits"]), rng)
        circuit.compose(arithmetic, inplace=True)
    elif template == "mcx_clean":
        from qiskit.synthesis import synth_mcx_1_clean_kg24

        metadata.update(data_qubits=n - 1, work_ancillas=1)
        _prepare(circuit, range(n - 1), rng)
        circuit.compose(synth_mcx_1_clean_kg24(n - 2), inplace=True)
    elif template == "threshold_comparator":
        from qiskit.synthesis import synth_integer_comparator_2s

        metadata.update(data_qubits=n // 2, output_qubits=1, work_ancillas=n // 2 - 1)
        _prepare(circuit, range(n // 2), rng)
        circuit.compose(synth_integer_comparator_2s(n // 2, 2 ** (n // 2 - 1) + 1), inplace=True)
    elif template == "toffoli_network":
        _prepare(circuit, range(n), rng)
        used = set()
        for _ in range(n):
            order = list(map(int, rng.permutation(n)))
            for start in range(0, n - 2, 3):
                wires = order[start:start + 3]
                circuit.ccx(*wires)
                used.update(wires)
        # Coverage is input construction, independent of either compiler's output.
        for q in sorted(set(range(n)) - used):
            circuit.ccx((q + 1) % n, (q + 2) % n, q)
    elif template.startswith("su2_"):
        from qiskit.circuit.library import efficient_su2

        ansatz = efficient_su2(n, su2_gates=["ry", "rz"], reps=3,
                              entanglement=template.removeprefix("su2_"))
        ansatz.assign_parameters(rng.uniform(-2.8, 2.8, ansatz.num_parameters), inplace=True)
        circuit.compose(ansatz, inplace=True)
    elif template.startswith("qaoa_"):
        if template == "qaoa_regular3":
            edges = _regular_graph(n, rng)
        elif template == "qaoa_ba2":
            edges = _ba_graph(n, rng)
        else:
            edges = [(a, b) for a in range(n) for b in range(a + 1, n)]
        circuit.h(range(n))
        for gamma, beta in zip((0.31, 0.47, 0.23), (0.19, 0.29, 0.41), strict=True):
            for a, b in edges:
                circuit.rzz(-gamma, a, b)
            for q in range(n):
                circuit.rx(2 * beta, q)
        metadata["graph_edges"] = [list(edge) for edge in edges]
    elif template.startswith("heisenberg_") or template == "tfim_grid":
        metadata["graph_edges"] = [list(edge) for edge in _spin(circuit, template, n)]
    elif template in ("quantum_volume", "su4_shallow"):
        for _ in range(n if template == "quantum_volume" else n // 4):
            pairs = rng.permutation(n).reshape(-1, 2)
            for a, b in pairs:
                circuit.unitary(_haar_su4(rng), [int(a), int(b)])
    elif template == "clifford_layers":
        from qiskit.quantum_info import random_clifford

        for _ in range(n):
            for q in range(n):
                local = random_clifford(1, seed=int(rng.integers(2**32))).to_circuit()
                circuit.compose(local, qubits=[q], inplace=True)
            for a, b in rng.permutation(n).reshape(-1, 2):
                pair = random_clifford(2, seed=int(rng.integers(2**32))).to_circuit()
                circuit.compose(pair, qubits=[int(a), int(b)], inplace=True)
    elif template == "bv_all_ones":
        metadata.update(data_qubits=n - 1, output_qubits=1)
        circuit.x(n - 1)
        circuit.h(range(n))
        for q in range(n - 1):
            circuit.cx(q, n - 1)
        circuit.h(range(n - 1))
    elif template == "ghz_tree":
        circuit.h(0)
        for q in range(1, n):
            circuit.cx((q - 1) // 2, q)
    elif template == "w_ladder":
        circuit.x(0)
        for q in range(n - 1):
            circuit.cry(2 * math.acos(1 / math.sqrt(n - q)), q, q + 1)
            circuit.cx(q + 1, q)
    else:  # pragma: no cover - template membership checked above
        raise AssertionError(template)
    circuit.metadata = metadata
    if circuit.num_parameters:
        raise ValueError(f"Unbound circuit parameters: {template}")
    if not np.isfinite(float(circuit.global_phase)):
        raise ValueError("Nonfinite input global phase")
    return circuit


def audit_circuit(circuit, spec):
    """Validate meaningful width, recursively inspecting custom definitions."""
    from qiskit.circuit.library import UnitaryGate, get_standard_gate_name_mapping

    standard = set(get_standard_gate_name_mapping())
    active, interacting = set(), set()
    operation_count = multi_count = 0

    def inspect(block, mapping, depth=0):
        nonlocal operation_count, multi_count
        if depth > 32:
            raise ValueError("Input definitions exceed audit recursion limit")
        for instruction in block.data:
            op = instruction.operation
            wires = [mapping[block.find_bit(q).index] for q in instruction.qubits]
            if op.name in {"barrier", "delay", "id"}:
                continue
            if op.name not in standard and not isinstance(op, UnitaryGate):
                if op.definition is None:
                    raise ValueError(f"Cannot audit custom input operation {op.name}")
                inspect(op.definition, wires, depth + 1)
                continue
            operation_count += 1
            active.update(wires)
            if len(wires) >= 2:
                interacting.update(wires)
                multi_count += 1

    inspect(circuit, list(range(circuit.num_qubits)))
    n = spec["logical_qubits"]
    if circuit.num_qubits != n or len(active) != n or len(interacting) != n:
        raise ValueError(
            f"Input width mismatch for {spec['input_id']}: declared={circuit.num_qubits}, "
            f"active={len(active)}, interacting={len(interacting)}, expected={n}"
        )
    metadata = copy.deepcopy(circuit.metadata or {})
    if sum(metadata.get(key, 0) for key in ("data_qubits", "work_ancillas", "output_qubits")) != n:
        raise ValueError("Input register roles do not sum to meaningful logical width")
    return {
        "declared_qubits": circuit.num_qubits,
        "active_qubits": len(active),
        "two_qubit_active_qubits": len(interacting),
        "data_qubits": metadata["data_qubits"],
        "work_ancillas": metadata["work_ancillas"],
        "output_qubits": metadata["output_qubits"],
        "operation_count": operation_count,
        "multi_qubit_operation_count": multi_count,
    }
