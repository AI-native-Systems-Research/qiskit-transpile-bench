# Baseline versus evolved on Benchpress: D2 and N2

Run a broad panel from [Qiskit Benchpress](https://github.com/Qiskit/benchpress)
against two frozen, release-built Qiskit checkouts. The default workload targets
**about 90 minutes total for baseline plus evolved** on the local Apple M1 Max.
It favors distinct circuits and targets over repeated seeds.

| Setting | Default |
| --- | --- |
| Baseline checkout | `../qiskit-evolved-main`, stock SABRE decay |
| Evolved checkout | `../qiskit-evolved`, decay + wait time, weight **0.0025 in both layout and routing** |
| Circuit/target combinations | 400, covering 215 distinct logical inputs |
| Optimization levels | 2 and 3: 800 cases |
| Transpiler seeds | 1 matched seed per case and build |
| SABRE trials | 20 layout, 20 layout-swap, 20 routing, for both builds |
| Workers | 6; one native-library thread per process |
| Total compilations | 1,600, including both builds |
| Measurements | Native two-qubit depth (D2) and count (N2) |

There is **no runtime comparison or runtime acceptance check**, no calibration,
warmup, separate timing stage, or extra timed compilation. Each observation runs
the original upstream benchmark callable once. The fixed panel is completed even
if collection exceeds 90 minutes. A configurable per-process hang watchdog
(`--timeout 600`) records a failed observation if a worker does not finish; this
does not create a runtime metric or a speed verdict.

## Run

From this repository root, using Python 3.11 or later:

```bash
uv sync --extra verifier --extra comparison
uv pip install --python .venv/bin/python -r compare_to_baseline_benchpress/requirements.txt
.venv/bin/python -m compare_to_baseline_benchpress run
```

Results go to `output/compare_to_baseline_benchpress/report.md`. Both checkouts
must already have release native extensions compatible with this interpreter.
The same release-only source freezing and native hash checks as
[`compare_to_baseline`](../compare_to_baseline/README.md#setup) apply. Both local
checkouts passed these checks during the smoke validation. This runner does not
build or modify either checkout.

The first run fetches Benchpress commit
`b695f30e83a32bac05b9b4d8e98d37ba9aae5236`. To use a local Git mirror containing
that commit, add `--benchpress-source /path/to/benchpress` or set
`QTB_BENCHPRESS_SOURCE`. A modified mirror working tree has no effect: preparation
archives the pinned Git commit. No IBM account or remote quantum service is used;
`qiskit-ibm-runtime==0.51.0` supplies the local FakeTorino backend data.

The full command with explicit defaults is:

```bash
.venv/bin/python -m compare_to_baseline_benchpress run \
  --baseline ../qiskit-evolved-main --evolved ../qiskit-evolved \
  --output output/compare_to_baseline_benchpress \
  --panel-size 400 --seeds 1 --trials 20 --workers 6
```

`--workers 1` collects serially and takes substantially longer. The default of six
workers is the configuration behind the 90-minute estimate. `--seeds` accepts
1–100. Increasing seeds trades time for repeated observations. Seed lists are
deterministic and matched between builds, with a default master seed of `20261107`.

## Coverage and selection

The pinned inventory has 1,054 nodes. Selection uses static source sizes, keeps
device-compatible inputs with at most 100 logical qubits, at most 30,000 QASM
statements, and at most 2,000 Hamiltonian terms. It selects one target for every
bounded logical input before adding more targets. Suites and Hamiltonian
categories are interleaved, and preferred abstract topologies rotate between
inputs. Every selection and exclusion has a recorded reason in the manifest.

| Suite | Circuit/target combinations |
| --- | ---: |
| Summit | 9 |
| Feynman | 47 |
| Device HamLib | 66 |
| Abstract HamLib | 80 |
| QASMBench small | 88 |
| QASMBench medium | 46 |
| QASMBench large | 64 |
| **Total** | **400** |

This includes all nine Summit workloads (QFT, quantum volume, both circular SU2
sizes, BV, square Heisenberg, QAOA, BV-like simplification, and Clifford), and
HamLib chemistry, condensed matter, binary optimization, and discrete
optimization. Targets comprise FakeTorino (122 combinations), heavy hex (70),
linear (73), square (69), and all-to-all (66). All-to-all remains as a useful
control where routing can be unnecessary. The larger unrestricted corpus is
excluded to keep a few expensive inputs from dominating the run.

## Why approximately 90 minutes

Current worker smoke runs took about eight seconds per small serial observation
and 15–18 seconds per observation with three concurrent workers, including imports,
input fingerprints, upstream test collection and compilation. For planning, allow
an average of **19 seconds of worker occupancy per observation** at six workers,
plus five minutes of preparation: `1600 × 19 / 6 / 60 + 5 ≈ 89 minutes`.
This allowance is an extrapolation, not a measured mean for the full panel.
The 15–18-second smoke runs used one SABRE trial; the planned allowance for twenty
trials and six workers has not been validated over the complete circuit mix.

An older local session (`swap-absorption-full-20261028-151026`) ran 600 observations
in 2,050.12 seconds, but its faster per-process overhead does not represent the
current worker. It is retained as historical context, not used to claim that a
serial run will finish in 90 minutes.

Allow roughly **60–120 minutes**, with possible outliers, for the default on
comparable hardware. The broader circuit mix, input fingerprinting, wait-time
heuristic, cold caches, source fetch, and host load can change the result. The
complete 90-minute run has not been used as a validation step. Planning assumptions
and the reference measurements are saved under `manifest.json` → `selection`.
Overriding trials removes the estimate; filtered smoke runs have no duration claim.

## Freeze, smoke-test, resume, and report

To inspect every selected case without compiling:

```bash
.venv/bin/python -m compare_to_baseline_benchpress freeze
```

Then `run` with identical settings uses the frozen builds and inputs. A small
comparison using one Feynman circuit is:

```bash
.venv/bin/python -m compare_to_baseline_benchpress run \
  --output output/compare_to_baseline_benchpress_smoke \
  --case device-feynman/qft_4/fake_torino/L2
```

Repeat `--case` to include additional exact case IDs from the selected panel.
Collection and reporting can run separately:

```bash
.venv/bin/python -m compare_to_baseline_benchpress prepare
.venv/bin/python -m compare_to_baseline_benchpress report
```

Re-running the same collection command reuses valid successful observations and
retries failed or missing seeds. Prior failures are archived in `attempts/`.
Use a new output directory after changing settings, code, dependencies, host, or
the builds to compare. Source checkout edits after freezing do not replace the
frozen builds. An output directory without a complete manifest must be empty;
if initial freezing fails, select a new output directory. Concurrent collection
into one output is rejected. Exit code 2 means failure or incomplete observations;
`report` can describe a partially collected session.

## Checks and interpretation

Both builds must collect identical upstream node inventories and target
fingerprints. Each successful observation must execute exactly one selected
benchmark callable and pass upstream setup, test, and teardown, including
Benchpress's native gate/topology validation. Actual logical input fingerprints
must match between builds. D2/N2 come from Benchpress's native two-qubit gate
metrics. These are the upstream structural checks; this workflow does not perform
the separate C6 routing replay used by `compare_to_baseline`.

The evolved worker explicitly sets the same weight of **0.0025** on integrated
`SabreLayout` and fallback `SabreSwap`, with equal trial budgets in both builds.
On all-to-all or already-compatible circuits, the preset may bypass SABRE. The
comparison includes all other differences between the two checkouts, so it does
not isolate the causal effect of the wait-time term.

Ratios are **evolved / baseline**; below 1 favors evolved. With one seed per case,
results describe that seed, not a well-estimated distribution; sample SD is
unavailable and there are no confidence intervals. Full-panel geometric means
are unavailable if any case is incomplete or has a nonpositive/undefined ratio.
A separately labeled positive-ratio subset reports its exact coverage, weights,
and exclusions, including the zero-count simplification cases. There is no
acceptance verdict.

Outputs include `report.md`, `statistics.json`, `raw_observations.csv`,
`arm_summaries.csv`, `case_comparisons.csv`, `aggregate_summary.csv`,
`positive_subset_summary.csv`, `group_summary.csv`, and quality scatter PNG/SVG
figures. Raw JSON, attempts, logs, frozen builds, the adapted Benchpress source,
configuration files, harness code, and provenance are retained for auditing and
resume. The original `compare_to_baseline` workflow is unchanged.

## Validation

```bash
.venv/bin/pytest tests/test_compare_to_baseline_benchpress_*.py
.venv/bin/ruff check compare_to_baseline_benchpress tests/test_compare_to_baseline_benchpress_*.py
```
