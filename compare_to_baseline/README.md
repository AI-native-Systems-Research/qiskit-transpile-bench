# Evolved versus baseline: D2, N2, and runtime

Run the `compare_distributions` benchmark panel on two separate Qiskit builds:

| Build | Default checkout | SABRE configuration |
| --- | --- | --- |
| Evolved | `/Users/ophir/PycharmProjects/qiskit-evolved` | Decay plus wait time with the new default weight `0.0025`, during layout and routing |
| Baseline | `/Users/ophir/PycharmProjects/qiskit-evolved-main` | Stock decay during layout and routing, without wait time |

The defaults select the same 61 scored, exact-100-seed cases as
`compare_distributions`, with 20 layout, layout-swap, and routing trials.
Six workers collect D2 and N2 using 100 seeds per case and build. After all quality
workers finish successfully, runtime measurements run serially using two seeds
per case and build. Each build has separate frozen, independent seed lists for
quality and runtime. The original logical input, target, optimization level, and
trial budgets match.
Baseline `SabreLayout` does **not** need the newer `heuristic=` API.
The evolved configuration explicitly applies weight `0.0025` to both `SabreLayout`
and `SabreSwap`, matching the evolved checkout's current `wait_time` preset.
The frozen manifest records this configuration for reporting and resume checks.

These scripts compare two checkouts, including any differences beyond the
wait-time heuristic. They do not establish that every observed difference is
caused by wait time alone. Existing `compare_distributions` results are not reused.

## Setup

Run commands from the benchmark repository root, with Python 3.11 or later:

```bash
uv sync --extra verifier --extra comparison
```

Both source directories must already contain a native `_accelerate` extension
built in **release mode** for this interpreter. **The baseline checkout had no
native extension when these scripts were prepared.** Build it before collecting
measurements, and use matching release settings for both checkouts.
Qiskit's source build requires its Rust toolchain and Python build dependencies:

```bash
uv pip install --python .venv/bin/python 'setuptools>=77.0' 'setuptools-rust>=1.13.0'
BENCH_PY="$(pwd)/.venv/bin/python"
(cd /Users/ophir/PycharmProjects/qiskit-evolved-main && \
  "$BENCH_PY" setup.py build_rust --inplace --release)
(cd /Users/ophir/PycharmProjects/qiskit-evolved && \
  "$BENCH_PY" setup.py build_rust --inplace --release)
```

Release mode is mandatory, with no override. Before collection, the selected
extension's SHA-256 must match the checkout's native library under
`target/release/`. Missing, debug, unknown, or ambiguous build evidence is rejected.
The same policy applies to resumed sessions, worker preflight, and the `report`
command, so earlier results without release evidence cannot be reused as a
release comparison.

The collector freezes prebuilt extensions; it does not build or install Qiskit
into either checkout. If multiple extension files exist, it selects the filename
Python would import, using interpreter extension-suffix precedence. Check the
recorded path in `preflight/*.json` when rebuilding a checkout with multiple
extensions: an older interpreter-specific file can take precedence over an
`abi3` file. Native hashes, revisions, working-tree changes, available release
fingerprints, and dependency versions are saved with the results. Existing
extensions' original compiler commands cannot be reconstructed reliably.
Both builds must be identified as `release` from the recorded hash evidence.
Rebuild and use a new output directory if the selected extension does not match
the release library; an existing debug extension is never silently replaced.

## Full comparison

```bash
.venv/bin/python -m compare_to_baseline run
```

The defaults above are equivalent to:

```bash
.venv/bin/python -m compare_to_baseline run \
  --evolved /Users/ophir/PycharmProjects/qiskit-evolved \
  --baseline /Users/ophir/PycharmProjects/qiskit-evolved-main \
  --output output/compare_to_baseline \
  --trials 20 --workers 6 --seeds 100 --runtime-seeds 2 \
  --warmups 1 --runtime-repeats 1
```

Read `output/compare_to_baseline/report.md` when the command finishes. Collection
and reporting can also run separately:

```bash
.venv/bin/python -m compare_to_baseline prepare
.venv/bin/python -m compare_to_baseline report
```

`prepare` resumes compatible sessions, retaining successful observations and
retrying the identical failed or missing seeds. Failed attempts are archived in
`attempts/`. Resume uses the frozen builds, even if the original checkouts have
subsequently changed. Use a new output directory to compare newly built sources
or change seeds, worker count, trial budgets, cases, heuristic weight, timing settings, dependencies,
collection code, or host. Exit code `2` indicates an error or incomplete observations;
`report` can still describe an interrupted session without compiling anything.
Successful cached quality rows must retain verified C0 and C6 checks and consistent
metrics. Runtime rows additionally require verified timed-output checks and matching
output identities for every timed repetition. A resumed run finishes any missing
quality observations before starting or resuming serial runtime measurements.

The two-stage collection format cannot resume earlier sessions that timed all 100
seeds. Keep those results and start a new output directory, for example
`--output output/compare_to_baseline_two_stage`. The `report` command can still read
earlier sessions that satisfy the release-build checks.

An output directory without a frozen manifest must be empty. If freezing fails
partway through, use a new output directory; existing files are never removed
automatically. Concurrent changes to a source checkout during freezing are
rejected, and runtime plugin metadata is generated from the frozen source files.

## Small smoke comparison

```bash
.venv/bin/python -m compare_to_baseline run \
  --output output/compare_to_baseline_smoke \
  --case confirm/qft_cp_n8/melbourne_14_u/L2 \
  --seeds 3 --runtime-seeds 2 --trials 2
```

Repeat `--case` to select more exact case IDs. `--seeds` accepts 1–100 quality seeds;
the default is 100. `--runtime-seeds` independently selects 1–100 runtime seeds;
the default is 2. `--workers` controls only quality collection and defaults to 6.
Selection keeps the original eligibility rule and normalizes the selected
cases' weights before measurement. Reduced runs are marked as smoke tests in the
manifest. `--profile` can select another profile using the repository's fixtures.

## Measurements

- **D2:** final native two-qubit depth, using the existing structural checker.
- **N2:** final native two-qubit gate count, using the same checker.
- **Runtime:** wall-clock seconds for constructing a fresh preset pass manager,
  configuring SABRE, and running the full transpilation pipeline.

Quality collection runs up to six subprocess workers concurrently, with one
native-library thread per worker. Each quality seed performs one compilation with
initialization/routing snapshots and the existing C0 structural and C6 routing
replay checks. D2 and N2 distributions use only these quality observations.

All quality workers finish before the serial runtime stage begins. Runtime
excludes interpreter startup, imports, fixture loading, input circuit
copying, output serialization, and correctness checks. A separate compilation
with initialization/routing snapshots performs the existing C0 structural and
C6 routing replay checks. Its output is checked against the measured compilation.
Invalid outputs remain failures and never enter successful distributions.
The case's `timeout_s` budget applies to each timed compilation, checked
compilation, and warmup. A separate process watchdog bounds unresponsive native
code; compilations returning after their budget are recorded as failures.

During the runtime stage, each build runs in its own persistent process for a case.
Both workers are warmed up before measurement, then run **serially**, alternating which build goes first
on successive seed indices and cases. Each native library gets one thread. There
is deliberately no parallel-worker option for timing; `--workers` applies only to
the earlier quality stage. The collector also takes
the harness's machine/user runtime lock; unrelated programs and older distribution
scripts can still affect CPU load, so run timing comparisons on an otherwise idle
machine.

The default full panel contains 12,200 quality observations and 244 runtime
observations, for 12,444 records total. Quality requires 12,200 checked compilations;
runtime requires 244 timed and 244 checked compilations, plus 122 warmups. That is
12,810 compilations across both stages. `--runtime-repeats 3` takes three timed
compilations per runtime seed, each using a fresh pass manager. Those repetitions
are averaged into one seed observation, not treated as independent statistical
samples. Runtime summaries use only runtime observations; their checked D2/N2
values do not enter the quality distributions.

Per-build tables include mean, sample SD, nearest-rank median, Q25, Q75, IQR,
P90, minimum, and maximum. Ratios are **evolved / baseline**; values below 1 and
negative percentage changes favor evolved for all three metrics. Runtime speedup
is the inverse ratio. Panel summaries use the frozen-weight geometric mean of
per-case ratios. A positive baseline and zero evolved D2/N2 produce a descriptive
ratio of `0` and change of `-100%`; a zero baseline makes the ratio undefined.
Missing, failed, or nonpositive ratios leave logarithmic aggregates and intervals
unavailable; no case is silently dropped or reweighted. Mean-ratio
intervals are approximate pointwise 95% independent-arm log-delta intervals.
Median and P90 ratios are descriptive. With the default two runtime seeds, runtime
spread, quantiles, and approximate intervals have very limited precision. Timing
intervals describe these observed runs and do not capture between-machine or
between-session variability.

## Outputs

| File | Contents |
| --- | --- |
| `report.md` | Readable D2, N2, and runtime comparison |
| `statistics.json` | Complete estimates, intervals, and completeness status |
| `arm_summaries.csv` | Per-case/build distributions for all three metrics |
| `case_comparisons.csv` | Per-case mean, median, and P90 ratios and changes |
| `aggregate_summary.csv` | Frozen-weight panel ratios and runtime speedups |
| `raw_observations.csv` | Collected measurements identified by quality/runtime phase |
| `figures/quality_scatter.png` / `.svg` | Evolved versus baseline D2 and N2 |
| `figures/runtime_scatter.png` / `.svg` | Evolved versus baseline runtime |
| `plot_data.json` | Plotted coordinates and exclusions |
| `manifest.json`, `fixtures/`, `build/`, `harness/` | Frozen inputs, builds, settings, and provenance |
| `raw/`, `attempts/`, `logs/`, `preflight/` | Resumable records, prior failures, worker logs, and import checks |
| `collection_status.json` | Current progress and completeness |

## Validation

```bash
.venv/bin/pytest tests/test_compare_to_baseline_*.py
.venv/bin/ruff check compare_to_baseline tests/test_compare_to_baseline_*.py
```
