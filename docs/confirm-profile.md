# The confirm profile

[Documentation index](README.md)

`confirm-profile` is the **broad check**. Run it once, when a change looks good on the
[iterations profile](iterations-profile.md), to find out whether the gain holds beyond the
three circuits you tuned on.

```bash
Q="uv run qiskit-transpile-bench"
S="$HOME/qtb-sessions/idea1-confirm"   # a new session directory

$Q compile     --baseline /path/to/baseline --evolved /path/to/evolved \
               --profile confirm-profile --store "$HOME/qtb-store" --results-root "$S"
$Q quality     --results-root "$S"
$Q correctness --results-root "$S"      # skipped unless the quality gate is open
$Q unit-tests  --results-root "$S"      # optional; if you run it, it counts
$Q cost        --results-root "$S"      # skipped unless correctness also passed
$Q decide      --results-root "$S"
```

The profile is chosen once, on `compile`; the later stages read it from the session. See the
[README](../README.md) for the stages, the store and the exit codes.

Files: `profiles/confirm-profile/manifest.json` and `profiles/confirm-profile/policy.json`.
Version 7 ([what changed](#version-7)).

> **Do not tune on this profile.** The workload is public and fixed, with no held-back part.
> Editing a candidate and rerunning confirm until it passes turns it into a second tuning
> loop. The harness does not count how often a workload has been decided, so keeping to
> this is up to you.

## What a `PASS` claims

Native two-qubit depth improved across eight circuit families, optimization levels 0–3,
heavy-hex and grid-class targets, and the `cx` and `cz` bases (`ecr` guarded), on the fixed
workload drawn from Qiskit's in-tree benchmark suite (`test/benchmarks/`), within the gate
count, compile time, memory and correctness limits. It says nothing about circuits outside
this workload.

## Workload: 248 cases

| Role | Count | Purpose |
| --- | ---: | --- |
| Scored | 130 | 38 input groups × levels × targets; 100 seeds each; weights sum to 1 |
| Guard | 19 | Per-case regression guards, including the `cx`/`ecr` basis guards of the three 100-qubit circuits |
| Deterministic | 51 | Seed-blind cases compared exactly, seed by seed; any increase fails |
| Zero-baseline | 4 | Cases whose baseline `D2` = `N2` = 0; any non-zero value fails |
| Canary | 5 | Constant cases that must equal a frozen value |
| Timing | 30 | T1–T19 and the preset panel from the iterations profile, plus an 8-case confirm timing panel |
| Memory | 9 | Peak RSS of one compile in a fresh process |

Quality work per revision is about 15,400 compiles.

### Families

Every family carries exactly 1/8 of the score.

| ID | Family | Scored input groups |
| --- | --- | --- |
| G1 | Quantum Fourier transform | `qft_n100`, `qft_full_n32`, `qft_full_n64`, `qft_cp_n8`, `qft_cp_n14` |
| G2 | Hamiltonian simulation | `square_heisenberg_n100`, `dtc_n100`, `trotter_chain_n16`, `trotter_chain_n32` |
| G3 | QAOA | `qaoa_ba_n100_3reps`, `qaoa_complete_n8`, `qaoa_complete_n16`, `qaoa_complete_n32` |
| G4 | Quantum volume | `qv_n50_d50`, `qv_n50_d20`, `qv_n27_d27`, `qv_n14_d14`, `qv_n14_d14_s10` |
| G5 | Reversible logic and arithmetic | `ripple_adder_10`, `ripple_adder_20`, `multiplier_h18_n16`, `multiplier_h18_n32`, `adder_modular_v17_n8/n16/n32`, `mcx_kg24_n5/n6/n16` |
| G6 | Bernstein–Vazirani | `bv_all_ones_n16`, `bv_all_ones_n50`, `bv_all_ones_n100` |
| G7 | Variational ansatz | `su2_circular_n89`, `su2_circular_n100`, `ring_random_n8`, `ring_random_n14` |
| G8 | Routing challenges (QUEKO) | `queko_bigd_20`, `queko_bss_53`, `queko_bntf_54`, with explicit SABRE layout and routing |

Six RevLib/HWB inputs from the original design had unclear redistribution terms. They were
replaced by generated reversible circuits from Qiskit's `test/benchmarks/utils.py`; the manifest
records the substitutions under `replacements`. See [fixture provenance](../fixtures/PROVENANCE.md).

### Coverage dimensions

| Dimension | Values in the scored panel |
| --- | --- |
| Optimization level | 0 (38 cases), 1 (31), 2 (31), 3 (30) |
| Size band (logical qubits) | small 4–16 (56), medium 17–64 (53), large 65–100 (21) |
| Topology | heavy-hex (100), grid (30) |
| Native 2-qubit basis | `cx` (101), `cz` (29) |
| Constraint form | frozen `Target` (67), loose basis list + coupling map (63) |
| Targets | `mumbai_27`, `heavy_hex_d9_cz`, `rochester_53`, `melbourne_14_u`, `mumbai_27_loose`, `melbourne_14` (directed), `rochester_53_u`, `grid_5x5_u`, `grid_7x7_u`, `tokyo_20`, `sycamore_54` |

Inputs whose interaction graph is a path or ring (Trotter chains, DTC, random rings, circular
SU2 at 100 qubits) embed perfectly at levels 1–3, so every seed gives the same result there.
They are scored at level 0 only and are deterministic guards at levels 1–3. So is
`mcx_kg24_n5`, which gives `D2` 14 and `N2` 15 on every seed at levels 1–3.

### Weights

The weights form a tree. Each family gets 1/8. Inside a family, weight is split equally by
level, then size band, then topology, then basis, then input group, then variant. Adding more
inputs to one family therefore cannot outweigh the other seven. Two consequences are stated
openly: `square_heisenberg_n100` carries about 9.9% of the score and `su2_circular_n89` about
9.4%, because each is the only seed-sensitive input of its family at some levels. The
iterations profile's three circuits together carry 18.2%.

### Guard-only and exact cases

- **Deterministic** (51): QUEKO with default methods (VF2 finds the perfect embedding), the
  path- and ring-shaped inputs and `mcx_kg24_n5` at levels 1–3, and six all-to-all Clifford +
  `rz` controls (`a2a_*` on `a2a_clifford_rz_16`). Compared exactly on 10 seeds.
- **Zero-baseline** (4): `bvlike_n100` and `qft16_cancel` at levels 2–3, where every
  entangling gate cancels.
- **Guards** (19): the three 100-qubit circuits on `cx`/`ecr`, `su2_circular_n89` at level 3
  (10 seeds), `multiplier_h18_n20` (100 seeds, the `hwb12` replacement), QUEKO at level 0,
  and others. Every guard must stay at or below the 1.05 per-case cap. Guards outside the
  basis panels must also satisfy `ln(ratio) ≤ 3·SE_case`.
- **Canaries** (5): `su2_circular_n100` and `long_2q_sequence` at several levels.

### Cost panels

| Panel | Cases | Mode |
| --- | --- | --- |
| `timing` | T1–T19, unchanged from the iterations profile | `timing_e2e` / `timing_reuse` |
| `preset` | Preset construction on the three heavy-hex targets | `preset_build` |
| `confirm-timing` | 8 cases, one large end-to-end case per family: `qft_n100` L3, `square_heisenberg_n100` L0, `qaoa_ba_n100_3reps` L3, `qv_n50_d50` L2, `multiplier_h18_n32` L0, `bv_all_ones_n100` L3, `queko_bss_53` L2, `su2_circular_n89` L0 | `timing_e2e`, seed 0 |
| `memory` | 9 cases, the heaviest scored input of each family plus `multiplier_h18_n20` at level 2; 3 fresh processes each | `memory` |
| `companion` | The level-2 scored case of `companion_groups`: `square_heisenberg_n100` (G2), `multiplier_h18_n32` (G5), `bv_all_ones_n100` (G6), `queko_bss_53` (G8), over `companion_seeds` 0–9 (only when layout or routing changed) | `timing_reuse` |

The confirm-timing and companion selections are provisional: the companion now samples four
families instead of eight, and confirm-timing one case per family instead of 20. Both must be
validated on the cost host (A/A noise, and a layout/routing slowdown in a retained and an
omitted family) before they are frozen (design/updated_confirm_plan.md, section 6 and V6).

## Acceptance rules (CA1–CA6)

The broad rules replace the iterations rules. A confirm `PASS` does **not** also require the
three-circuit iterations score to improve.

| Rule | Records | Requirement |
| --- | --- | --- |
| CA1 correctness | `CA1/C0`, `CA1/C1-C5`, `CA1/C6`, `CA1/C7`, `CA1/C1-lite`, `CA1/stage-coverage`, and `CA1/upstream` once `unit-tests` has started | As IA1, plus **C1-lite**: exact all-zero-input state or measurement checks on the first 10 seeds of every scored case with at most 25 logical qubits and no free parameters. A pair is required when the baseline's simulated wire union is at most 22 (`c1_lite_max_width`); a wider or missing evolved output leaves it unresolved. C7 on 5 seeds (`clifford_seeds`) in prefix and full mode |
| CA2 improvement | `CA2/improvement` | `ln(D2 score) + 2·SE < ln(0.99)`, a practical 1% reduction beyond seed noise |
| CA3 breadth | `CA3/breadth` | At least 4 of the 8 families individually satisfy `ln(D2) + 2·SE < 0`, **and** removing any one family still leaves the score below 1 |
| CA4 guards | `CA3/primary/N2`, `CA3/cx/*`, `CA3/ecr/*`, `CA4/family/<G>/<metric>`, `CA4/optimization_level/<L>/<metric>`, `CA4/cap/...`, `CA4/exact/...` | Every family and level summary has `ln(score) ≤ 3·SE` for `D2` and `N2`, and so does overall `N2`. Per-case caps (1.05). Deterministic, zero-baseline and canary cases exact. Band, topology and basis summaries are reported only |
| CA5 cost | `CA5/timing`, `CA5/timing-basis`, `CA5/confirm-timing`, `CA5/memory`, `CA5/preset`, `CA5/companion` | Each panel's ratio at most 1.10, and no per-case breach (> 20% and by more than 25 ms, or 32 MiB for memory). Timing panels screen with 4 rounds and measure 10 in full ([iterations-profile.md](iterations-profile.md#timing-panels)) |
| CA6 completeness | `CA6/completeness` | Every observation present; determinism audit passed |

Required IDs in `policy.json`: `harness/roundtrip`, `baseline/preflight`,
`audit/determinism`, `CA1/C0`, `CA1/C1-C5`, `CA1/C6`, `CA1/C7`, `CA1/stage-coverage`,
`CA2/improvement`, `CA5/timing`, `CA6/completeness`, `CA3/breadth`, `CA1/C1-lite`,
`CA5/confirm-timing` and `CA5/memory`. The evaluator adds every guard, cap and exact record it
creates, and the cost panels the change scope requires.

**Upstream tests are optional.** They belong to the `unit-tests` stage, which you may run or
leave out ([verifier.md](workflow/unit-tests.md#what-runs)). The rule is: if you run it, it counts.
`CA1/upstream` is not in `policy.json`; `decide` adds it to the required set once
`unit-tests` has started (running, failed or complete). A regression then gives
`CONSTRAINT_VIOLATION`, an unfinished or unresolved suite `INCONCLUSIVE`, and a failed stage
`ERROR`. If the stage never started, or the quality gate skipped it, the verdict is decided
without it and the report says "Upstream tests: not run."

Report-only numbers:

- **`U_instance`**: a family-stratified cluster bootstrap over input groups (10,000
  replicates). It estimates how much the score depends on which circuits were chosen. It is
  printed, not tested, because several family/size cells have fewer than three groups.
- **Leave-iterations-out score**: the `D2` and `N2` scores recomputed without `qft_n100`,
  `square_heisenberg_n100` and `qaoa_ba_n100_3reps` (the policy's `iterations_groups`),
  printed in the report. A gain that lives mostly in those three circuits improved the
  tuning loop, not the suite.

Policy differences from the iterations profile: `practical_ratio` is 0.99 (versus 1.0), and
the required set adds `CA3/breadth`, `CA1/C1-lite`, `CA5/confirm-timing` and `CA5/memory`.

## Version 7

Version 7 (design/updated_confirm_plan.md) makes the confirm A/A pass its own checks and
shortens the stages:

- **Harness:** C6 replays snapshots of the quality compile itself; the determinism audit
  accepts a difference only as a proven VF2 tie (`VF2Layout` or `VF2PostLayout`); a failure
  both revisions share is
  `INCONCLUSIVE`, not a candidate violation; a timed-out seed is retried once and a
  candidate timeout is classified; checks run in harness processes on 15 slots with a
  per-case batch schedule (`quality_batch_schedule`); C7 runs one job per seed.
- **Policy:** `clifford_seeds` 5 and `clifford_full_mode` (C7 was 10 seeds);
  `c1_lite_max_width` 22 (was 25); `quality_timeout`; `companion_seeds` 0–9 (was 0–19) and
  `companion_groups`; `memory_processes` 3 (was 5); `quality_cap_rule` `plain`.
- **Manifest:** `mcx_kg24_n5` at levels 1–3 is a deterministic case on 10 seeds (it leaves the
  score, C6 and C1-lite: 300 replays and 30 C1-lite candidates per revision); G5 weights
  recomputed; `multiplier_h18_n20` L2 from 20 to 100 seeds; confirm-timing from 20 to 8 cases.
- **Not in version 7:** 50 seeds on the costliest scored cases (W1). The estimator supports it
  (50 or 100 seeds per scored case), but the list must be derived from measured costs and a
  cluster A/A first. `su2_circular_n89` L3 keeps its 120 s timeout until its cluster compile
  time is measured.

The targets are about 45 minutes for quality (both revisions, audit included), 20 for
correctness and 20 for cost when every screen passes. They are estimates to be measured by a
cluster A/A (V3), not measured results.

## Statistical power

The score averages many roughly independent cases, so its paired standard error over 100
seeds is about 0.2%: roughly three times smaller than the iterations profile's. A real gain
that passed iterations has a good chance here, and a lucky one very little. What limits the
confirm profile is the number of independent inputs, not the number of seeds. That is why
`U_instance` is reported.

## Budget

From the design probe on an Apple M1 Max, serial:

| Item | Cost |
| --- | --- |
| Quality compiles | About 15,400 compiles and 87 CPU-minutes per revision; version 7 compiles each seed once (no routing prefixes) |
| C1-lite | About 20 s per `ripple_adder_10` check at union width 22 and about 125 s at 25; wider baselines are no longer required pairs |
| One session | About 300 slot-minutes of quality work per revision on the cluster model (section 4.3 of the plan), about 46 minutes of wall time on 15 slots for both revisions |
| Cost panels | When every screen passes (plan section 6, estimates): timing 3 min, preset 1, confirm-timing 5, memory 4 (9 cases × 3 processes per arm), plus timing-basis 2.5 and companion about 6 (unmeasured) when the change scope requires them: about 13 minutes narrow, 21.5 with both. A breached screen still pays for its full and rerun rounds. The 20-minute goal with both optional panels needs a measured further saving (plan section 6, D4) |

Quality runs up to 15 tasks at once (compiles, checks in harness processes, audit and
verification). Plan for a long first run, and see [environments.md](environments.md) for
build time.

## Declared coverage gaps

Listed in the manifest (`coverage_gaps`) and repeated in every report:

- The upstream suite has no line target.
- G2, G3 and G6 appear on only one sparse topology class.
- G7 has no scored level-3 case.
- Some family/size cells have fewer than three independent groups.
- G4, G5 and G8 have no large-band scored inputs.

The design plan (section 3.7) lists what the next manifest version should add first.
