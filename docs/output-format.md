# Output formats

[Documentation index](README.md)

Open the session's `report.md`, then `decision.json`. Before deciding, use
`stages/quality/report.md` and `stages/benchpress/report.md` for immediate comparisons.

| Artifact | Contents |
| --- | --- |
| `run.json` | Sources, build identities, archived harness/profile hashes, store and provenance |
| `manifest.json`, `policy.json` | Frozen session workload and acceptance policy |
| `stages/<stage>/state.json` | Commit status, invocation, host, inputs, attempts, duration and gate |
| `stages/<stage>/evidence.json` | Stage-owned constraint/completeness records |
| `stages/<stage>/report.md`, `report.json` | Human-readable and structured stage reports |
| `observations.jsonl` | Quality B0 observations, checks, D2/N2 and fresh runtime data |
| `stages/quality/runtime/*.json` | Session-local paired timing checkpoints with build/case identities |
| `stages/benchpress/ready.json` | Pinned sources, preparation provenance and artifact hashes |
| `stages/benchpress/panel.json` | Fifty frozen circuit/target entries |
| `stages/benchpress/pairs/` | Paired revision/level/seed checkpoints |
| `stages/benchpress/raw/` | Per-invocation logs, pytest outputs and measurements |
| `stages/benchpress/summary.json` | Per-case/per-level D2/N2/runtime differences and ratios |
| `decision.json`, `evidence.json`, `report.md` | Merged verdict and committed stage detail |
| `clean.json` | Cleanup plan, retained provenance and completion |

Current stages are compile, quality, benchpress and optional unit-tests. Required stages
are compile, quality and benchpress; unit-tests counts once started. Removed correctness
and cost records are excluded from current decisions.

Quality observations have `runtime_ns` for a valid measurement and `runtime_measurement`
for the raw timing worker result. Cached quality metadata (`cached_from`) applies to circuit
quality, not runtime; both revisions are timed freshly within the session.

Quality and decision reports contain `runtime_comparison`: expected/measured pairs,
missing/failed samples, per-case baseline/evolved mean seconds, `difference_seconds`
(evolved minus baseline), `ratio` (evolved/baseline), and `improvement_percent`
(positive means faster). Complete panels also expose total workload seconds and the
geometric mean of case ratios. Incomplete measurements are unavailable, never zero.

The final report's `test_reports` contains benchpress and unit-tests. Benchpress uses
`change` and `change_pct` (evolved minus baseline; negative means better), plus ratios.
Its runtime measures the benchmark callable (`pm.run`), while quality measures end-to-end
`transpile`; report scopes identify this difference.

Records retain format tags `qtb-run/3`, `qtb-stage/2`, `qtb-observation/1`,
`qtb-quality-report/1`, `qtb-benchpress-report/1` and `qtb-decision/1`.
Schema definitions live in `src/qtb/config/schemas/`. Historical formats and helper readers
remain available, but older sessions should use their archived harness for decisions.

## Canonical circuit files (`*.ops.jsonl.gz`)

Inputs, outputs and references use one format (`qtb-circuit/1`): gzip-compressed JSON Lines, a
header line, then one operation per line in circuit order:

```text
{"format":"qtb-circuit/1","num_qubits":193,"num_clbits":0,"qregs":[["q",193]],"cregs":[],"global_phase":"0x0.0p+0","parameters":[]}
["rz",[17],[],["0x1.921fb54442d18p+0"],null]
["cz",[17,18],[],[],null]
```

Each operation is `[name, qubits, clbits, parameters, payload]`. Floats are C99 hex strings
(`float.hex()`), so values round-trip exactly. Non-standard operations (matrix gates, Pauli
evolution, annotated operations, control flow) carry a typed `payload`. The hash of a circuit
is the SHA-256 of its canonical uncompressed bytes, so gzip timestamps do not matter.

A layout is `{input_num_qubits, output_num_qubits, initial_index_layout, final_index_layout,
routing_permutation}`, or `null` when Qiskit attached none (identity).

## LSF orchestration records

`<results-root>.lsf/` contains the launch configuration, durable job ledger, logs and
orchestration `report.md`/`report.json`. See the [LSF guide](cluster.md).
