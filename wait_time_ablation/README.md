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

## Collect and report

```bash
uv run python -m wait_time_ablation prepare \
  --source /Users/ophir/PycharmProjects/qiskit-evolved \
  --mode end_to_end \
  --output output/wait_time_weight_ablation_end_to_end

uv run python -m wait_time_ablation report \
  --output output/wait_time_weight_ablation_end_to_end
```

Defaults are the confirm profile's 61 scored exact-100 cases, 100 independent
seeds per case/arm, 20 layout trials, 20 layout-swap trials, 20 routing trials,
master seed `20261105`, and six workers with one native thread per worker.
The default grid is:

```text
0, 1e-8, 1e-7, 1e-6, 3e-6, 1e-5, 2.2e-5, 5e-5, 1e-4, 3e-4, 1e-3
```

Including the shared decay baseline, this is **73,200 observations**. The grid
covers weak through strong penalties and includes the current preset coefficient
`2.2e-5`. It is exploratory; the best coefficient is not known in advance. Decay
remains enabled in every weight arm. Weight zero exercises wait-time bookkeeping
without adding a scoring penalty; independent seed samples mean its measured
curve point need not equal zero exactly.

End-to-end collection starts from the original logical circuit and applies the
same weight during SABRE layout search, integrated routing, and any fallback
routing. Other preset settings and downstream optimization stay fixed. Cases
where VF2 bypasses SABRE remain in the panel and execution coverage is reported.

For routing conditional on a fixed decay-selected layout, run both commands with
a different output directory and `prepare --mode fixed_layout`. This secondary
study has its own baseline and report; never combine its observations with the
end-to-end study. It does not measure layout improvements.

## Pilot and custom settings

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
