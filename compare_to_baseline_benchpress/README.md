# Balanced baseline versus evolved comparison

The default run uses **3 matched seeds**, **8 worker processes**, and **20 SABRE
trials** for layout, layout-swap and routing. Each worker has one native thread.

```bash
.venv/bin/python -m compare_to_baseline_benchpress run
```

Results go to `output/compare_to_baseline_benchpress_balanced`. The previous
`output/compare_to_baseline_benchpress` results remain available. Repeat the
identical command to resume a compatible frozen collection; changed harness,
settings, dependencies or host require a new output directory.

## Balance and coverage

Eight families × five exact active logical widths (8, 16, 32, 64, 100) × three
structural templates produce 120 frozen inputs. Every input runs on all three
targets of its panel at both L2/L3. Every family gets 1/8, width 1/5, template 1/3,
target family 1/3 and level 1/2. Each main panel has 720 equal-weight cases.
**There is no combined score across panels.**

| Panel | Cases | Observations: 3 seeds × 2 builds |
| --- | ---: | ---: |
| General: line, grid, heavy-hex patch | 720 | 4,320 |
| IBM: FakeTorino, FakeSherbrooke, FakeWashingtonV2 | 720 | 4,320 |
| All-to-all controls | 240 | 1,440 |
| Near-capacity routing | 432 | 2,592 |
| Upstream application/chemistry/regression coverage | 52 | 312 |
| **Total** | **2,164** | **12,984** |

Synthetic core targets have exactly twice the circuit width, with CZ and
`rz/sx/x` and uniform absent calibration properties. Near-capacity targets at
32/64/100 qubits use physical width equal to logical width. IBM targets preserve
native basis, direction and frozen calibration: Torino 133/CZ, Sherbrooke 127/ECR,
Washington 127/CX. Washington is a historical 2022 snapshot, not live hardware.

See the [plan](../docs/balanced-benchmark-plan.md) and
[120-input list](../docs/balanced-benchmark-circuit-list.md). This is a balanced
scaling core; chemistry/QEC/natural-width applications remain separately reported
coverage. Missing large electronic-chemistry cells are explicitly disclosed.

## Freeze, inspect, smoke-test and run

```bash
.venv/bin/python -m compare_to_baseline_benchpress freeze --output output/balanced
.venv/bin/python -m compare_to_baseline_benchpress run --output output/balanced
.venv/bin/python -m compare_to_baseline_benchpress report --output output/balanced
```

Freeze prints exact case IDs. A small integration run is:

```bash
.venv/bin/python -m compare_to_baseline_benchpress run \
  --output output/balanced_smoke --core-only \
  --case general/F8/ghz_tree_n8/line_16/L2
```

Repeat `--case` to select more cases; filtered runs are explicitly smoke tests.
`--core-only` retains both complete main panels but omits supplemental coverage.
Explicit defaults are `--seeds 3 --workers 8 --trials 20 --master-seed 20261108`.

Both source checkouts must have compatible release-built native extensions.
Defaults are `../qiskit-evolved-main` and `../qiskit-evolved`. Source snapshots,
native hashes, dependencies, harness, typed circuit fixtures, target snapshots
and paired seeds are frozen. Both builds must import identical inputs/targets.
The collector does not rebuild or modify the source checkouts.

The process watchdog defaults to 600 seconds per observation. Failures/timeouts
are retained and never dropped or reweighted. There is no 90-minute promise.
Progress is saved after each completion in `collection_status.json`; raw results
are resumable. Ctrl-C cancels subprocess groups and retains completed records.
Failed attempts are archived on retry.

## Measurements and validation

Each observation compiles once. Generated inputs use C0 target compliance and
C6 routing replay. Post-init/layout/routing SWAP counts, widths, executed SABRE
passes and diagnostic stage timings are retained. C6 checks routing, not every
synthesis transformation. Generator semantics have separate small-instance tests.
Upstream corpus guards retain their upstream basis/connectivity checks and do
not claim C6 or full semantic equivalence. Parallel diagnostic times are not a
runtime comparison.

Baseline uses stock decay. Evolved explicitly uses decay plus wait time with
weight 0.0025 in both SABRE layout/routing. Other checkout differences can affect
results. Ratios are evolved/baseline arithmetic seed means; lower favors evolved.
Each panel geometrically aggregates its own frozen case weights. Missing or
undefined ratios invalidate its full score; a labeled positive subset is only
descriptive. Three seeds give limited precision, so reports make no confidence
interval or significance claim. Controls cannot dilute core scores.

## Legacy mode

The previous corpus selector remains available. To use its former protocol:

```bash
.venv/bin/python -m compare_to_baseline_benchpress run --design legacy \
  --output output/benchpress_legacy_new --seeds 1 --workers 6 --master-seed 20261107
```

Only legacy mode uses `--panel-size` (default 400). Upstream source is pinned to
`b695f30e83a32bac05b9b4d8e98d37ba9aae5236`. Balanced appendices reuse a verified
local archive when available; `--benchpress-source` can supply a Git mirror.
`qiskit-ibm-runtime==0.51.0` supplies local backend data; no IBM account is needed.

## Verification

```bash
.venv/bin/pytest tests/test_balanced_benchpress_*.py tests/test_compare_to_baseline_benchpress_*.py
.venv/bin/ruff check compare_to_baseline_benchpress tests/test_balanced_benchpress_*.py
```
