# Plan: widen the quality coverage of the iterations profile

Status: **proposal, nothing implemented.** Written 2026-09-27 against
`qiskit-transpile-bench` at commit `68dd8ac`, where both profiles are at version 6.

The costs, noise figures and check results in this plan were measured on the development
machine (Apple M1 Max) with the baseline Qiskit build, `2.6.0.dev0`. Section 10 describes
the measurements. They are planning numbers: the cluster must confirm them before a new
profile version is frozen.

**Contents:** [1 Summary](#1-summary) ·
[2 Scope](#2-scope) ·
[3 Where the profile stands](#3-where-the-iterations-profile-stands) ·
[4 Selection rules](#4-selection-rules) ·
[5 Recommended cases](#5-recommended-cases) ·
[6 Extras and trim order](#6-optional-extras-and-trim-order) ·
[7 How the cases are judged](#7-how-the-added-cases-are-judged) ·
[8 Effect on confirm](#8-effect-on-the-confirm-profile) ·
[9 Cost](#9-cost-estimate) ·
[10 Evidence](#10-evidence) ·
[11 Findings](#11-findings-that-need-a-decision) ·
[12 Work items](#12-work-items) ·
[13 Validation](#13-validation-before-freezing) ·
[14 Decisions and risks](#14-open-decisions-and-risks)

## 1. Summary

**Request.** The quality stage of `iterations-profile` takes about 15 minutes. Spend about
10 more minutes on circuits from `confirm-profile`, in the quality stage only, so that an
iteration run covers more than three circuits at one level.

**Recommendation.**

1. **Add 50 quality cases**, all of them existing confirm cases with existing fixtures:
   30 seed-sensitive *coverage* cases and 20 exact guards (section 5).
2. **Estimated cost: 9 to 10 minutes** on a 15-minute stage at a worker start-up cost of
   1 second, and 12 to 14 minutes at 4 seconds (section 9). Section 6 lists what to drop
   or add once the cluster has measured it.
3. **Coverage goes from 3 to 8 families**, from one level to four, from one size band to
   three, and from one topology class to two (section 5.4).
4. **Judge the coverage cases as a guarded panel, not as part of the objective**
   (section 7). The improvement test stays on the three 100-qubit circuits. This is
   decision D1 in section 14, and the case list does not depend on it.
5. **The confirm check pays for this.** The input groups that the loop sees with
   seed-sensitive cases grow from 3 to 11 of confirm's 38, and their share of the confirm
   score from 18.2% to 43.2% (section 8).

**Three findings change what can be added** (section 11):

| Finding | Consequence for this plan |
| --- | --- |
| The baseline assigns idle qubits in a random order on targets without error properties. The harness compares whole layouts, so the determinism audit and the routing replay fail on such cases | Those cases are excluded here. 26 confirm cases are affected, so a confirm session is at risk independently of this plan |
| The per-case cap of 1.05 ignores seed noise. Four of the recommended cases are noisy enough to trip it by chance | The coverage cases need a cap that also requires three standard errors |
| Small cases cost process start-up and serial checks, not compile time | The budget is driven by the number of cases and by output size. Large level-0 outputs are the most expensive additions |

## 2. Scope

**In scope.** The quality cases of the iterations manifest, the evaluator rules that judge
them, the report, the documentation and the profile versions.

**Out of scope.** The cost panels (`timing`, `timing-basis`, `preset`, `companion`), the
Clifford variants of C7, the C1 to C5 suite, the confirm workload and any new fixture.

"Quality only" holds under two conditions, and costs one thing elsewhere:

- **Cost panels.** The timing cases T11 to T19 and the preset cases are generated from
  the first nine iteration cases. New cases must be appended after them. The cost panels
  then do not change.
- **C7.** Curation attaches a Clifford variant to every case of the three loop circuits,
  and C7 selects its cases by that key. The added levels of those circuits must be
  generated without it. Otherwise C7 grows from 9 to 13 cases and has to verify at levels
  1 and 3.
- **Compile stage.** It round-trips about 20 more inputs per revision. This is the only
  added work outside the quality stage.

The quality stage compiles every case whose role is neither `timing` nor `memory`, so the
added cases need no new stage and no new command.

## 3. Where the iterations profile stands

### 3.1 Coverage today

| Dimension | Iterations today | Confirm scored panel |
| --- | --- | --- |
| Family | G1, G2, G3 (one circuit each) | G1 to G8 |
| Optimization level | 2 | 0, 1, 2, 3 |
| Size band | Large (100 qubits) | Small, medium, large |
| Topology | Heavy-hex distance 9 | Heavy-hex class and grid class |
| Native basis | `cz` scored, `cx` and `ecr` guarded | `cx` and `cz` scored, `ecr` guarded |
| Constraint form | Frozen `Target` | `Target` and loose basis list with coupling map |
| Occupancy | 100 of 193 qubits | Spare and fully occupied |
| Direction | Every coupling in both directions | Also maps with one direction per coupling: Melbourne, Sycamore, Tokyo |
| Compile methods | Default | Default and explicit SABRE |
| Input features | Numeric, no measurement, already in `cx` | Also symbolic parameters, measurements, matrix gates, high-level gates |
| Exact guards | Three canaries | 48 deterministic, 4 zero-baseline, 5 canaries |

The column for today describes the scored and guard cases. The three canaries add a
symbolic input, a loose target and level 3, as exact checks only.

### 3.2 Cost today

Work of the current quality stage for one revision, from the measurements of section 10:

| Part | Amount | Runs |
| --- | ---: | --- |
| Observations | 930 | |
| Compile and export of the outputs | 859 s | In worker processes, up to 9 at once |
| Routing-replay prefixes | 654 s | In worker processes |
| Worker processes | 111, plus 47 for the determinism audit | |
| C0 and C6 checks | 171 s | In the coordinator, serial under the GIL |

The model of section 9 turns this into about 6 minutes of wall time per revision. A
session that compiles both revisions (an A/A session, or the first session against a new
baseline) then takes 12 to 16 minutes, which matches the reported 15. All estimates in
this plan are a share of the current stage, converted to minutes on a 15-minute stage.
The share depends a little on the kind of session, so section 9 gives both kinds.

## 4. Selection rules

A confirm case was eligible only if it passed all of these at the baseline:

1. **It fills a gap.** It adds a family, level, size band, topology, basis, constraint
   form, occupancy or input feature that the loop does not yet see.
2. **It is deterministic under the harness's own comparison.** Repeated compiles give the
   same output and the same layout, idle qubits included (section 11.1).
3. **C0 verifies on its first ten seeds**, and C6 as well where the case is replayed.
4. **Its seed noise allows a per-case judgment**, or it is accepted knowingly with the
   noise-aware cap of section 7.
5. **It is affordable in wall time**, which means worker time and serial check time.
6. **It costs the confirm check as little as possible.** Cases of groups the loop already
   contains come first. New groups are added sparingly: one for each family the loop does
   not see, one small input each for G1 and G3, and one input on a square grid. Among
   equals, the group with the smaller confirm weight wins.

## 5. Recommended cases

Columns: `sd` is the standard deviation of `ln D2` over seeds 0 to 99 at the baseline.
*Confirm weight* is the share of the confirm score carried by the whole input group.
*Cost* is the estimate of section 9 at a 1-second worker start-up.

Each case keeps the circuit, target, level and options of its confirm twin. Its ID
changes only in the prefix, from `confirm/` to `iterations/`, so the SABRE variant keeps
its `+sabre` suffix. The coverage cases also change role, panel and weight (section 7).

### 5.1 Tier A: other levels of the loop's own circuits (4 cases, no new group)

| Input group | Family | Target | Levels | Baseline `D2` | `sd` | Confirm weight | Cost (min) |
| --- | --- | --- | --- | --- | --- | ---: | ---: |
| `square_heisenberg_n100` | G2 | `heavy_hex_d9_cz` | 1, 3 | 956, 353 | 0.084, 0.079 | 9.9% | 0.9 |
| `qaoa_ba_n100_3reps` | G3 | `heavy_hex_d9_cz` | 1, 3 | 1,644, 1,523 | 0.044, 0.048 | 4.2% | 2.4 |

They cover most of an open risk in section 12 of the implementation plan: levels 1 and
3 of the scored circuits are unguarded today. `qft_n100` at level 3 is the first extra
of section 6. They add no new input group.

### 5.2 Tier B: new input groups (26 cases, 8 groups)

One group for each family the loop does not see, G4 to G8. A second G5 input brings the
square grid. G1 and G3 get one small input each.

| Input group | Family | Target | Levels | Qubits | What it adds | `sd` | Confirm weight | Cost (min) |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: |
| `qft_cp_n14` | G1 | `melbourne_14_u` | 0 to 3 | 14 of 14 | Small band, one direction per coupling, fully occupied, loose form, legacy `u` basis, `cp` gates | 0.065 to 0.087 | 2.1% | 0.4 |
| `qaoa_complete_n8` | G3 | `mumbai_27` | 0 to 3 | 8 of 27 | Small band, complete graph, heavy-hex fragment with error properties | 0.044 to 0.097 | 2.1% | 0.5 |
| `qv_n27_d27` | G4 | `mumbai_27_loose` | 0 to 3 | 27 of 27 | Medium band, matrix gates, fully occupied, loose form | 0.047 to 0.054 | 1.6% | 1.8 |
| `multiplier_h18_n16` | G5 | `mumbai_27` | 0 to 3 | 16 of 27 | High-level arithmetic gate, very low noise | 0.017 to 0.024 | 1.0% | 0.9 |
| `ripple_adder_20` | G5 | `grid_7x7_u` | 0 | 42 of 49 | 7×7 grid, measurements, medium band. Level 0 only (section 11.1) | 0.076 | 1.6% | 0.1 |
| `bv_all_ones_n100` | G6 | `heavy_hex_d9_cz` | 0, 1, 2 | 100 of 193 | Star interaction, measurements | 0.034 to 0.053 | 4.2% | 0.6 |
| `su2_circular_n89` | G7 | `heavy_hex_d9_cz` | 0, 1 | 89 of 193 | Symbolic parameters, a ring that cannot embed | 0.027, 0.066 | 9.4% | 0.7 |
| `queko_bntf_54`, SABRE methods | G8 | `sycamore_54` | 0 to 3 | 54 of 54 | Explicit SABRE layout and routing, grid class, fully occupied, one direction per coupling | 0.134 to 0.213 | 3.1% | 0.7 |

Notes on three of them:

- **`su2_circular_n89`** is the most informative single addition and the most expensive
  one for the confirm check. It carries 9.4% of the confirm score and three quarters of
  family G7. Level 1 costs 0.1 s per compile. Level 2 costs 6 to 7 s because the layout
  search exhausts its budget, so it is left to section 6. Decision D2.
- **`queko_bntf_54`** is the noisy one. It is kept because explicit SABRE layout and
  routing on a fully occupied device is the path that a change of the SABRE search
  reaches most directly. It needs the noise-aware cap.
- **`multiplier_h18_n16`** has a standard error of about 0.3% per case. A change of 1%
  on this input alone is visible at three standard errors.

### 5.3 Tier C: exact guards (20 cases, 10 seeds each, no replay)

| Role | Cases | What a failure means |
| --- | --- | --- |
| Zero-baseline | `bvlike_n100` at levels 2 and 3; `qft16_cancel` at levels 2 and 3 | A cancellation was lost |
| Deterministic | `queko_bss_53` with default methods at levels 1, 2, 3; `queko_bntf_54` with default methods at level 2 | The perfect embedding is no longer found |
| Deterministic | `trotter_chain_n16` at levels 1, 2, 3; `ring_random_n14` at level 2 | Same, for a path and a ring. The chain also exercises high-level evolution gates |
| Deterministic | The six all-to-all controls (`a2a_*`) at level 2 | Synthesis or optimization changed where no routing exists |
| Canary | `su2_circular_n100` at levels 1 and 3 (level 2 exists) | The constant 300 moved |

Together they cost about 0.3 minutes. Each failure here would also fail the confirm run,
as a `CONSTRAINT_VIOLATION` or an `INCONCLUSIVE`.

### 5.4 Coverage reached

Counts are coverage cases, 30 in total. The three primary cases are not included.

| Dimension | Values covered (cases) |
| --- | --- |
| Family | G1 (4), G2 (2), G3 (6), G4 (4), G5 (5), G6 (3), G7 (2), G8 (4) |
| Level | 0 (8), 1 (9), 2 (6), 3 (7) |
| Size band | Small (12), medium (9), large (9) |
| Topology | Heavy-hex class (21), grid class (9) |
| Native basis | `cx` (21), `cz` (9) |
| Constraint form | `Target` (17), loose (13) |
| Fully occupied | `qft_cp_n14`, `qv_n27_d27`, `queko_bntf_54` |
| One direction per coupling | `qft_cp_n14` on Melbourne, `queko_bntf_54` on Sycamore |

### 5.5 Left out, and why

| Left out | Reason |
| --- | --- |
| Every medium input on Rochester, the 7×7 grid at levels 1 to 3, `qv_n14_d14_s10`, `qft_cp_n8` | Excluded by the rule of section 11.1. Most of them showed a changing layout in the probe |
| `qft_n100` at levels 0 and 1 | Outputs of 190,000 and 100,000 operations. They cost 5 and 3 minutes, most of it in the serial checks |
| `su2_circular_n89` at levels 2 and 3 | 6 to 7 s and 34 to 40 s per compile |
| `qv_n50_d50`, `qaoa_complete_n32`, `qft_full_n64` | 12, 7 and 5 minutes |
| `queko_bigd_20` | `D2` is near 20 at levels 2 and 3, so one layer is 5%. Its default-method layout is also non-deterministic |
| A line target, a large quantum-volume input | Not in the confirm workload either |

## 6. Optional extras and trim order

Measure the recommended set first (section 13), then use this list.

**Add, in this order, if the stage has time left:**

| Extra | Cost (min) | What it adds |
| --- | ---: | --- |
| `qft_n100` at level 3 | 1.8 | Level 3 for the third loop circuit, no new group |
| `su2_circular_n89` at level 2, 20 seeds | 1.0 | The heaviest confirm case of G7, as a per-case guard outside the panel. Its `sd` is 0.055, so with the noise-aware cap it trips at about 5% |
| `qv_n14_d14` at levels 0 to 3 | 0.7 | A `Target` with error properties and one direction per coupling. A new group with 3.1% |
| `bv_all_ones_n100` at level 3 | 0.8 | Completes the levels of G6 |
| `square_heisenberg_n100`, `qaoa_ba_n100_3reps` at level 0 | 1.3 each | Routing alone on a trivial layout, large band |

Every extra needs the checks of section 4 before it is used. `su2_circular_n89` at
level 2 and `bv_all_ones_n100` at level 3 have not had the two-process determinism probe.

**Drop, in this order, if the stage runs long:** `qaoa_ba_n100_3reps` at level 3
(1.5 minutes), then at level 1 (0.9), then `qv_n27_d27` at levels 3 and 2 (0.5 each).
The exact guards are not worth dropping: all 20 cost 0.3 minutes.

**A lever: seeds per coverage case.** A guard panel may use fewer seeds than a scored
panel, as long as all its cases use the same number.

| Seeds per coverage case | Recommended set | With every extra above | Standard errors |
| ---: | ---: | ---: | --- |
| 100 | 9.2 min | 16.0 min | As stated in this plan |
| 50 | 5.0 min | 9.1 min | 1.4 times larger |

The recommendation is 100 seeds: it keeps the per-case checks sharp and matches the
seed block of confirm. Use 50 seeds if the measured stage is over budget, or to buy the
extras within the 10 minutes. Decision D6.

## 7. How the added cases are judged

### 7.1 Recommended: a guarded coverage panel

The 30 coverage cases get role `guard` and a new panel, `coverage`. The objective does not
change: `IA2/improvement` stays on the three `cz` cases at level 2.

| Rule | Record | Requirement |
| --- | --- | --- |
| Panel guard | `IA3/coverage/D2`, `IA3/coverage/N2` | `ln(score) <= 3·SE`, like the basis panels |
| Per-case cap, noise-aware | `IA4/cap/<case>/<metric>` | Fails only when the ratio exceeds 1.05 **and** `ln(ratio) > 3·SE_case` |
| Exact rules | `IA4/exact/<case>` | Unchanged, for the 20 exact cases |
| Routing replay | `IA1/C6` | The first ten seeds of each coverage case, the scope a guard without a panel has today. Every seed of the primary and basis cases, as today |
| Report only | Coverage score with its `2·SE` bound; family and level summaries; families improved out of 8 | Never part of the verdict |

**Weights.** The confirm tree, applied to the 30 cases: each family gets 1/8, then level,
size band, topology, basis, input group and variant. The weights are frozen in the
manifest. The largest are 1/16, for the two Heisenberg cases and the two
`su2_circular_n89` cases.

**What the guard detects.** For `D2` the panel's standard error is 0.13% as measured on
three candidates, and at most 0.23% if the candidate's results were independent of the
baseline's. The guard then trips on a regression of 0.4% to 0.7% across the panel. For
`N2` the standard error is about half of that. A per-case check trips at 5%, or at three
standard errors for the noisy cases.

**False rejection of a neutral candidate.** About 0.3% from the two panel guards and about
0.7% from the 60 per-case checks. With the plain 1.05 cap the per-case figure would be
about 12%, almost all of it from the four `queko_bntf_54` cases.

**Why a guard and not the objective.**

- An iterations `PASS` keeps its meaning, and earlier results stay comparable.
- A guard puts little selection pressure on the added inputs, so the confirm run stays
  close to an independent check of them.
- The report still shows how broad a gain is, which is what a candidate ranking needs.

### 7.2 Alternative: make the coverage panel part of the objective

Give the 30 cases role `scored` and one weight tree together with the three primary
cases. The evaluator then needs no new panel, only the noise-aware cap. The role brings
two costs of its own. Every scored case replays all 100 seeds, which raises the estimate
from 9.2 to 13.0 minutes. The stage-coverage rule then applies to 33 cases instead of 3.

| | Guarded panel (recommended) | Part of the objective |
| --- | --- | --- |
| Loop optimizes | The three circuits, without harming the rest | The broad average |
| Chance to pass confirm's breadth rule | Depends on the gain generalizing | Higher, the loop rewards breadth |
| Meaning of an iterations `PASS` | Unchanged | New claim, history not comparable |
| Confirm as an independent check | Mostly kept | 43% of its weight is tuned on |
| Standard error of the objective | About 0.4% to 0.6%, as today | About 0.1% to 0.2% |

Choose this if candidates keep failing confirm on breadth rather than on regressions.

### 7.3 Not recommended

- **Per-case guards without a panel.** No evaluator change, but each case adds two
  guards at three standard errors and a plain cap. A neutral candidate would be rejected
  in about one run of five.
- **The 133 scored confirm cases on ten seeds.** About 12 minutes, so a similar cost. It
  leaves no input held out, and per-case checks become meaningless.

## 8. Effect on the confirm profile

The implementation plan rests on one rule: the check profile must be mostly circuits the
loop does not contain. This plan spends part of that margin.

| | Today | With this plan |
| --- | ---: | ---: |
| Confirm input groups with seed-sensitive cases in the loop | 3 of 38 | 11 of 38 |
| Their share of the confirm score | 18.2% | 43.2% |
| Held-out groups | 35 | 27 |
| Held-out share of the confirm score | 81.8% | 56.8% |
| Confirm weight on the very cases the loop compiles | 5.2% | 32.2% |

By family, the loop's share of the confirm weight becomes G1 50%, G2 79%, G3 50%,
G4 12.5%, G5 21%, G6 33%, G7 75% and G8 25%. G2 and G7 are concentrated in confirm itself:
one input carries most of each family.

The exact guards are not counted as exposure, today or with this plan. They only pass or
fail, so they give no score to tune on. Four more confirm groups enter the loop in this
way only: `trotter_chain_n16`, `ring_random_n14`, `queko_bss_53` and, as today,
`su2_circular_n100`. Counting them, the loop touches 4 groups today and 15 with this
plan, which carry 19.3% and 49.5% of the confirm score.

**Two changes keep the drift visible:**

1. List all 11 groups in the policies, next to the three scored groups. Both lists feed
   report-only numbers.
2. The confirm run computes two leave-out scores, without the 3 scored groups and
   without all 11 loop groups, and prints both. Today the first one is computed and
   stored in `decision.json`, but `report.md` does not show it.

## 9. Cost estimate

### 9.1 Model

```text
wall time per revision = (compile + export + replay + processes × start-up) / 9  +  checks
```

Nine is the number of seed batches in flight. The checks are serial. A parallelism of 6
instead of 9 changes the results by less than 0.4 minutes.

The inputs have different sources. Compile times are means over the measured seeds, taken
while six probe workers ran. Prefix times come from five seeds. Export and check times
come from one compile per case on a quieter machine. The checks run on the pool threads
and overlap the workers, so adding them in full is an upper bound for small start-up
costs.

### 9.2 Recommended set, per revision

| | Current | Added |
| --- | ---: | ---: |
| Cases | 12 | 50 |
| Observations | 930 | 3,200 |
| Compile and export | 859 s | 543 s |
| Routing-replay prefixes | 654 s | 38 s |
| Worker processes, audit included | 158 | 360 |
| Serial checks | 171 s | 113 s |

Added time on a 15-minute stage:

| Worker start-up cost | Both revisions compiled | Baseline rows from the store |
| ---: | ---: | ---: |
| 0.5 s | 8.5 min | 8.9 min |
| 1 s | 9.2 min | 9.8 min |
| 2 s | 10.3 min | 11.4 min |
| 4 s | 12.4 min | 14.2 min |

With the baseline in the store, one revision is compiled and both are audited, so the
audit weighs more. By tier, at 1 second and both revisions compiled: tier A 3.3 minutes,
tier B 5.6, tier C 0.3. The per-case costs in sections 5 and 6 use the same setting.

Two checks of the model move the 1-second figure by less than a minute:

| Variation | 0.5 s | 1 s | 2 s | 4 s |
| --- | ---: | ---: | ---: | ---: |
| Checks overlap the workers completely | 7.2 | 8.5 | 10.6 | 14.1 |
| Compile times as the harness records them, 1.65 times the probe's | 7.8 | 8.3 | 9.3 | 11.0 |

The second row exists because the harness's own builds compile more slowly than the
build the probe used. In the recorded A/A session the compiles of the current cases took
1.2 to 2.2 times as long. The harness builds Qiskit with thin link-time optimization,
which may explain it.

### 9.3 What drives the cost

- **Process start-up.** The added cases are small. For tiers B and C, starting the
  workers costs as much as compiling once a start takes more than about 0.6 s. On this
  machine, with a local disk, a start takes about 0.5 s for a small input and about 1 s
  for a 100-qubit input, the input load included. A shared filesystem on the cluster may
  be several times slower. This is the largest uncertainty in the estimate.
- **The determinism audit** recompiles 5% of all observations, one process each: 47
  today, 207 with this plan. It audits the baseline half as well when those rows come
  from the store.
- **Output size.** The C0 check costs about 5 microseconds per output operation and runs
  serially. This is why large level-0 outputs are expensive.

### 9.4 Two optional optimizations

Both leave every quality result unchanged. Together they bring the estimate at a
4-second start-up from 12.4 to 9.9 minutes, with both revisions compiled.

1. One batch of 100 seeds for cheap cases, instead of four batches of 25.
2. Group the audit recompiles by case, instead of one process per observation.

The first one has constraints. The policy schema and a test cap the batch size at 25.
The batch size is part of the key of the stored baseline rows, so a change recomputes
them, and a change in one profile only ends the sharing of rows between the profiles.

Decide on them after the measurement of section 13. They are not needed at a start-up
cost of 2 seconds or less.

## 10. Evidence

All measurements used the harness's own adapter, fixtures and metric code. They wrote
their results to a scratch directory and changed no source, fixture or profile. The probe
script and its summary are
`design/probes/iterations_coverage_probe.py` and
`design/probes/iterations_coverage_probe.json`.

| Measurement | Size | Result |
| --- | --- | --- |
| Baseline quality of the confirm workload | 209 cases: 150 on 100 seeds, 57 exact cases on 10 seeds, 2 slow guards on 3 seeds | Per-case `D2`, `N2`, noise and compile time |
| Reproduction of the harness | `qft_n100`, seed 0 | `D2` 1,983 and `N2` 9,835, equal to the recorded A/A observation |
| Determinism, confirm cases | 206 cases, 3 seeds × 3 repeats in one process | 26 cases with a changing layout (section 11.1). No output circuit ever changed |
| Determinism, the 50 recommended cases | 8 seeds × 5 compiles, two processes, `PYTHONHASHSEED` 0 and 1 | Identical outputs and layouts everywhere |
| C0 and C6, recommended coverage cases and extras | 41 cases × 10 seeds | 410 of 410 verified |
| C0 and constants, the 20 exact guards | 20 cases × 10 seeds | 200 of 200 verified. Every case gave one value on all seeds |
| Check and export time | One compile per case | About 5 and 8 microseconds per operation |
| Known-outcome check | The evolved tree with its default rule, 15,519 compiles | Exact tie with the baseline on every one |
| Three candidate rules | The evolved tree, whole workload, 100 seeds | Section 10.1 |

### 10.1 Does the coverage panel predict the confirm result?

The evolved tree in this repository selects a SABRE trial by a rule named in
`QISKIT_SABRE_SELECT`. Each rule was compiled on the whole confirm workload and judged
with the harness's estimator. These are experimental rules, used here only as realistic
candidates.

`D2` ratio, evolved over baseline:

| Candidate rule | Primary, 3 cases | Coverage panel, 30 cases | Confirm, scored panel | Confirm, 27 held-out groups |
| --- | ---: | ---: | ---: | ---: |
| `depth` | 0.910 | 0.937 | 0.932 | 0.932 |
| `slack:10` | 0.915 | 0.950 | 0.941 | 0.940 |
| `product` | 0.920 | 0.945 | 0.939 | 0.939 |

`N2` ratio:

| Candidate rule | Primary, 3 cases | Coverage panel, 30 cases | Confirm, scored panel | Confirm, 27 held-out groups |
| --- | ---: | ---: | ---: | ---: |
| `depth` | 1.043 | 1.026 | 1.030 | 1.029 |
| `slack:10` | 1.034 | 1.016 | 1.012 | 1.007 |
| `product` | 1.015 | 1.008 | 1.007 | 1.005 |

The confirm columns use 132 of the 133 scored cases. `su2_circular_n89` at level 2 was
compiled for `depth` only. With it, the confirm score of `depth` is 0.934 for `D2` and
1.029 for `N2`.

The coverage cases are themselves confirm cases and carry 27% of its weight, so part of
the agreement with the scored panel is by construction. The held-out column shares no
input with the loop and is the fair comparison.

Reading:

- **The three-circuit score overstates the effect on the suite** by 2 to 3 points of
  depth, and the gate-count loss by 1 to 2 points.
- **The coverage panel is within one point of the confirm score**: 0.5 to 0.9 points for
  `D2` and 0.1 to 0.4 for `N2`. Against the held-out groups the gaps are 0.4 to 1.0 and
  0.3 to 1.0 points.
- **The coverage panel ranks the three candidates as confirm does.** The primary panel
  swaps `slack:10` and `product` on depth. Three candidates are weak evidence, so
  section 13 repeats the comparison.
- **Level summaries are usable.** They differ from confirm by up to 0.6, 2.6 and 1.3
  points of `D2` for the three rules, and by at most 1.0 point of `N2`.
- **Family summaries agree in sign, not in size.** For `D2` all eight families have the
  sign they have in confirm, for all three rules. For `N2` seven do, and G3 is not
  distinguishable from zero in the coverage panel for two of the rules. With two to six
  cases per family the size differs by up to 7 points. This is a reason to report the
  family summaries and not to guard them.
- **All three rules trade gate count for depth.** They already fail the `N2` guard of
  today's primary panel, so they do not show a case that only the coverage panel
  catches. With `depth`, 26 of 150 confirm cases exceed the 1.05 cap on `N2`, and 5 of
  the 30 coverage cases do.

## 11. Findings that need a decision

### 11.1 Idle qubits get a random layout at the baseline

When a VF2 layout pass finds a solution on a target without error properties, the idle
qubits land in an order that changes from one compile to the next. The output circuit and
the layout of the used qubits do not change. The difference was traced to `VF2PostLayout`
in three cases and to `VF2Layout` in `queko_bigd_20`. Targets with error properties
showed no difference.

The harness compares the whole layout in three places:

- **The determinism audit** requires the same layout on a recompile. A difference makes
  `audit/determinism` `unresolved`, and with it every quality verdict. It also
  invalidates the stored baseline quality of every case in the manifest.
- **The routing replay** requires the full compile and the routing prefix to agree on
  the layout at levels 0 to 2. A difference is recorded as a `mismatch`, which is a failed
  correctness record.
- **The API contract checks** compare the layouts of repeated runs. Their 60 cases use
  targets with error properties, so they are not exposed today.

The audit samples 5% of the observations, so it fails by chance. The replay does not:
in the probe, 20 of 300 replayed observations mismatched on two seeds per case. A confirm
session replays every scored seed, so it would record mismatches almost certainly.

In the probe, 26 confirm cases showed it, all on loose targets at levels 1 to 3 with at
least two idle qubits. 23 of them are scored and carry 13.5% of the confirm score. The
other three are `queko_bigd_20` with default methods, where the idle qubits are declared
circuit qubits that carry no gate. The probe is a lower bound: it used three seeds and
repeated the compiles inside one process, while the audit uses fresh processes and two
hash seeds. Another 18 confirm cases meet the same condition and showed no change.

| Option | Effect |
| --- | --- |
| Exclude such cases from the iterations profile | Used by this plan. No harness semantics change. Costs the medium band on Rochester and the 7×7 grid above level 0 |
| Compare layouts on the used qubits only | Opens those cases. Needed for the confirm profile in any case. It changes what three checks accept, so it needs its own review |

Decision D4. The rule behind the exclusion: a loose target at levels 1 to 3 is accepted
only when the circuit fills the device. The rule is stricter than the evidence. The two
`long_2q_sequence` canaries of today's profile break it, and they were stable in the
probe and in the recorded A/A sessions.

### 11.2 The plain cap trips on noise

A case whose `ln D2` has a standard deviation of 0.2 over seeds has a standard error of
about 2.8% on its ratio. A neutral candidate then exceeds 1.05 in about 4% of runs.

The table counts per-case caps only, for a neutral candidate. The panel guards add
about 0.14% each.

| Cases | Expected false breaches per run | Chance of at least one |
| --- | ---: | ---: |
| The 9 scored and basis cases of today's iterations profile | Below 0.001 | Below 0.1% |
| The 30 added coverage cases, plain cap | 0.12 | About 12% |
| The 30 added coverage cases, noise-aware cap | 0.007 | About 0.7% |
| The 152 scored and guard cases of confirm, plain cap | 0.70 | About 50% |

These figures assume that the candidate's result at a seed is independent of the
baseline's. A candidate that changes only which trial is kept stays correlated with the
baseline, which usually lowers the rate.

Decision D3. The confirm figure is outside this plan, and it deserves the same rule.

### 11.3 Smaller observations

- `mcx_kg24_n5` is scored in confirm at levels 1 to 3, where it gives `D2` 14 and `N2` 15
  on every seed. It carries weight and no information.
- `multiplier_h18_n20` compiles in 0.2 s. The 49 s belonged to `hwb12`, which it
  replaced. It could use the whole seed block in confirm.

## 12. Work items

High level, in order. None is started.

| # | Item | Where |
| --- | --- | --- |
| 1 | Generate the 50 cases from the existing catalog rows and append them after the first nine iteration cases | `tools/curate/curate.py` |
| 2 | Compute the coverage weights with the confirm tree and freeze them in the manifest | `tools/curate/curate.py` |
| 3 | Add the `coverage` panel guard, the noise-aware cap and the report-only summaries | `src/qtb/evaluator/__init__.py` |
| 4 | Replay the first ten seeds of a coverage case | `routing_replay_seeds` in `src/qtb/coordinator/__init__.py` |
| 5 | Add the list of loop groups and the cap rule to the policy and its schema. Compute the second leave-out score | `policy.json`, `src/qtb/config/schemas/policy.json`, evaluator |
| 6 | Add the coverage section to the iterations report, and both leave-out scores to the confirm report | `src/qtb/reporter/__init__.py` |
| 7 | Bump both profiles to version 7 | `curate.py` sets the version in two places |
| 8 | Update the case counts, the rule tables and the budgets | `README.md` and, under `docs/`: `iterations-profile.md`, `confirm-profile.md`, `metrics.md`, `versioning.md`, `verifier.md`, `validation.md`, `output-format.md`, `workflow/quality.md` |
| 9 | Tests: frozen weights, guard and cap outcomes on synthetic observations, replay scope, completeness count, the version | `tests/test_profiles_evaluation.py`, `test_statistics.py`, `test_worker_storage.py`, `test_policy_protocol.py`, `test_timing_metadata.py`, `test_stages.py` |
| 10 | Optional: per-case batch size and grouped audit (section 9.4) | Coordinator, policy schema |

**Points that are easy to get wrong.**

- **Do not extend the `ITERATIONS` list** in `curate.py` to the new groups. It also
  drives the Clifford variants, the basis guards and the levels of the confirm timing
  panel. The added cases need a list of their own.
- **Leave the Clifford variant out** of the four tier A cases (section 2).
- **Give every coverage case a weight above zero.** `make_case` sets 0.0, and only scored
  cases get a weight afterwards. A zero weight makes the panel estimator raise an error.
- **Keep the replay scope of a coverage case equal to that of a guard without a panel.**
  The panel is not part of the key of a stored row, and a stored row carries its checks.
- **Keep the evaluator usable on archived sessions.** `decide` judges version 6 sessions
  with the current evaluator. Read the new policy keys with defaults, and create the
  coverage records only when the manifest has coverage cases.
- **Edit the checked-in schema by hand.** `tools/generate_schemas.py` is out of date.

**Side effects to expect.**

- The harness code changes, so the store recomputes the baseline quality once, for both
  profiles. For confirm that is about 15,000 compiles.
- The harness wheel changes, so the stored baseline build gets a new environment from
  the stored Qiskit wheel, and the baseline correctness and unit-test results are
  computed again.
- A session compiled before the change cannot run further stages with the new harness.
  `decide` still works on it.
- The 30 coverage cases do not share stored baseline rows with their confirm twins,
  because the role is part of the key. The 20 exact guards do share them.
- The fixtures do not change. Curation must reproduce every existing fixture hash.
- The confirm manifest changes in its version only. Its policy gains the list of loop
  groups.

## 13. Validation before freezing

1. **A/A session on the cluster** with the new manifest. Every paired difference must be
   zero and the audit must pass. Once correctness and cost have finished, the verdict
   must be `NO_IMPROVEMENT`.
2. **Read the stage time** from `stages/quality/progress.log`, per batch: compile, routing
   prefixes and checks. Compare it with section 9 and apply section 6.
3. **Measure the worker start-up cost** on the cluster. It decides section 9.4.
4. **Repeat the determinism probe on the cluster** for the 50 cases, with more seeds.
   The probe here ran on macOS.
5. **Known-outcome tests** on synthetic observations with seed noise:
   - a quiet coverage case that regresses by 8% gives `CONSTRAINT_VIOLATION`;
   - the whole panel regressing by 1% gives `CONSTRAINT_VIOLATION`;
   - a neutral candidate with independent noise on every case passes every coverage
     check, also on the noisy cases.
6. **One real candidate** through the new profile, then through confirm. Compare the
   coverage score with the confirm score, as in section 10.1.

## 14. Open decisions and risks

| # | Decision | Recommendation |
| --- | --- | --- |
| D1 | Coverage cases as a guarded panel, or as part of the objective | Guarded panel (section 7) |
| D2 | `su2_circular_n89` in the loop, at 9.4% of the confirm score | Include it at levels 0 and 1. To keep it held out, use `su2_circular_n100` at level 0 instead, at 1.0% |
| D3 | Noise-aware cap for the coverage cases | Adopt it. Without it, replace `queko_bntf_54` with SABRE methods by its default-method case at level 0 |
| D4 | Exclude cases with a random idle-qubit layout, or compare layouts on used qubits | Exclude now. Fix the comparison separately, because confirm needs it |
| D5 | Family and level summaries as guards | Report only. With two to six cases per family they are too rough to reject on |
| D6 | 100 or 50 seeds per coverage case | 100. Switch to 50 to stay within budget or to afford the extras (section 6) |

| Risk | Mitigation |
| --- | --- |
| The stage takes longer than estimated because workers start slowly on the cluster | Sections 13.2 and 13.3, then section 6 or 9.4 |
| A case is non-deterministic on Linux although it was stable here | Section 13.4 before freezing. Exclude the case, never the seed |
| More guards reject more neutral candidates | About 1% added. The report states the number of guards |
| Candidates get ranked on the coverage score | The 11 groups are declared as loop groups, and confirm reports the score without them |
| The coverage panel is read as a confirm verdict | The report labels it as an estimate. Family summaries are shown with their case counts |
| The baseline changes and a constant moves | The deterministic guards compare with the baseline seed by seed. Only the two new canary levels and the four zero-baseline cases hold a constant |
