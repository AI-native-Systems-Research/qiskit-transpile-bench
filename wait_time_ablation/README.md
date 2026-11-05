# Wait-time weight ablation

Compare **decay + weighted wait time** with **plain decay**, using one shared decay
sample per case. This standalone workflow leaves `compare_distributions` unchanged.
Run from the repository root with Python 3.11 or later and the existing dependencies:

```bash
uv sync --extra verifier --extra comparison
```

The source must contain a prebuilt native Qiskit extension supporting
`Heuristic.with_wait_time(weight)` and `SabreLayout(heuristic=...)`. Collection
snapshots that candidate and checks the weighted API and actual routing in an
isolated process. It does not build or modify the candidate checkout.

If preflight reports `with_wait_time() takes no arguments`, the loaded extension
still implements the old strict tie-break API. Rebuild the weighted source for
the Python interpreter running collection; updating Python or Rust source alone
does not update the compiled extension.

## Collect and report all weights

The full-sweep entry point collects all **17 weights** and then generates the
report and D2/N2 graphs. Use a new output directory for a fresh collection:

```bash
.venv/bin/python -m wait_time_ablation.full_sweep \
  --source /Users/ophir/PycharmProjects/qiskit-evolved \
  --mode end_to_end \
  --output output/wait_time_weight_ablation_all_weights
```

This command pins the confirm profile's 100 independent seeds per case/arm,
20 layout trials, 20 layout-swap trials, 20 routing trials, master seed
`20261105`, 20,000 bootstrap replicates, and bootstrap seed `20261105`.
It defaults to the confirm profile and six workers with one native thread per
worker. The full grid is:

```text
0, 1e-8, 1e-7, 1e-6, 3e-6, 1e-5, 2.2e-5, 5e-5, 1e-4, 3e-4, 1e-3, 0.0025, 0.05, 0.1, 0.25, 0.5, 0.75
```

On the completed study's 61 cases, including the shared decay baseline, this is
**109,800 observations**: 61 cases × 100 seeds × 18 arms. The grid covers weak
through strong penalties and includes the current preset coefficient
`2.2e-5`. It is exploratory; the best coefficient is not known in advance. Decay
remains enabled in every weight arm. Weight zero exercises wait-time bookkeeping
without adding a scoring penalty; independent seed samples mean its measured
curve point need not equal zero exactly.

`full_sweep.py` stores the complete grid explicitly. The lower-level `prepare`
command retains its original 11-weight default so the frozen collection-code
checksums of the existing sessions remain compatible. Use `full_sweep` for the
complete grid, or pass an explicit `prepare --weights` list for a custom study.
Repeat the same full-sweep command to resume a matching new collection; successful
samples are reused and the report runs after collection completes.

End-to-end collection starts from the original logical circuit and applies the
same weight during SABRE layout search, integrated routing, and any fallback
routing. Other preset settings and downstream optimization stay fixed. Cases
where VF2 bypasses SABRE remain in the panel and execution coverage is reported.

For routing conditional on a fixed decay-selected layout, run the command with
a different output directory and `full_sweep --mode fixed_layout`. This secondary
study has its own baseline and report; never combine its observations with the
end-to-end study. It does not measure layout improvements.

## Reconstruct the completed results

The completed session contains all 17 weights and 109,800 verified observations.
Regenerate its report, graphs, statistics, and tables directly from saved raw
data, without compiling any circuits:

```bash
.venv/bin/python -m wait_time_ablation report \
  --output output/wait_time_weight_ablation_end_to_end_extended_0025
```

This uses the complete frozen 61-case panel for every weight. It needs no
temporary-graph scripts and does not recompute old weights on an incomplete
case subset. The graph coordinates and confidence intervals come from the
saved observations and frozen analysis settings.

Keep the **entire session directory**, including `manifest.json`,
`profile_manifest.json`, `raw/`, `fixtures/`, `build/`, and `harness/`, when
archiving or transferring the study. The manifest records exact dependencies,
source and native-extension hashes, case weights, seeds, settings, and reuse
provenance. Preserve the repository's dependency lockfile and use the recorded
runtime versions to reproduce the analysis. The `output/` directory is ignored
by Git; committing the scripts alone does not archive the measurements.

A fresh `full_sweep` run recompiles every arm. To repeat the historical experiment,
use the same prebuilt Qiskit Git checkout, profile inputs, and dependency versions
recorded in the saved manifest. A changed candidate or profile is a new experiment.
The archived `build/source` is a frozen file snapshot, not a Git checkout, and
cannot be passed directly to `--source`. Session timestamps and provenance IDs
of a fresh collection differ; rebuilding a native extension on another platform
can also change numerical results. Regenerating from saved raw data reconstructs
the recorded measurements, statistics, and plotted values. SVG/PDF export
timestamps and internal identifiers may differ even when those values match.

## Pilot and custom settings

To add coefficients to a completed study, use a new extension directory. This
reuses the parent's frozen build, inputs, preparations, settings, and observations
(including decay), then compiles only the newly requested weights:

```bash
uv run python -m wait_time_ablation.extend \
  --base-output output/wait_time_weight_ablation_end_to_end \
  --weights 0.05,0.1,0.25,0.5,0.75 \
  --output output/wait_time_weight_ablation_end_to_end_extended

uv run python -m wait_time_ablation report \
  --output output/wait_time_weight_ablation_end_to_end_extended
```

For the original 11-weight study, this adds 30,500 observations and reuses the existing
73,200, yielding 103,700 combined observations. The parent remains independently
reproducible; each inherited row records its original session and checksum.
The extension requires a complete verified parent, matching collection code and
runtime dependencies, and weights absent from the parent. Repeat the same
extension command to resume; successes are retained, and only failed or missing
new-weight samples can be compiled. Reports show the combined weight range and
disclose that it was expanded after inspecting the original results.

The final extension added only `0.0025`, reusing those 103,700 observations:

```bash
.venv/bin/python -m wait_time_ablation.extend \
  --base-output output/wait_time_weight_ablation_end_to_end_extended \
  --weights 0.0025 \
  --output output/wait_time_weight_ablation_end_to_end_extended_0025
```

This produces the completed 109,800-observation session used by the reconstruction
command above. Repeating this extension command validates and reuses the saved
samples; it does not recompile successful observations.

Start with a small pilot before a full study:

```bash
uv run python -m wait_time_ablation prepare \
  --source /Users/ophir/PycharmProjects/qiskit-evolved \
  --case-id confirm/qft_cp_n8/melbourne_14_u/L2 \
  --weights 0,2.2e-5,1e-3 --seeds-per-arm 3 --trials 2 --workers 2 \
  --bootstrap-replicates 500 \
  --output output/wait_time_weight_ablation_pilot

uv run python -m wait_time_ablation report \
  --output output/wait_time_weight_ablation_pilot
```

Repeat `--case-id` to select more cases. Pilot reports are explicitly labeled;
their subset weights and small seed counts do not represent the full confirm
panel. `--profile`, `--weights`, `--seeds-per-arm`, `--master-seed`, `--trials`,
`--workers`, `--bootstrap-replicates`, and `--bootstrap-seed` control the design.
Numeric equivalents such as `0.000022` and `2.2e-5` share one identity; duplicate
or negative/nonfinite/unrepresentable coefficients are rejected.

Repeat the identical prepare command to resume. Successful observations are
retained; failed attempts are archived and their identical frozen seeds retried.
Changed grids, seeds, profiles, budgets, candidate identity, or collection code
require a new output directory. Frozen fixtures, build hashes, resolved heuristic
components, and C0 legality/C6 routing-replay checks identify each observation.
Both commands exit with status 2 for incomplete or invalid runs; partial reports
remain useful and show missing data explicitly.

## Results

`report` reads only frozen data; it never compiles circuits or imports candidate
Qiskit. The main outputs are:

| File | Contents |
| --- | --- |
| `report.md` | Configuration, completeness, both graphs, tradeoffs, regressions, and provenance |
| `figures/D2_change_vs_weight.png` | Native two-qubit depth change against decay |
| `figures/N2_change_vs_weight.png` | Native two-qubit count change against decay |
| `statistics.json` | Estimates, intervals, diagnostics, and validity statuses |
| `plot_data.json` | Exact plotted values, intervals, and missing-point reasons |
| `raw_observations.csv` | Collected measurements, checks, and provenance |
| `arm_summaries.csv` | Per-case distributions |
| `case_contrasts.csv` | Each coefficient versus decay, including panel contributions |
| `aggregate_summaries.csv` | Panel, family, and optimization-level effects |

Both figures also have SVG/PDF exports. Positive coefficients use a logarithmic
axis with a separate zero position, a horizontal 0% decay reference, and a marker
at the current coefficient. Negative percentage changes mean improvement.

For each case, compute the ratio of arithmetic mean D2 or N2 to the decay mean;
aggregate these ratios with the frozen panel-weighted geometric mean. Missing
samples invalidate the affected point without dropping cases or reweighting.
No epsilon is added to undefined ratios. Metrics are measured after native
translation and downstream optimization, and describe circuit structure rather
than hardware duration.

The plots show pointwise 95% seed-bootstrap intervals (20,000 replicates by
default). Each observation's D2/N2 values stay together in resampling, and each
decay resample is shared across all coefficients. Cases and panel weights remain
fixed. Curve points are correlated through the baseline. Diagnostic-gated analytic
intervals use a Bonferroni correction over twice the number of weights per study;
family and level breakdowns remain descriptive. A zero-width empirical interval
does not prove determinism.

Assess D2 and N2 together and inspect influential cases before recommending a
coefficient. A best observed value is exploratory, not proof of an optimum. Refine
or extend the grid in a new session; use fresh seeds for repeatability and unseen
circuits for generalization.

## Validation

```bash
uv run pytest tests/test_wait_time_ablation_*.py tests/test_compare_distributions_*.py
uv run ruff check wait_time_ablation tests/test_wait_time_ablation_*.py
```

See the [design plan](../design/wait-time-weight-ablation-plan.md) for the full
protocol and rationale.
