"""Deterministic, capacity-matched targets for the balanced comparison.

Qiskit imports are deliberately inside constructors so the worker can select its
frozen source checkout before importing either Qiskit build. All generated edge
lists are deterministic. Real IBM targets retain their full frozen calibration
and directed instruction support when exported through ``qtb_worker.adapter``.
"""

from __future__ import annotations

from collections import deque
from functools import cache
from math import isqrt

WIDTHS = (8, 16, 32, 64, 100)
TOPOLOGIES = ("linear", "grid", "heavy_hex_patch")
PATCH_VERSION = "connected-degree-distance-prune-v1"


def grid_shape(width):
    """Return the most square integer rectangle with exactly ``width`` sites."""
    if type(width) is not int or width < 1:
        raise ValueError("Grid width must be a positive integer")
    rows = isqrt(width)
    while width % rows:
        rows -= 1
    return rows, width // rows


def synthetic_spec(topology, logical_qubits, *, occupancy="half"):
    """Describe one exact-width target, including separately scored controls."""
    if topology not in (*TOPOLOGIES, "all_to_all"):
        raise ValueError(f"Unknown synthetic topology: {topology}")
    if type(logical_qubits) is not int or logical_qubits < 2:
        raise ValueError("Logical width must be an integer of at least two")
    if occupancy not in ("half", "full"):
        raise ValueError("Occupancy must be half or full")
    physical = logical_qubits * (2 if occupancy == "half" else 1)
    if topology == "linear":
        identifier = f"line_{physical}"
    elif topology == "grid":
        rows, columns = grid_shape(physical)
        identifier = f"grid_{rows}x{columns}"
    elif topology == "heavy_hex_patch":
        identifier = f"hh_patch_{physical}"
    else:
        identifier = f"all_to_all_{physical}"
    if occupancy == "full":
        identifier += "_near"
    panel = (
        "all_to_all" if topology == "all_to_all"
        else "near_capacity" if occupancy == "full"
        else "general"
    )
    result = {
        "target_id": identifier,
        "panel": panel,
        "topology": topology,
        "logical_qubits": logical_qubits,
        "physical_qubits": physical,
        "native_2q_gate": "cz",
        "native_two_qubit_gate": "cz",
        "native_2q_names": ["cz"],
        "single_qubit_basis": ["rz", "sx", "x"],
        "occupancy": occupancy,
        "instruction_properties": "absent_uniform",
    }
    if topology == "grid":
        result["grid_shape"] = list(grid_shape(physical))
    if topology == "heavy_hex_patch":
        result["construction_version"] = PATCH_VERSION
    return result


def specifications(*, include_supplemental=False):
    """List 18 core targets, optionally adding five controls and nine stress targets."""
    result = [synthetic_spec(topology, width) for width in WIDTHS for topology in TOPOLOGIES]
    for name, physical, gate, date in (
        ("FakeTorino", 133, "cz", "2025-02-26"),
        ("FakeSherbrooke", 127, "ecr", "2025-02-26"),
        ("FakeWashingtonV2", 127, "cx", "2022-04-12"),
    ):
        result.append({
            "target_id": name,
            "panel": "ibm",
            "topology": name,
            "logical_qubits": None,
            "physical_qubits": physical,
            "native_2q_gate": gate,
            "native_two_qubit_gate": gate,
            "native_2q_names": [gate],
            "backend_class": name,
            "property_snapshot_date": date,
            "historical_snapshot": name == "FakeWashingtonV2",
            "provider_distribution": "qiskit-ibm-runtime",
            "provider_version": "0.51.0",
        })
    if include_supplemental:
        result.extend(synthetic_spec("all_to_all", width) for width in WIDTHS)
        result.extend(
            synthetic_spec(topology, width, occupancy="full")
            for width in (32, 64, 100) for topology in TOPOLOGIES
        )
    return result


def _distances(adjacency, start):
    distance = {start: 0}
    pending = deque([start])
    while pending:
        vertex = pending.popleft()
        for neighbor in adjacency[vertex]:
            if neighbor not in distance:
                distance[neighbor] = distance[vertex] + 1
                pending.append(neighbor)
    return distance


def _connected_without(adjacency, excluded, center):
    reached, pending = {center}, [center]
    while pending:
        for neighbor in adjacency[pending.pop()]:
            if neighbor != excluded and neighbor not in reached:
                reached.add(neighbor)
                pending.append(neighbor)
    return len(reached) == len(adjacency) - 1


@cache
def heavy_hex_patch_edges(width):
    """Return undirected edges for the plan's connected, cycle-preserving patch."""
    from qiskit.transpiler import CouplingMap

    if type(width) is not int or width < 2:
        raise ValueError("Heavy-hex patch width must be an integer of at least two")
    distance = 3
    while (5 * distance * distance - 2 * distance - 1) // 2 < width:
        distance += 2
    parent = CouplingMap.from_heavy_hex(distance, bidirectional=False)
    adjacency = {q: set() for q in range(parent.size())}
    for first, second in parent.get_edges():
        adjacency[first].add(second)
        adjacency[second].add(first)
    center = min(adjacency, key=lambda q: (max(_distances(adjacency, q).values()), q))
    while len(adjacency) > width:
        distances = _distances(adjacency, center)
        candidates = sorted(
            (q for q in adjacency if q != center),
            key=lambda q: (len(adjacency[q]), -distances[q], -q),
        )
        removed = next(
            q for q in candidates if _connected_without(adjacency, q, center)
        )
        for neighbor in adjacency[removed]:
            adjacency[neighbor].remove(removed)
        del adjacency[removed]
    renumber = {q: i for i, q in enumerate(sorted(adjacency))}
    return tuple(sorted(
        (renumber[first], renumber[second])
        for first in adjacency for second in adjacency[first] if first < second
    ))


def undirected_edges(spec):
    """Return explicit deterministic connectivity for a synthetic specification."""
    width, topology = spec["physical_qubits"], spec["topology"]
    if type(width) is not int or width < 2:
        raise ValueError("Physical width must be an integer of at least two")
    if topology == "linear":
        return tuple((q, q + 1) for q in range(width - 1))
    if topology == "all_to_all":
        return tuple((a, b) for a in range(width) for b in range(a + 1, width))
    if topology == "heavy_hex_patch":
        if spec.get("construction_version", PATCH_VERSION) != PATCH_VERSION:
            raise ValueError("Unsupported heavy-hex patch construction version")
        return heavy_hex_patch_edges(width)
    if topology == "grid":
        rows, columns = spec.get("grid_shape", grid_shape(width))
        if rows * columns != width or min(rows, columns) < 1:
            raise ValueError("Grid shape does not match physical width")
        return tuple(sorted(
            [(r * columns + c, r * columns + c + 1)
             for r in range(rows) for c in range(columns - 1)]
            + [(r * columns + c, (r + 1) * columns + c)
               for r in range(rows - 1) for c in range(columns)]
        ))
    raise ValueError(f"Unknown synthetic topology: {topology}")


def build_target(spec):
    """Build an exact planned target, preserving IBM calibration and directions."""
    if spec["panel"] == "ibm":
        from importlib.metadata import version

        from qiskit_ibm_runtime import fake_provider

        if version("qiskit-ibm-runtime") != spec["provider_version"]:
            raise ValueError("IBM target provider differs from the pinned target recipe")
        backend_class = getattr(fake_provider, spec["backend_class"])
        target = backend_class().target
        native = {
            name for name in target.operation_names
            if not isinstance(target.operation_from_name(name), type)
            and target.operation_from_name(name).num_qubits == 2
        }
        if target.num_qubits != spec["physical_qubits"] or native != set(spec["native_2q_names"]):
            raise ValueError("IBM target capacity or native two-qubit gates changed")
        return target

    from qiskit.circuit import Parameter
    from qiskit.circuit.library import CZGate, RZGate, SXGate, XGate
    from qiskit.transpiler import Target

    if spec["native_2q_names"] != ["cz"]:
        raise ValueError("Balanced synthetic targets require CZ")
    width = spec["physical_qubits"]
    edges = undirected_edges(spec)
    target = Target(num_qubits=width)
    for gate in (RZGate(Parameter("theta")), SXGate(), XGate()):
        target.add_instruction(gate, properties={(q,): None for q in range(width)})
    target.add_instruction(CZGate(), properties={
        edge: None for a, b in edges for edge in ((a, b), (b, a))
    })
    return target
