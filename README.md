# qiskit-transpile-bench

Compare a baseline and evolved Qiskit revision on circuit quality and compilation runtime.
The harness builds each revision in isolation, measures native two-qubit depth (`D2`) and
count (`N2`), records paired runtimes across the quality panel, and runs a separate
Benchpress comparison at optimization levels 2 and 3.

## Run locally

Requires Python 3.11+, Git, uv, the Qiskit Rust toolchain and a C/C++ compiler.
Benchpress preparation needs network access or local source mirrors and dependency caches.
Use an idle machine for runtime measurements; the harness does not monitor host load.

```bash
uv sync --all-extras
mkdir -p ./qtb-store
S=./qtb-sessions/idea1  # a new session directory
uv run qiskit-transpile-bench compile \
  --baseline /path/to/baseline --evolved /path/to/evolved \
  --profile confirm-profile --store ./qtb-store --results-root "$S"
uv run qiskit-transpile-bench quality --results-root "$S"
uv run qiskit-transpile-bench benchpress --results-root "$S"
# Optional upstream Python and Rust tests:
uv run qiskit-transpile-bench unit-tests --results-root "$S"
uv run qiskit-transpile-bench decide --results-root "$S"
```

`decide` writes the final report and cleans bulk build data when all required stages finish.
Quality and Benchpress each write `stages/<stage>/report.md` and `report.json` immediately.
Their runtime tables show baseline/evolved seconds, absolute differences and relative changes.
Runtime differences are descriptive; they do not impose the former cost thresholds.
The separate correctness and cost stages have been removed. Quality retains C0 legality,
C6 routing replay and the determinism audit. Optional upstream tests retain their gate.

## Run on LSF

```bash
uv run python lsf/submit.py \
  --store /shared/qtb-store --results-root /shared/qtb-sessions/idea1 \
  --baseline /shared/qiskit-baseline --evolved /shared/qiskit-evolved \
  --profile confirm-profile
```

The manager runs quality and Benchpress sequentially, decides, then cleans the session.
See the [LSF guide](docs/cluster.md) for resources, logs and resume behavior.

## Profiles and results

The default iterations profile has 12 quality cases and 930 seeds per revision.
Confirm has 109 quality cases, including 61 scored cases across eight families, exclusively
at L2/L3: 7,390 seeds per revision. Runtime measurement covers every quality case and seed
with fresh uninstrumented calls for both revisions, even when baseline quality was cached.
Benchpress uses 50 fixed circuit/target entries × two levels × three seeds per revision.

| Verdict | Exit | Meaning |
| --- | ---: | --- |
| `PASS` | 0 | Quality improved and every required check completed and passed |
| `NO_IMPROVEMENT` | 10 | The quality improvement rule failed |
| `CONSTRAINT_VIOLATION` | 20 | A candidate quality guard or optional test failed |
| `INCONCLUSIVE` | 30 | Required evidence/stages are missing, or the baseline failed |
| `ERROR` | 40 | Harness, build or worker failure |

Start a new session after changing the harness or profile; existing sessions pin their code
and data. Existing results remain on disk and can be read with their archived harness.
See the [documentation index](docs/README.md) and [manual workflow](docs/workflow/README.md).

For the four-preset `wait_time` distribution study, use the separate
[two-stage comparison workflow](compare_distributions/README.md): `prepare` freezes and
collects the raw data; `report` generates statistics, graphs, and the final report.
Collection defaults to end-to-end transpilation: every heuristic controls its own
SABRE layout search and routing. Use a fresh output directory (for example
`./compare_distributions_results_end_to_end`) to preserve earlier fixed-layout runs;
`--mode fixed_layout` retains that conditional routing experiment. The command's default
output is `./compare_distributions_results`, and it does not issue a two-build verdict.

For a broad, quality-only baseline/evolved comparison, use
[`compare_to_baseline_benchpress`](compare_to_baseline_benchpress/README.md).
It selects 400 Benchpress circuit/target combinations at L2/L3 with one seed per
build (1,600 compilations), targeting roughly 90 minutes with six workers. Evolved uses
wait-time weight `0.0025` in both SABRE layout and routing; no runtime comparison
or acceptance check is performed.

## Development

```bash
uv run pytest
uv run ruff check src worker verifier tests lsf
uv run python tools/probes/confirm_panel.py
```
