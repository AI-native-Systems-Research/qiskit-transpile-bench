# Four-preset distribution comparison

This implements [benchmark_update.md](benchmark_update.md) as two independent stages.
Run the commands from the benchmark repository root using Python 3.11 or later.
The default confirm profile selects its 61 scored cases with exactly 100 seeds per
case and arm: 24,400 compilations across `wait_time`, `decay`, `lookahead`, and `basic`.
Only the three wait-time-first comparisons enter the report. The default
`end_to_end` mode lets each arm use its heuristic throughout layout search and
routing. `fixed_layout` retains the earlier routing-only experiment.

Install the harness and comparison dependencies:

```bash
uv sync --extra verifier --extra comparison
```

The source checkout must already contain a native Qiskit extension implementing
`Heuristic.with_wait_time`, built for the Python interpreter running the command.
End-to-end collection also requires the optional `SabreLayout(heuristic=...)`
Python API. Its default remains decay without wait time; the benchmark explicitly
supplies each arm's complete heuristic.
Stage 1 copies the candidate sources and native extension into the output directory;
it does not build Qiskit or change the source checkout. The extension hash, source
revision and modifications, available build fingerprints, and dependency versions
are recorded. The original native build command may be unavailable, which is
explicitly recorded in the manifest.

## 1. Prepare the raw data

```bash
uv run python -m compare_distributions prepare \
  --source /Users/ophir/PycharmProjects/qiskit-evolved \
  --mode end_to_end \
  --output ./output/compare_distributions_end_to_end
```

Before collecting observations, this stage freezes eligibility, weights, original
logical inputs, targets, optimization settings, trial budgets, and independently
generated lists of 100 unique transpilation seeds for every case and arm. Each
run starts from the original logical circuit and executes the full preset
pipeline. The selected arm's heuristic configures integrated `SabreLayout` and
any fallback `SabreSwap`. `SabreLayout` keeps its integrated routing enabled, and
the preset's routing condition avoids routing an already routed circuit again.
Layouts vary by arm and seed and are measured outcomes; there is no shared
decay-selected layout or shared preparation seed in this mode.

All arms use the same candidate build, logical input, target, and downstream
optimization settings. Every observation records final native two-qubit depth
(`D2`) and count (`N2`), structural legality (`C0`), initialization and routing
snapshots for replay (`C6`), and hashes identifying its actual layout. The shared
wait-time measurements are stored once and reused in the three contrasts.

Defaults are master seed `20261103`, 20 routing trials, 20 layout trials, 20
layout-swap trials, and six worker processes. Internal native-library threading
is limited to one thread per worker. Change these using `--master-seed`,
`--trials`, and `--workers` before freezing the session. `--profile` selects a
different manifest; scored cases declaring exactly 100 seeds remain the eligibility
rule, and any necessary weight normalization is frozen before measurement.

The same command resumes a compatible frozen session. Start a new output
directory after changing its execution mode, collection harness, profile, trial budget, or seed
protocol. A missing or failed observation keeps its affected comparison incomplete;
cases and seeds are never replaced with more favorable observations. On resume,
failed attempts are archived under `attempts/` and their identical frozen seeds
are retried; successful observations are retained.

For a separate conditional routing experiment, pass `--mode fixed_layout` and a
different output directory. That mode prepares each circuit once with a fixed
preparation seed and decay-selected layout, then compares only the routing
heuristic from that physical circuit. Frozen manifests without `execution_mode`
are interpreted as this legacy mode when reporting. Fixed-layout observations
cannot be reused as end-to-end observations. Keep old result directories intact
when collecting the new experiment.

Important outputs are `manifest.json`, `profile_manifest.json`, `fixtures/`,
`raw/*.jsonl`, `collection_status.json`, and `build/`. Fixed-layout sessions also
retain `prepared/`. Raw records identify the
case, arm, seed, build, and resolved configuration. `progress.json` and console
messages describe collection progress.

## 2. Prepare statistics, graphs, and the final report

```bash
uv run python -m compare_distributions report \
  --output ./output/compare_distributions_end_to_end
```

This stage reads frozen observations without compiling circuits again. Within
each case and arm it computes arithmetic mean, sample SD (`ddof=1`), nearest-rank
median, Q25, Q75, IQR, P90, minimum, and maximum. At 100 observations the median
is the 50th ordered value and P90 is the 90th.

Mean, median, and P90 effects aggregate case ratios using the frozen-weight
geometric mean. Relative-spread effects average case differences in SD divided
by each arm's own mean and IQR divided by its own median, in percentage points.
There is no seed pairing or four-arm normalization denominator.

The report also overlays all three wait-time comparison histograms in
`figures/spread_histograms.png` (with SVG/PDF exports). Four panels show per-case
CV and IQR-to-median differences for D2 and N2, in percentage points. Comparisons
share 20 equal-width bins within each panel; the vertical axis counts cases without
weights. Negative differences indicate lower relative spread for wait time.
Unavailable cases are identified in the legends, and bin edges, counts, and case
IDs are exported under `spread_histograms` in `plot_data.json`.

Raw means receive Student-t intervals. Mean ratios receive approximate log
delta-method intervals; aggregate variances use squared case weights. The six
panel mean effects have both pointwise 95% and Bonferroni-adjusted 99.1667%
intervals. Other statistics remain descriptive. Frozen diagnostics flag unstable
approximations, and missing data, undefined ratios, and observed zero variance
retain explicit statuses without epsilon adjustments or omitted case weights.

Open `output/compare_distributions_end_to_end/report.md` for the final report. Supporting
outputs include:

| Artifact | Contents |
| --- | --- |
| `statistics.json` | Complete estimates, validity statuses, diagnostics, and interval metadata |
| `arm_summaries.csv` | Raw distributions for every case, arm, and metric |
| `case_contrasts.csv` | Three direct comparisons per case and metric |
| `aggregate_summaries.csv` | Panel, family, optimization-level, and leave-one-family-out results |
| `raw_observations.csv` | Tabular export of observations and provenance |
| `eligibility.csv` | Inclusion and exclusion reasons |
| `spread_counts.csv` | Descriptive lower/equal/higher case counts, raw and normalized |
| `material_regressions.csv` | Every finding meeting the frozen regression selection threshold |
| `tail_observations.csv` | Actual largest observed outcomes |
| `plot_data.json` | Plotted estimates, case order, and distribution selection reasons |
| `figures/index.md` | Figure index linking SVG, PDF, and PNG exports |
| `artifacts.md` | Output schema and interpretation |

The report concerns this fixed benchmark panel and configuration. End-to-end
intervals describe seed variability across the full pipeline, including layout
selection and routing; fixed-layout intervals condition on the prepared layout.
Only wait_time versus decay isolates the added wait-time component, applied in
both layout and routing in end-to-end mode. D2/N2 measure structural
circuit quality, not hardware duration. This mode reports completeness and
statistical estimates without issuing the legacy two-build acceptance verdict.

## Validate the implementation

```bash
uv run pytest tests/test_compare_distributions_collect.py tests/test_compare_distributions_statistics.py
uv run ruff check compare_distributions tests/test_compare_distributions_*.py
```
