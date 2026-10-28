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

The report displays improvement as `100 × (1 − ratio)`: positive percentages mean fewer
two-qubit gates, less two-qubit depth, or faster compilation. At each optimization level,
the overall quality ratio is the equally weighted geometric mean of the cases' median paired seed
ratios. A zero median change across cases can coexist with substantial improvements when
most cases are unchanged. The topology breakdown shows where the gains occur; cases with
zero baselines have undefined ratios and are excluded from geometric means, with coverage
shown explicitly.

The report follows the quality stage's layout: check results, checks needing attention,
overall improvement, a quality table with one row per test/target/level, and a separate
runtime table. The quality table shows baseline/evolved arithmetic means for D2 and N2
and their improvement percentages. The runtime table shows mean baseline/evolved seconds,
their difference and improvement, with full-panel runtime totals and paired-seed coverage.
These descriptive means are calculated from the saved seed pairs; incomplete or invalid
measurements are unavailable, and zero-baseline percentages are N/A.

The original paired statistics remain under the expandable details. Their baseline and
evolved columns are independent seed medians. Paired changes and ratios are calculated for
each matching seed before taking their medians, so they need not equal the difference or
ratio of those two displayed medians. The overall quality scores retain these paired-median
estimators; the per-test mean tables do not change stage completeness or acceptance.

Benchpress and the quality stage use different circuit/target panels and estimators.
Quality's primary score uses its frozen scored-case weights and seed log ratios; its guard
cases do not contribute to that score. Benchpress includes its entire fixed panel, uses
three seeds per case, and includes all-to-all targets. An optimization that helps only some
circuits can therefore have different aggregate gains in the two stages. Circuit quality
and compilation runtime are separate measurements: a reduction in D2/N2 does not imply a
speedup. Benchpress times an unwarmed `pm.run` call, while quality times warmed `transpile`
calls, so their runtime percentages also describe different workloads.

Artifacts live in `stages/benchpress/`: preparation logs and provenance, the selected panel,
individual paired checkpoints, summary, evidence, `report.md`, `report.json` and state.
Retries reuse completed session-local pairs. A complete stage never reruns in its session.
`decide` includes its report and `clean` retains measurements needed to reproduce reports.
The JSON report includes `quality_comparison` and `runtime_comparison` alongside the original
summary, using the same descriptive means as the Markdown tables.
