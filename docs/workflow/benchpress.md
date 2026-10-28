# benchpress

[Documentation index](../README.md) · [Workflow](README.md)

```bash
uv run qiskit-transpile-bench benchpress --results-root "$S"
```

Compile must be complete. Benchpress runs independently of the quality gate and is required
for a complete session. Run it sequentially with quality on an idle machine.

The stage follows the `qiskit-evolved/tools/benchpress` reference: pinned Benchpress and
pytest-benchmark fork commits, a deterministic 50 circuit/target panel, optimization levels
2 and 3, transpiler seeds 0, 1 and 2, and reproducible backend construction. Each revision
runs 300 selected compilations (600 across both revisions).

Preparation creates private environments using the session's built Qiskit wheels, checks
native-library provenance, and checks identical inventories, targets and dependencies.
Source repositories are fetched at fixed commits. To use local Git mirrors containing the
pins, set `QTB_BENCHPRESS_SOURCE` and `QTB_BENCHMARK_SOURCE`. Dependency installation uses
the configured package mirror/cache; the shared baseline store is never modified.

Each call measures `pm.run(circuit)` wall time with a single library thread. Circuit loading,
imports, preset construction, result export and metric extraction are outside the timed region. There are no
host-load checks or noise adjustments. Baseline and evolved calls run sequentially.
Reports contain native two-qubit depth/count and runtime, with paired differences/ratios.
These comparisons are descriptive; completeness is required, and missing/failed calls are
reported explicitly.

Artifacts live in `stages/benchpress/`: preparation logs and provenance, the selected panel,
individual paired checkpoints, summary, evidence, `report.md`, `report.json` and state.
Retries reuse completed session-local pairs. A complete stage never reruns in its session.
`decide` includes its report and `clean` retains measurements needed to reproduce reports.
