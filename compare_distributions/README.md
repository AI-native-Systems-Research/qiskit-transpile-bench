# Four-preset distribution comparison

This implements [benchmark_update.md](benchmark_update.md) as two independent stages.
Run the commands from the benchmark repository root using Python 3.11 or later.
The default confirm profile selects its 61 scored cases with exactly 100 seeds per
case and arm: 24,400 compilations across `wait_time`, `decay`, `lookahead`, and `basic`.
Only the three wait-time-first comparisons enter the report.

Install the harness and comparison dependencies:

```bash
uv sync --extra verifier --extra comparison
```

The source checkout must already contain a native Qiskit extension implementing
`Heuristic.with_wait_time`, built for the Python interpreter running the command.
Stage 1 copies the candidate sources and native extension into the output directory;
it does not build Qiskit or change the source checkout. The extension hash, source
revision and modifications, available build fingerprints, and dependency versions
are recorded. The original native build command may be unavailable, which is
explicitly recorded in the manifest.

## 1. Prepare the raw data

```bash
uv run python -m compare_distributions prepare \
  --source /Users/ophir/PycharmProjects/qiskit-evolved \
  --output ./compare_distributions_results
```

Before collecting observations, this stage freezes eligibility and weights,
prepares each input once using a separate preparation seed, freezes its initial
layout, and records independently generated lists of 100 unique routing seeds
for every case and arm. All arms use the same candidate build, prepared circuit,
target, layout, and downstream processing. Each result records native two-qubit
depth (`D2`) and count (`N2`), structural legality (`C0`), and routing replay (`C6`).
The shared wait-time measurements are stored once.

Defaults are master seed `20261103`, 20 routing trials, 20 layout trials, 20
layout-swap trials, and six worker processes. Internal native-library threading
is limited to one thread per worker. Change these using `--master-seed`,
`--trials`, and `--workers` before freezing the session. `--profile` selects a
different manifest; scored cases declaring exactly 100 seeds remain the eligibility
rule, and any necessary weight normalization is frozen before measurement.

The same command resumes a compatible frozen session. Start a new output
directory after changing its collection harness, profile, trial budget, or seed
protocol. A missing or failed observation keeps its affected comparison incomplete;
cases and seeds are never replaced with more favorable observations. On resume,
failed attempts are archived under `attempts/` and their identical frozen seeds
are retried; successful observations are retained.

Important outputs are `manifest.json`, `profile_manifest.json`, `prepared/`,
`raw/*.jsonl`, `collection_status.json`, and `build/`. Raw records identify the
case, arm, seed, build, and resolved configuration. `progress.json` and console
messages describe collection progress.

## 2. Prepare statistics, graphs, and the final report

```bash
uv run python -m compare_distributions report \
  --output ./compare_distributions_results
```

This stage reads frozen observations without compiling circuits again. Within
each case and arm it computes arithmetic mean, sample SD (`ddof=1`), nearest-rank
median, Q25, Q75, IQR, P90, minimum, and maximum. At 100 observations the median
is the 50th ordered value and P90 is the 90th.

Mean, median, and P90 effects aggregate case ratios using the frozen-weight
geometric mean. Relative-spread effects average case differences in SD divided
by each arm's own mean and IQR divided by its own median, in percentage points.
There is no seed pairing or four-arm normalization denominator.

Raw means receive Student-t intervals. Mean ratios receive approximate log
delta-method intervals; aggregate variances use squared case weights. The six
panel mean effects have both pointwise 95% and Bonferroni-adjusted 99.1667%
intervals. Other statistics remain descriptive. Frozen diagnostics flag unstable
approximations, and missing data, undefined ratios, and observed zero variance
retain explicit statuses without epsilon adjustments or omitted case weights.

Open `compare_distributions_results/report.md` for the final report. Supporting
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

The report concerns this fixed benchmark panel and configuration. Only wait_time
versus decay isolates the added wait-time component. D2/N2 measure structural
circuit quality, not hardware duration. This mode reports completeness and
statistical estimates without issuing the legacy two-build acceptance verdict.

## Validate the implementation

```bash
uv run pytest tests/test_compare_distributions_collect.py tests/test_compare_distributions_statistics.py
uv run ruff check compare_distributions tests/test_compare_distributions_*.py
```
