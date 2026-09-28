# Draft: Qiskit issue on VF2PostLayout reproducibility (plan P7, work item 14)

**Status:** draft, not filed. Filing publishes it on GitHub; the maintainer files it.

---

**Title:** `VF2PostLayout` picks different layouts for a fixed `seed_transpiler` on a target without error rates

### Environment

- Qiskit 2.5.2 (released wheel), Python 3.11, macOS (arm64). Also seen on a Linux source
  build of the development branch.
- `PYTHONHASHSEED` fixed or not: no difference. Fresh processes and repeated calls in one
  process both differ.

### What happens

Transpiling the same circuit to the same `Target` with the same `seed_transpiler` at
optimization level 1 or 2 gives different `initial_layout`s from run to run. The first pass
whose result differs is `VF2PostLayout` (its `post_layout`); every pass before it, `SabreLayout`
and `SabreSwap` included, is identical. The target has no error rates, so every candidate
layout scores the same and the choice among the ties is not reproducible.

Usually only idle (ancilla) qubits move. On some seeds a used qubit moves too, and then the
output circuit itself differs, by a relabelling of physical qubits. The metrics (two-qubit
depth and count) are the same. Level 0, which has no `VF2PostLayout`, is reproducible, and so
is a target with error rates.

### Reproduction

```python
import itertools

from qiskit import QuantumCircuit, transpile
from qiskit.transpiler import CouplingMap, Target

# IBM Rochester (53 qubits), both directions of each edge; no error rates.
EDGES = [(0, 1), (0, 5), (1, 2), (2, 3), (3, 4), (4, 6), (5, 9), (6, 13), (7, 8), (7, 16),
         (8, 9), (9, 10), (10, 11), (11, 12), (11, 17), (12, 13), (13, 14), (14, 15), (15, 18),
         (16, 19), (17, 23), (18, 27), (19, 20), (20, 21), (21, 22), (21, 28), (22, 23),
         (23, 24), (24, 25), (25, 26), (25, 29), (26, 27), (28, 32), (29, 36), (30, 31),
         (30, 39), (31, 32), (32, 33), (33, 34), (34, 35), (34, 40), (35, 36), (36, 37),
         (37, 38), (38, 41), (39, 42), (40, 46), (41, 50), (42, 43), (43, 44), (44, 45),
         (44, 51), (45, 46), (46, 47), (47, 48), (48, 49), (48, 52), (49, 50)]
coupling = CouplingMap([*EDGES, *((b, a) for a, b in EDGES)])
target = Target.from_configuration(["cx", "rz", "sx", "x", "measure"], 53, coupling)

n = 32
qc = QuantumCircuit(n)
qc.h(range(n))
for i, j in itertools.combinations(range(n), 2):
    qc.rzz(0.1 * (i + j), i, j)
qc.rx(0.3, range(n))

for seed in range(6):
    layouts = {
        tuple(
            transpile(qc, target=target, optimization_level=2, seed_transpiler=seed)
            .layout.initial_index_layout(filter_ancillas=False)
        )
        for _ in range(3)
    }
    print(seed, len(layouts))  # 1 expected; 2.5.2 prints 3 for every seed
```

### The same in `VF2Layout`

`VF2Layout` shows the same behaviour where it finds a perfect layout and the circuit leaves
qubits idle. A 20-qubit QUEKO circuit that uses 9 of its qubits (`queko_bigd_20`), compiled
to a 20-qubit Tokyo coupling map without error rates at levels 1 to 3: 58 of 60 recompiles
gave another `initial_layout` than the first compile of the same seed. The first pass whose
result differs is `VF2Layout`. Only the idle qubits are placed differently, so the output
circuit is the same; its layout is not. (Not yet reduced to a self-contained script like the
one above.)

### Expected

A fixed `seed_transpiler` gives the same output on every run, as `SabreLayout` and
`SabreSwap` do. Ties among equal-score layouts should be broken deterministically (for
example by candidate order, or by the seed).

### Why it matters

Benchmarks and regression tests that compare compiled outputs, or that replay a compile
from a truncated pipeline, see differences that no code change caused. In our harness this
made an A/A comparison of an unmodified build fail its routing-replay check and its
determinism audit. We now compare snapshots from a single compile and accept only
differences proven to be a physical-qubit relabelling introduced at `VF2PostLayout` or
`VF2Layout`.
