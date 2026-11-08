"""Freeze and verify balanced inputs using the existing typed circuit adapter.

Qiskit imports are deferred until the worker has selected and verified its frozen
release build. Canonical JSON is shared between the arms; workers never regenerate
their own random inputs or backend calibrations during collection.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from compare_distributions.collect import digest, file_hash, write_json
from compare_to_baseline.collect import safe_path


def normalize_circuit(circuit):
    """Expand only definitions which have no supported typed serialization."""
    from qtb.errors import Unsupported
    from qtb_worker.adapter import operation_descriptor

    if circuit.parameters:
        raise ValueError("Balanced inputs must have all parameters bound before freezing")
    normalized = circuit
    for _ in range(32):
        custom = set()
        for instruction in normalized.data:
            try:
                operation_descriptor(instruction.operation, {}, opaque=False)
            except Unsupported:
                if instruction.operation.definition is None:
                    raise ValueError(
                        f"Cannot serialize input operation {instruction.operation.name}"
                    ) from None
                custom.add(instruction.operation.name)
        if not custom:
            return normalized
        normalized = normalized.decompose(gates_to_decompose=sorted(custom))
    raise ValueError("Balanced input definitions exceed the expansion limit")


def logical_identity(data):
    """Ignore register names, retaining exact operations, bit indices and phase."""
    normalized = dict(data, header=dict(data["header"]))
    normalized["header"].pop("qregs", None)
    normalized["header"].pop("cregs", None)
    return digest(normalized)


def _load_checked(root, relative, expected_hash):
    path = safe_path(root, relative)
    if file_hash(path) != expected_hash:
        raise ValueError(f"Frozen fixture changed: {relative}")
    return json.loads(path.read_text())


def prepare(root):
    """Create the full logical library and target snapshots once, in one arm."""
    from qtb_worker.adapter import export_circuit, export_target, import_circuit, import_target

    from . import balanced_circuits, balanced_targets

    root = Path(root)
    inputs, targets = [], []
    for spec in balanced_circuits.specifications():
        circuit = balanced_circuits.build_circuit(spec)
        generation = copy.deepcopy(circuit.metadata or {})
        balanced_circuits.audit_circuit(circuit, spec)
        normalized = normalize_circuit(circuit)
        data = export_circuit(normalized)
        restored = import_circuit(data)
        restored.metadata = copy.deepcopy(generation)
        if export_circuit(restored) != data:
            raise ValueError(f"Circuit serialization roundtrip differs: {spec['input_id']}")
        audit = balanced_circuits.audit_circuit(restored, spec)
        relative = (
            f"fixtures/circuits/{spec['family_id']}/"
            f"{spec['template_id']}_n{spec['logical_qubits']}.json"
        )
        write_json(root / relative, data)
        inputs.append(
            dict(
                spec,
                input_name=f"{spec['template_id']}_n{spec['logical_qubits']}",
                input_file=relative,
                input_hash=file_hash(root / relative),
                input_identity=logical_identity(data),
                input_qubits=restored.num_qubits,
                generation=generation,
                audit=audit,
            )
        )
        print(f"Prepared input {spec['input_id']}", flush=True)
    for spec in balanced_targets.specifications(include_supplemental=True):
        target = balanced_targets.build_target(spec)
        native_names = spec.get("native_2q_names") or [spec["native_two_qubit_gate"]]
        data = export_target(target, native_names)
        restored = import_target(data)
        if export_target(restored, native_names) != data:
            raise ValueError(f"Target serialization roundtrip differs: {spec['target_id']}")
        relative = f"fixtures/targets/{spec['target_id']}.json"
        write_json(root / relative, data)
        targets.append(
            dict(
                spec,
                target_file=relative,
                target_hash=file_hash(root / relative),
                native_2q_names=list(native_names),
                physical_qubits=restored.num_qubits,
            )
        )
        print(f"Prepared target {spec['target_id']}", flush=True)
    return {
        "inputs": inputs,
        "targets": targets,
        "validation": {
            "status": "verified",
            "circuit_roundtrips": len(inputs),
            "target_roundtrips": len(targets),
            "scope": "Typed serialization, active widths, and frozen artifact identities",
        },
    }


def check(root, inventory):
    """Verify every frozen fixture can be loaded identically by the other arm."""
    from qtb_worker.adapter import export_circuit, export_target, import_circuit, import_target

    from .balanced_circuits import audit_circuit

    for item in inventory["inputs"]:
        data = _load_checked(root, item["input_file"], item["input_hash"])
        circuit = import_circuit(data)
        circuit.metadata = copy.deepcopy(item["generation"])
        exported = export_circuit(circuit)
        if exported != data or logical_identity(exported) != item["input_identity"]:
            raise ValueError(f"Frozen input import differs: {item['input_id']}")
        if circuit.num_qubits != item["input_qubits"]:
            raise ValueError(f"Frozen input width differs: {item['input_id']}")
        if audit_circuit(circuit, item) != item["audit"]:
            raise ValueError(f"Frozen active-input audit differs: {item['input_id']}")
    for item in inventory["targets"]:
        data = _load_checked(root, item["target_file"], item["target_hash"])
        target = import_target(data)
        if export_target(target, item["native_2q_names"]) != data:
            raise ValueError(f"Frozen target import differs: {item['target_id']}")
        if target.num_qubits != item["physical_qubits"]:
            raise ValueError(f"Frozen target width differs: {item['target_id']}")
    return inventory


def load_context(root, case):
    """Verify the exact input and target before one checked compilation."""
    from qtb_worker.adapter import export_circuit, export_target, import_circuit, import_target

    circuit_data = _load_checked(root, case["input_file"], case["input_hash"])
    target_data = _load_checked(root, case["target_file"], case["target_hash"])
    circuit = import_circuit(circuit_data)
    circuit.metadata = copy.deepcopy(case.get("generation", {}))
    identity = logical_identity(export_circuit(circuit))
    if identity != case["input_identity"]:
        raise ValueError("Actual logical input differs from its frozen identity")
    if circuit.num_qubits != case["input_qubits"]:
        raise ValueError("Actual logical width differs from its frozen case")
    target = import_target(target_data)
    native_names = case["native_2q_names"]
    if export_target(target, native_names) != target_data:
        raise ValueError("Actual target differs from its frozen snapshot")
    if circuit.num_qubits > target.num_qubits:
        raise ValueError("Logical circuit does not fit the frozen target")
    return {
        "circuit": circuit,
        "target": target,
        "target_data": target_data,
        "input_identity": identity,
    }
