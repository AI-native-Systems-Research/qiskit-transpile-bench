"""Pinned upstream identities and serialization shared with isolated Benchpress children."""

import math

from qtb.canonical import atomic_bytes, digest, file_hash, read_json, write_json
from qtb.coordinator.storage import read_records

BENCHPRESS = "b695f30e83a32bac05b9b4d8e98d37ba9aae5236"
BENCHMARK = "e33f5934cb462e719b13b8d352ced107f575b4f7"
SCHEMA = 1
LEVELS = (2, 3)
SEEDS = (0, 1, 2)
BACKEND_SEED = 424242
RUNTIME_METHOD = "perf_counter"
RUNTIME_SCOPE = "benchmark_callable"
QUALITY_FIELDS = (
    "input_num_qubits",
    "output_num_qubits",
    "output_gate_count_2q",
    "output_depth_2q",
    "output_circuit_operations",
    "native_2q_gate",
)


def valid_runtime(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def atomic_text(path, value):
    atomic_bytes(path, value.encode())


atomic_json = write_json
read_jsonl = read_records

# Imported by the reference adapters as well as this module's users.
__all__ = [
    "BACKEND_SEED",
    "BENCHMARK",
    "BENCHPRESS",
    "LEVELS",
    "QUALITY_FIELDS",
    "RUNTIME_METHOD",
    "RUNTIME_SCOPE",
    "SCHEMA",
    "SEEDS",
    "atomic_json",
    "atomic_text",
    "digest",
    "file_hash",
    "read_json",
    "read_jsonl",
    "valid_runtime",
]
