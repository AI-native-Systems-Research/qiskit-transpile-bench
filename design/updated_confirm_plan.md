# Plan: a faster confirm profile that passes its own A/A

**Status:** proposal, nothing implemented. 2026-09-28, re-reviewed against harness @`7e59562`, the two manifests and the locally available A/A report. Cluster timings and the earlier probe results have not been independently rerun in this review (see below).

**Inputs:**
- the running session `qtb-sessions/qiskit-evolve-0.83`: quality progress log (baseline half, 329 of 1,279 batches after 1h49m at the latest logged snapshot) and its observations;
- the A/A report `design/problem_in_confirm_profile_aa.md`;
- a reproduction of that report on the baseline build `54c6e505` (outputs in `/tmp/qtb_c6_probe/`);
- `design/probes/confirm_probe.jsonl`;
- the unimplemented `design/iterations-quality-coverage-plan.md`.

**Evidence availability:** the A/A report is present locally at `design/problem_in_confirm_profile_aa.md`, but is ignored and untracked, as are the VF2 and C1-lite probe scripts referenced below. The report names Qiskit `33bca5c0` and harness `68dd8ac`; the later reproduction names baseline build `54c6e505`. Keep those identities separate. `/tmp/qtb_c6_probe/` is absent, and the session stays on the cluster. Track the report and probe scripts and archive the reproduction outputs and a summary of the session log under `design/` before this plan is frozen. The report records its own figures, but does not substantiate the session timings in section 2. `design/probes/vf2_postlayout_probe.py` repeats the core of the reproduction (section 3).

**Targets on the cluster.** Every stage is its own LSF job (`docs/cluster.md`): 16 slots for quality and 16 for correctness, and 9 exclusive cores for cost, where one serial worker measures.

| Stage | Target |
| --- | --- |
| Quality | about 45 min for the quality stage alone: both revisions compiled, determinism audit included. The build, correctness, unit tests and cost are outside this budget |
| Correctness | about 20 min |
| Cost | about 20 min |

The profile must keep broad coverage. A healthy A/A control should finish `NO_IMPROVEMENT`: its improvement records may fail as expected, while every required harness, correctness, guard, completeness and cost record passes.

The verdict alone does not show that. When an improvement record is the only kind that failed, `verdict()` returns `NO_IMPROVEMENT`, also with a required record unresolved and the gate closed. A healthy A/A therefore also has the quality gate `aa` and every stage complete (V3).

## 1. Summary

| Stage | Today | Target | Main levers |
| --- | --- | --- | --- |
| Quality | About 4.5 h with both revisions compiled (estimate). After 1h49m, 329 of 1,279 batches are committed | 45 min for the whole stage, both revisions compiled, audit included. A baseline from the store is a bonus (about 17 min), not the target | Harness changes alone estimate about 46 min. Cutting 22 costly cases to 50 seeds estimates about 34 min, but requires a mixed-seed panel estimator before it can be used |
| Correctness | 28–40 min with both halves, 15–20 min with a cached baseline (estimate: no confirm session has reached it) | About 20 min with both halves | C7 split into one job per seed, verified as each seed finishes, with 5 seeds instead of 10; measure both C7 modes |
| Cost | About 27 min without the companion panel and about 85 min with it, when every screen passes (estimate) | About 20 min on the full, screen-clear path | Fewer confirm-timing and companion cases, 10 companion seeds, 3 memory processes; the proposed serial sum is still 21.5 min, so a measured further saving is required |
| A/A | `CONSTRAINT_VIOLATION`, with every panel unresolved | `NO_IMPROVEMENT`, with no failed non-improvement or unresolved required record | Section 3: seven fixes and one upstream report |

**What stays:**
- all 209 quality cases: 8 families, levels 0 to 3, every target and topology, every role;
- the whole C1–C5 suite;
- all 18 C7 Clifford cases;
- every cost panel, with the same screen, normal and rerun rules and the same thresholds.

**What shrinks:**
- seeds on 22 costly scored quality cases, from 100 to 50;
- the C1-lite limit, from a union width of 25 to 22. About 600 of the 630 candidate observations per revision fit it, and `ripple_adder_10` keeps about a quarter of its 40 (Q5);
- `mcx_kg24_n5` at L1–L3, from a scored case on 100 seeds to an exact check on 10. It leaves the score, C6 and C1-lite, where it has 30 of the candidates (W2);
- C7 seeds, from 10 to 5;
- confirm-timing cases, from 20 to 8;
- companion cases, from 8 to 4, and companion seeds, from 20 to 10;
- memory processes, from 5 to 3.

**What grows:** `multiplier_h18_n20` L2, from 20 to 100 seeds (W2).

**Profile version.** Seed blocks, roles, weights, constraints and the measurement protocol change, so the result is version 7 of the confirm profile (`docs/versioning.md`). The iterations policy changes with it if the C7 settings move into the policy (section 5). The iterations plan claims version 7 as well, so the two plans need one numbering.

The quality seed reduction is tied to validating the noise-aware cap (section 7). With fewer seeds on the costliest cases, the plain 1.05 cap can trip more often when a neutral candidate decorrelates seed outcomes. Cost sample reductions need their own A/A noise and known-regression checks; the quality cap does not validate them.

Each budget covers its own stage only. Quality's 45 minutes runs from stage start to stage completion: both revisions, C0/C6/C1-lite, the audit and the aggregate checks. It does not depend on a cached baseline. The input round-trip is part of `compile` (`compile_body`), not of this stage.

## 2. Where the time goes today

### 2.1 Quality

Measured on the 74 cases the baseline half had finished (75 min of wall time):

| Component | Time | Where it runs |
| --- | --- | --- |
| Full compile, one worker job per batch | 107 slot-min | Worker processes |
| Two routing-prefix recompiles per seed | 84 slot-min | Worker processes |
| C0 structural check, C6 replay, layout comparison | About 46 min, serialized (418 min of thread wall time, 9 threads sharing the GIL) | Coordinator threads |

1. **The checks are GIL-bound.** Each of the 9 pool threads runs C0 and C6 in Python after its batch compiles. The job used only 1.9–3.8 of its 16 cores. For example, `qft_n100` L0 logs `compile 1m03s, checks 13m20s` per batch.
2. **Only 9 batches are in flight** (`QUALITY_BATCHES_IN_FLIGHT`, `coordinator/__init__.py:94`) on 16 slots.
3. **The routing prefixes double the compile work.** They are two extra compiles per seed, one stopped after init and one after routing, and they make up 44% of worker time. They are also the root of the C6 failure (section 3).
4. **C1-lite runs in the commit loop.** `verify_many` runs on the main thread, in plan order. When the verifier hits its 300 s idle timeout, the log stops advancing for 5 minutes, as on `ripple_adder_10`: 11 of the first 20 seeds timed out at union widths 23–25.
5. **A few cases carry most of the work.** Per revision, in slot-minutes. The first six come from the fitted model (section 4.3); the `su2_circular_n89` figure is from the M1 probe and is the least certain:

| Group | Slot-min | Group | Slot-min |
| --- | ---: | --- | ---: |
| `qft_n100` (6 cases) | 57 | `dtc_n100` | 20 |
| `qv_n50_d50` | 45 | `qft_full_n64` | 19 |
| `su2_circular_n89` (probe) | 44 | `multiplier_h18_n32` | 17 |
| `qaoa_ba_n100_3reps` (6 cases) | 33 | `square_heisenberg_n100` (6 cases) | 15 |
| `qaoa_complete_n32` | 32 | the other 40 groups | 135 |

Total work per revision is about 417 slot-min: 335 in workers and 82 GIL-bound.

### 2.2 Correctness

C1–C5 does not depend on the profile: 2,400 compiles and 3,120 oracle requests, about 1 minute per revision.

C7 dominates. It has 18 cases with a Clifford variant, run as 36 jobs per revision. Each job compiles 10 seeds serially at 100 qubits, about 15 s per seed. Verification takes about 13 s per output. `CLIFFORD_SEEDS` and `CLIFFORD_FULL_MODE` are code constants (`coordinator/checks.py:21-25`), not policy.

### 2.3 Cost

Cost runs serially after correctness, one process at a time, alternating the two arms. On the cluster the job holds 9 exclusive cores: one for the monitor, one for the worker, seven reserved. The monitor refuses two workers at once (`lsf/cost_monitor.py`, contract `qtb-lsf-monitor/3`).

When every screen passes, the time is:
- timing (13 cases) 3 min;
- preset 1 min;
- timing-basis (6 cases) 2.5 min, run only when the change can reach translation or optimization;
- confirm-timing (20 large end-to-end cases) 11–15 min;
- memory 6–10 min;
- companion (8 cases × 20 seeds) 50–70 min, run only when the change can reach layout or routing.

An A/A session has no changed path, so its scope is empty and it measures neither `timing-basis` nor `companion` (`cost_panels`).

`companion_seeds` in the policy has no effect: 20 seeds are hardcoded in `costs.py` `_measure_rounds`, in `evaluator/cost.py`, and in `lsf/cost_evidence.py` `_coverage_problems`, which requires 20 measured entries per companion worker job. The policy field is a list of seed IDs, not an integer count.

## 3. Problems to fix

The reproduction confirms the report's hypothesis and sharpens it.

On targets without error rates, **VF2PostLayout breaks ties differently from run to run**:
- The seed is fixed, and the tie-breaking changes anyway, whatever `PYTHONHASHSEED` is set to.
- It even changes between runs in the same process: four runs gave 51, 52, 52, 51.
- SabreLayout is identical on every run; only VF2PostLayout varies.

**What the tie-breaking moves:**
- It always moves idle qubits.
- On `qaoa_complete_n32/rochester_53` it also moves a used qubit, on 15 of 100 seeds at L1 and 19 of 100 at L2. Virtual qubit 31 lands on physical 51 or 52, two equivalent leaves.
- On seeds 7 and 9 of that case the output bytes change too.

Comparing only the used qubits, as finding 11.1 of the iterations plan proposes, is therefore not enough.

**Review check (2026-09-28).** Qiskit 2.5.2, the version of the development environment and of CI, behaves the same on macOS. `design/probes/vf2_postlayout_probe.py` compiled seeds 0 to 11 three times each in one process. It ran twice, with `PYTHONHASHSEED` 0 and 1:
- `qaoa_complete_n32/rochester_53` at L1 and L2: the layout changed on 12 of 12 seeds in both runs.
- Virtual qubit 31 moved on 3 or 4 of the 12 seeds at L1 and on 2 or 3 at L2. The seeds differ between the runs: 4, 5, 6, 9 and 11 at L1, and 7, 9 and 11 at L2. The output bytes changed on exactly those seeds.
- Virtual 31 moved between 51 and 52, except on seed 11 at L2, where it landed on five physical qubits over the two runs. "Two equivalent leaves" does not describe every tie.
- C0, `D2`, `N2` and the union width were the same for every output of a seed.
- The first pass whose result differs is VF2PostLayout (`post_layout`). Everything before it, SabreLayout included, is identical.
- At L0 nothing changed, and nothing changed on `qaoa_complete_n16/mumbai_27` L2, a target with error rates. On `qft_cp_n8/melbourne_14_u` L2 only idle qubits moved, on 8 and on 12 of the 12 seeds.
- Fresh processes with the same `PYTHONHASHSEED` differ as well.

Two consequences: the upstream report (P7) can name a released version, and the CI smoke test below meets the failure without a source build.

| # | Problem | Cause | Fix |
| --- | --- | --- | --- |
| P1 | C6 fails on the unmodified baseline at L1 and L2 (report issue 1). In 0.83: 658 mismatches, on `qaoa_complete_n32`, `qft_full_n32`, `qv_n14_d14_s10`, `qft_cp_n8` and `qv_n50_d20` | The full compile and the routing prefix are separate compiles, and VF2PostLayout does not repeat itself. C6 then compares whole layouts (`coordinator/__init__.py:992`) | Replay C6 on snapshots taken from the same full compile (Q1). The independent replay must still verify the routed operations and layout. Keep the documented L3 allowance for a later legitimate layout change, unless a new end-to-end mapping check proves equality there |
| P2 | A failure the baseline shares is reported as a candidate violation (report issue 2) | `verdict()` checks evolved failures before reference failures | In quality, match only the same case, seed and oracle, with the same oracle-specific failure signature (status, reason and differing operation or outcome where available). For a matching pair, the evolved side is not recorded as a candidate failure. The aggregate record stays `unresolved` and names the pair, and the baseline side stays `failed` with subject `reference`: that record makes `verdict()` return `INCONCLUSIVE`. With both sides only `unresolved`, a failed improvement record would give `NO_IMPROVEMENT`. Any extra or different evolved failure remains `failed`. Apply this to C0, C6 and C1-lite before aggregation. In correctness, a failed baseline C1–C5/C7 preflight already stops evolved checks and yields `INCONCLUSIVE`; preserve that behavior. Keep `verdict()` fail-closed |
| P3 | `audit/determinism` is unresolved, and every panel with it (report issue 3) | VF2 tie-breaking can change layout and even output bytes. The audit requires identical output hash and whole layout (`:1053-1061`) | Always report the first differing case, seed, hash seed and field. Accept a differing hash/layout only when a specific VF2 tie is identified and the exported circuits are proven equivalent under the corresponding physical-wire permutation, with identical `D2`, `N2` and C0/C6 outcomes. Otherwise keep the audit `unresolved` and invalidate cached quality. Do not weaken the separate API contract checks. Validate this rule before using it (V1) |
| P4 | A worker timeout on the evolved build gives `CONSTRAINT_VIOLATION` (report note) | A timeout becomes a failed completeness record (`process.py:158-171`, `coordinator/__init__.py:854-865`). Only the seeds after the interrupted one get a fresh process today | Retry the interrupted seed once in a fresh process. Classify an isolated, repeated candidate timeout as `failed` only when the same baseline seed consistently completes with a substantial margin and the host shows no contamination; otherwise mark it `unresolved` with both attempts and the baseline timing. Put the margin and the host check in the policy: the quality job may share its host, and nothing measures contamination there today. Never turn missing output into a passing observation |
| P5 | New: `CA1/C1-lite` is unresolved in 0.83. The verifier times out at union widths 23–25, and every timeout blocks the commit loop for 300 s | The eligibility limit of 25 (`:953`) is above what the verifier finishes in 300 s | Lower baseline eligibility to 22 (Q5). An eligible baseline still obligates the evolved check: a wider or missing evolved output cannot remove the pair. Verification leaves the commit path (Q4). A required check that cannot finish remains `unresolved` and names the seed |
| P6 | New: `companion_seeds` is ignored | Hardcoded to 20 in measurement, estimation and LSF evidence admission | Use the frozen seed-ID list throughout all three paths, including the extension validation arguments (section 6) |
| P7 | Upstream | VF2PostLayout is not reproducible with a fixed seed, on the baseline build and on the released 2.5.2 | File a Qiskit issue with the `qaoa_complete_n32` reproduction. Accept only the equivalences proved in P3; otherwise apply a deterministic Qiskit fix to both arms before trusting confirm |
| P8 | W1 would stop the session at `compile`, and would make scored panels unresolved | `load_profile` refuses a scored case without 100 seeds (`config/__init__.py:95-96`). `panel_record` rejects mixed `seeds_per_block` (`evaluator/__init__.py:95-98`), and report-only summaries hardcode 100 seeds (`:218`) | Implement and test a paired panel estimator for differing per-case seed counts, including family/level guards, leave-out scores and the report-only instance bootstrap, before enabling W1. Then let the loader accept the seed counts that the estimator supports |

**Shared-failure evidence (P2).** A generic `mismatch`, reason string or gate name alone is not a sufficient signature. Today's C6 can return only `status="mismatch"` plus a SWAP count for a final-map or leftover-operation failure (`metrics/replay.py`), and many different defects can yield `Operation cannot be replayed` for the same gate name. Add structured failure witnesses (the divergent mappings or operation, its wires/parameters and replay position; corresponding oracle-specific evidence for C0/C1-lite). Suppress an evolved failure only when those witnesses establish the same failure; insufficient evidence keeps it failed. Test two distinct failures with the same generic status/reason, not only different reason strings.

**Regression test.** A confirm A/A smoke run in CI covers:
- the five C6 cases above at L1 and L2, 10 seeds each;
- the audit on those cases;
- separate synthetic tests for a shared failure, a candidate-only failure on another seed of the same case, and a different failure on the same seed.

CI has no source build. The smoke run compiles with the locked Qiskit 2.5.2 through a local build, as `tests/test_worker_storage.py` does, and that version shows the same tie-breaking (review check above).

The ordinary A/A expects `NO_IMPROVEMENT`, with only improvement records allowed to fail and no unresolved required record. The injected shared failure must instead be `INCONCLUSIVE`, and the candidate-only failure must remain a violation. A full cluster A/A is the acceptance test of this plan (V3).

## 4. Quality: 45 minutes

The budget covers the quality stage alone: every quality batch of both revisions, C0, C6, C1-lite and the determinism audit. It must hold with no stored baseline rows.

### 4.1 Harness changes and C1-lite eligibility

**Q1. One compile per seed, with snapshots.**
- A quality batch asks for the taps with pipeline edits, as the prefix jobs ask for `drop_stage` today. The worker then adds a no-op tap pass in `post_init` and in `post_routing`. The pass stores the DAG and layout at that point, with any permutation that `init` elided.
- Append each tap after any existing post-stage hook; never replace that hook. Freeze copies of the DAG and relevant property-set state at capture time, before later passes mutate them. Construct the exported layout from that captured state, including ancillas, input mapping and elided permutations; a DAG alone does not carry the final circuit's `TranspileLayout`. Missing or unsupported boundary state leaves C6 unresolved. Test snapshots against the old prefix exports on deterministic cases, including elided SWAPs and ancillas.
- The mode does not select the taps: the audit and the C1–C5 suite also compile in `quality` mode, and they ask for none.
- The worker writes those two snapshots next to the output.
- The two prefix jobs (`routing_batch`, `:968-976`) go away. The stored baseline rows and `clean` keep snapshots where they keep prefix jobs and outputs today (`prefix_jobs`, `prefix_outputs`).
- C6 independently replays routing between the snapshots. At L0–L2 it checks the full output's layout against the routed snapshot; at L3 it retains the documented allowance for a layout change after routing and records that difference. Add an exact final mapping check only if the later optimization can be traced.
- This removes the false comparison between separate compiles without accepting an invalid routing replay. It also removes the two prefix compiles, about 44% of worker time today. The snapshots are still exported, so the net saving is smaller (section 4.3).
- The taps must stay out of `fingerprint(pm)`.
- A tapped compile must give the same output as an untapped one, apart from a VF2-only equivalence proved under V1/V2. The audit, which compiles without tap passes, keeps checking this on every run. The evidence needed for that exception is specified in Q6; separate diagnostic recompiles cannot establish the provenance of an already exported output.

**Q2. Checks in a harness process pool.**
- C0, the C6 replay and the layout comparison move from the coordinator threads to a pool of harness processes. They read the files the worker wrote.
- The checks stay out of the worker on purpose: the worker imports the candidate's Qiskit, and a candidate must not be able to reach its own checks.
- Compile, check, audit and C1-lite verifier tasks share a hard limit of 15 runnable slots on a 16-slot job, leaving one for the coordinator; for smaller allocations use `max(1, min(15, allocated_slots - 1))`. The check and verifier pools must not oversubscribe the worker pool. Release a compile slot before waiting for a check/verifier slot, so full compile queues cannot deadlock the pipeline.

**Q3. Scheduling.**
- Up to 15 runnable tasks across the compile, check, audit and verifier pools.
- Longest first: order by the stored baseline `compile_ns`, or by a static estimate when none is stored.
- Batch sizes follow cost, so no task runs longer than about 2 minutes. Costly cases use batches of 5 seeds, and `su2_circular_n89` L3 uses one seed per task.
- Cheap cases use one batch of 100 seeds (iterations plan 9.4).
- A deterministic per-case batch schedule replaces the single `quality_batch_size`; include its version in the measurement protocol/cache identity. Existing stored baseline rows will need rebuilding, possibly in both profiles.
- The policy schema and `tests/test_policy_protocol.py` cap a batch at 25 seeds today. Both change with the schedule.

**Q4. Commit and verify off the critical path.**
- Batches finish in any order.
- Each finished batch is durably checkpointed before acknowledging it. Materialize `observations.jsonl` and `evidence.json` in plan order from those checkpoints, including on resume, so scheduling does not affect their bytes or discard finished work after a crash.
- C1-lite requests go to the verifier pool as soon as their batch finishes. The progress log reports completed batches, not the position in plan order.

**Q5. C1-lite at union width 22 or less** (P5).
- **Count.** 63 scored cases have at most 25 logical qubits, so 630 observations per revision are candidates. In a local compile of all of them (`design/probes/c1_lite_width_probe.py`, Qiskit 2.5.2), 601 have a union width of 22 or less. The count on the baseline build depends on its outputs.
- **What is lost.** Only `ripple_adder_10`, 22 qubits on the 5×5 grid, is wider: 29 of its 40 observations have a width of 23 to 25, and 11 stay. No other case reached 22. W2 removes 30 more candidates, which leaves about 570.
- **Pairs.** Keep baseline-driven eligibility (`:1093`) with the new limit of 22. If the baseline fits, require both checks to verify. A wider evolved output, missing check or timeout remains `unresolved`; do not remove that pair from the denominator. Otherwise a candidate could evade a semantic check by widening just its incorrect outputs, even while C0 and C6 pass. Publish baseline exclusions and evolved width overruns separately (D6). Preserve any observed mismatch even outside the required pair set.
- **One eligibility limit.** Put 22 in the frozen measurement protocol, use it consistently in `quality_batch` and `aggregate_checks`, and pass it to the verifier as `max_qubits`, whose default is 25. Any optional wider verification uses an explicit, separately frozen fallback budget; it does not change eligibility.
- **Time.** Measure that the retained pairs finish within the verifier timeout. A timeout remains `unresolved`.

**Q6. The audit** (5% of seeds, at least 10, per revision) runs in the same pool, grouped by case and hash seed (iterations plan 9.4), with the comparison rule of P3. Keep the baseline cache invalidation for any unresolved audit.

- The VF2 exception needs evidence from the two actual compiles being compared. Record sufficient immutable pass-boundary evidence to locate the first difference at VF2PostLayout and identify its physical-wire permutation. On the untapped audit compile, use a read-only callback to capture the needed state and init/routing boundaries without adding tap passes; independently run C0 and, where required by `routing_replay_seeds`, C6 on that output and those snapshots. If the adapter cannot capture this evidence, keep the strict hash/layout comparison. Include capture/export overhead on the original quality compiles and capture, export and replay overhead on audit compiles in the stage budget; validate callback transparency in V2.
- The harness must prove that the permutation consistently maps both initial and final layouts and the exported operations, including parameters, classical wires, payloads and global phase. Equal metrics or a later diagnostic run are insufficient. Retain the proof artifacts with stored observations and through `clean`; missing evidence prevents using the exception.

### 4.2 Workload changes

**W1. 50 seeds instead of 100 on the 22 costly scored cases.** These are the cases costing more than 2.5 s per seed:

| Group | Levels |
| --- | --- |
| `qft_n100` | L0–L3 |
| `qft_full_n64` | L1–L3 |
| `dtc_n100` | L0 |
| `qaoa_ba_n100_3reps` | L2–L3 |
| `qaoa_complete_n32` | L2–L3 |
| `qv_n50_d50` | L0–L3 |
| `qv_n50_d20` | L2–L3 |
| `multiplier_h18_n32` | L0, L2, L3 |
| `su2_circular_n89` | L2 |

- Weights do not change. For each evaluated panel or subset, normalize its frozen weights to sum to one, as `paired_panel` does today; call these `w_c`. Estimate each case's paired log ratio from its own 50 or 100 seeds, then sum those case means with `w_c`. For uncertainty, keep the correlation of cases sharing a seed: use the contribution `X_s = Σ_{c with seed s} w_c·d(c,s)/n_c`, partition seeds by identical case availability (0–49 and 50–99 for W1), and set `SE² = Σ_strata n_stratum·sample_variance(X_s)`. If all cases have 100 seeds, use one 0–99 stratum. Use the same stratified seed resampling in the report-only instance bootstrap. Missing seeds within a case remain `unresolved`; they are never filled or reweighted.
- Replace the uniform-seed assumption in `panel_record`, `report_summary`, family/level guards and leave-out scores, and the loader rule that every scored case has 100 seeds (`load_profile`). Verify that its scores and standard errors agree with the present estimator to numerical precision when every case has 100 seeds. **Do not change the manifest to mixed seed counts until this estimator passes those tests.**
- Show each mixed panel's minimum and maximum case seed count in `report.md`; a single `Seeds: 100` label would hide the 50-seed cases.
- Their standard error grows by about √2 (1.4×) if seed variance is unchanged. Recompute panel and per-case uncertainty from paired seeds; today's score has 133 scored cases, and W2 leaves 130. No claim of negligible panel noise is justified until a replay or A/A measures it. The per-case caps need section 7.
- Guards and the cx/ecr twins keep their seeds.

**W2. Small fixes from finding 11.3 of the iterations plan.**
- `mcx_kg24_n5` at L1–L3 gives `D2` 14 and `N2` 15 on every seed. It becomes a deterministic case on 10 seeds: it keeps its coverage as an exact check and stops carrying score weight.
- C6, C1-lite and the stage-coverage rule follow the scored role (`routing_replay_seeds`), so these three cases leave them: 300 replays and 30 C1-lite candidates per revision. Publish this with the other sample counts.
- Recompute the G5 weights after those three cases leave the scored panel; W1 alone leaves weights unchanged, but W2 changes that panel's composition. Check the family and leave-out summaries for this deliberate shift.
- `multiplier_h18_n20` L2 goes from 20 to 100 seeds, since it compiles in 0.2 s.

**W3. `su2_circular_n89` at L3.** The probe measured 40 s per compile on M1, against a 120 s timeout. The model of section 4.3 puts the full compile at about 50 s on the cluster (2.19 × 40 s, less the prefix share). Measure it in the first run. If it exceeds 60 s on the cluster, raise the timeout for this case to 300 s. The timeout is part of the manifest; `tools/freeze_timeouts.py` drafts it from measured compiles.

### 4.3 Estimate

**Model.**
- Cost per seed ≈ 0.36 s + 1.4·10⁻⁴ s × `N2` + 2.19 × the M1 compile time.
- Fitted on the 74 measured cases; it predicts the top cases to within about 20%.
- The other 135 cases are estimated from the M1 compile times of `confirm_probe.jsonl`. They are the `bv`, `queko`, adder, `mcx`, `multiplier`, `su2`, `ring` and `a2a` groups, the rest of `ripple_adder`, and the 12 cases whose baseline rows came from the store.
- Those 12 are the quality cases of the iterations profile: the L2 cases and cx/ecr twins of the 100-qubit groups, and the canaries `su2_circular_n100` L2 and `long_2q_sequence` L2 and L3. The manifest gives 1,318 batches for both revisions; the 39 batches of these cases are what the 1,279 of the log leave out.
- The probe has no row for the six generated replacement groups: `mcx_kg24_n5`, `mcx_kg24_n6`, `adder_modular_v17_n6`, `_n8` and `_n16`, and `multiplier_h18_n20`. It holds the circuits they replaced. These 21 cases are small, but their figures need another source.
- Wall time = work ÷ 15 slots × 1.15, for start-up, the tail and contention.
- The original estimate adds 5% of compile work for the audit. It does not price Q6's additional provenance capture and replay; measure those before treating the table as a stage-duration estimate.
- The model has no term for C1-lite. About 570 checks per revision remain (Q5), fewer where seeds repeat an output. On the M1, alone on the machine, one `ripple_adder_10` check took about 20 s at union width 22 and about 125 s at 25. The verifier's slot-minutes count against the same 15 slots: measure them in the first run.

| Scenario | Work per revision | Both revisions | Evolved only (baseline from the store) |
| --- | ---: | ---: | ---: |
| Today | 417 slot-min, GIL-bound | About 4.5 h | About 2.3 h |
| Q1–Q6 | 299 slot-min | 46 min | 23 min |
| Q1–Q6 + W1 | 222 slot-min | 34 min | 17 min |

The recommended set is Q1–Q6 with W1 **after P8 is implemented and validated**. It leaves about 10 minutes of estimated margin for the uncertain timings: C1-lite, and mainly `su2_circular_n89`, which accounts for 44 slot-minutes per revision today. Until then, keep 100 seeds on all scored cases; Q1–Q6 alone estimate about 46 minutes, close to the requested target but with no margin.

**If the first measured run is over budget:** profile the slowest cases and the process pool first. Keep C6 on every scored and basis-guard seed and 10 seeds of each other static guard, as the current verifier contract requires. Change the W1 threshold or seed count only after measuring A/A false breaches and known routing/quality regressions; publish the resulting C6 and C1-lite sample counts. Do not claim 45 minutes by silently shrinking a correctness check.

## 5. Correctness: 20 minutes

- **C1–C5** stays as it is: about 1 minute per revision, and near zero on a cached baseline.
- **C7 runs one job per seed** in the shared pool. Each output is verified as soon as it lands, instead of 36 jobs that each compile 10 seeds serially.
- **Seeds and policy.** C7 keeps all 18 cases, because `CA1/stage-coverage` needs C7 on the 100-qubit scored cases. Its seeds go from 10 to 5. `CLIFFORD_SEEDS` and `CLIFFORD_FULL_MODE` move into the policy of both profiles (`clifford_seeds`, `clifford_full_mode`). The correctness store key already covers both constants, and the policy hash as well (`baseline_correctness_key`), so the move adds nothing to the key. Its `clifford` entry then reads the policy.
- **Estimate:** 18 cases × 2 modes (full and prefix) × 5 seeds = **180 outputs per revision**. At an assumed 15 s compile and 13 s verification per output, that is 84 slot-min per revision, or 168 slot-min for both halves. The ideal 16-slot floor is 10.5 min before C1–C5, startup and scheduling tails. Budget about 15–20 min provisionally; measure the two modes and verifier concurrency on the cluster before claiming the target. A cached baseline shortens this, but is not assumed.
- **Unit tests run alongside correctness, and cost waits for both.** They have their own 16-slot job, so they take no slot from correctness. The suites do not depend on the profile, and in iterations they took about 11 minutes. They must stay under 20 minutes, or they become the critical path to cost; V3 measures them.

## 6. Cost: about 20 minutes when the screens pass

Unchanged:
- `screen_rounds` 4, `timing_rounds` 10, `rerun_multiplier` 2;
- the ratio thresholds and floors;
- the rerun on a breach.

A breach still gets its full normal and rerun rounds. The 20 minutes is the path where every screen passes.

| Panel | Today | Plan | Time (screens pass) |
| --- | --- | --- | ---: |
| timing (T1–T11, T14, T17) | 13 cases, screen | Same | 3 min |
| preset | 3 cases | Same | 1 min |
| timing-basis (T12, T13, T15, T16, T18, T19; translation/optimization in scope) | 6 cases | Same | 2.5 min |
| confirm-timing | 20 cases, 8 families | 8 cases, one per family: `qft_n100` L3, `square_heisenberg_n100` L0, `qaoa_ba_n100_3reps` L3, `qv_n50_d50` L2, `multiplier_h18_n32` L0, `bv_all_ones_n100` L3, `queko_bss_53` L2, `su2_circular_n89` L0 | 5 min |
| memory | 9 cases × 5 processes × 2 arms | 9 cases × 3 processes | 4 min |
| companion (layout/routing in scope) | 8 L2 cases × 20 seeds | 4 L2 cases × 10 seeds: G2, G5, G6, G8, provisionally selected as the cheaper families. `companion_seeds` is read from the policy and validated by the estimator (P6) | 6 min, unmeasured |

- **Companion protocol end to end:** freeze `companion_seeds` as the unique nonnegative seed-ID list `[0, ..., 9]` (or `[0, ..., 5]` for the validated fallback), and require exactly those IDs in both arms at every regime. Pass the policy-derived list through `validate_bundle` and `check_cost_evidence` to `lsf.cost_evidence.validate`; `_coverage_problems` must require its length in measured entries per worker job instead of 20. Never infer the expected count from the submitted samples, which could hide missing seeds. Validate the list in the policy schema. Preserve the historical 0–19 rule for archived policies without the field, and keep old monitored bundles replayable. This dependency applies even with serial memory and the existing monitor layout; concurrency additionally requires the new contract below.
- **Totals:** the table sums to **21.5 minutes** when both `timing-basis` and `companion` are required, and **13 minutes** without them. The companion estimate assumes the selected cases are substantially cheaper than the current eight-case panel; no confirm cost run has established that. Measure each panel on the target host before freezing this selection.
- **Path to 20 minutes:** the cost job has 9 exclusive cores, and 7 of them are reserved and idle. A second worker breaks the serial protocol of the monitor, so options 1 and 2 need a new monitor contract with more than one worker core, its evidence format and its validator (`lsf/cost_monitor.py`, `lsf/cost_evidence.py`, `docs/versioning.md`).
  1. Run the memory panel's own processes side by side on reserved cores, while no timing panel runs. Peak RSS needs no quiet core, and no timing sample is exposed. Four at a time would bring the 4 minutes to about 1, and the worst scope to about 18.5 minutes. Check in a cost A/A that the memory ratios do not move.
  2. Overlap at least 2 minutes of the memory panel with timing or companion on disjoint pinned cores. This exposes timing samples to shared-cache and memory-bandwidth effects, which the monitor does not detect. Adopt it only if repeated cost A/A runs show no material shift in timing ratios, noise or contamination.
  3. If neither validates, try six companion seeds on the same four cases (linear estimate about 3.6 minutes; worst-scope total about 19.1 minutes). Adopt that only after A/A noise and seeded known-regression checks show the cost guard still detects meaningful changes.

  If no option validates, record the 20-minute goal as unmet and profile the slow cases; do not omit a required panel.
- **Coverage tradeoff:** `confirm-timing` keeps one case in each of the eight families; the proposed companion samples four families over multiple seeds, down from eight. Publish this loss of companion coverage and test a layout/routing slowdown in a retained and an omitted family before freezing the four-case list.
- **Required ids:** `required_ids` and `curate.py:95` must stay in step with the panel lists (`decide.py:300-303`). A panel with no cases is dropped silently, and a missing panel then leaves a required id unresolved.
- **Screen path only:** a breached or inconclusive screen still pays for its normal and rerun rounds. The budget does not claim to cover that path.

## 7. Noise-aware cap in confirm

Decision D3 of the iterations plan applies here, and W1 makes it necessary.

The iterations plan's section 11.2 estimates about a 50% chance of at least one false breach over the 152 scored and guard cases **assuming independent baseline and candidate seed outcomes**. This is a model for a neutral change that decorrelates routing, not a measured confirm A/A rate: identical deterministic builds have zero paired differences. Halving seeds generally increases uncertainty when paired variance is nonzero; measure the actual paired variance and cross-case dependence before quoting a false-breach rate for W1.

**Rule:** a scored or guard case fails the 1.05 cap only when its ratio exceeds 1.05 **and** `ln(ratio) > 3·SE_case`. Compute `SE_case` from paired per-seed log differences, using the same seed block as the ratio. Preserve the existing independent `ln(ratio) ≤ 3·SE_case` guard for guard cases outside a basis panel, and preserve family, level, `N2` and basis-panel guards. Exact deterministic, zero-baseline and canary checks stay exact. This reduces false cap breaches but also reduces sensitivity near 1.05; validate the false-breach rate and injected regressions, including a 6–10% case-level degradation, before changing the rule. Apply the same cap rule to the iterations coverage panel if that plan is implemented.

## 8. Relation to the iterations coverage plan

**Taken from it:**
- finding 11.1, generalized: section 3 shows that comparing used qubits only is not enough, and snapshots from the same compile replace it;
- finding 11.2 (section 7);
- finding 11.3 (W2);
- the two optimizations of 9.4 (Q3, Q6);
- the leave-out scores of section 8. Print the existing three-group `D2` leave-out score and compute/print its `N2` counterpart; if the iterations expansion ships, add the full 11-group list to policy and compute and print both metrics for that second leave-out score.

**Coverage interaction.** Adding 50 cases to iterations leaves the confirm manifest intact but exposes more confirm inputs to tuning: the iterations plan estimates that the held-out share of confirm's score falls from 81.8% to 56.8%, and the overlap rises to 11 of 38 input groups. Report both leave-out scores so a result driven by loop-visible groups is clear. Repair the confirm A/A before interpreting any confirm verdict; coordinate shared harness work if both plans proceed.

**Store keys.**
- **Quality.** The harness identity and the quality measurement protocol are part of the quality cache key. Any change of harness code, Q1–Q6 included, recomputes the stored baseline quality rows once, in both profiles. `seeds_per_block` and `timeout_s` are not part of that key, so W1 and W3 alone keep the rows.
- **Sharing.** Matching protocol values across profiles may let identical case definitions share rebuilt rows; implementing both plans together does not guarantee only one baseline run.
- **Build and unit tests.** Both are keyed by the harness wheel: the stored baseline build gets a new environment, and the baseline unit-test results are computed again.
- **Correctness.** Its key covers the harness, the manifest hash and the policy hash. Every manifest or policy change of this plan recomputes the baseline correctness, not only the C7 settings.
- **Archived sessions.** `decide` judges them with the current evaluator. Read the new policy keys with defaults that keep today's rules, so that a version-6 session keeps its verdict.

## 9. Work items

| # | Item | Main files |
| --- | --- | --- |
| 1 | Tap passes in `post_init` and `post_routing`, requested by a pipeline edit; write the snapshots; keep them out of the fingerprint | `worker/qtb_worker/adapter.py` (`preset`, `fingerprint`), `worker/qtb_worker/modes.py` (`run_seed`) |
| 2 | C6 from the snapshots; remove the prefix jobs; preserve L3 semantics; snapshots in the stored rows and in `clean` | `src/qtb/coordinator/__init__.py` (`quality_batch`, `routing_batch`, `check_routing`), `src/qtb/metrics/replay.py`, `src/qtb/coordinator/storage.py` (`cache_quality_observation`, `prefix_output_deletions`), `src/qtb/coordinator/clean.py` |
| 3 | Check process pool; scheduler with 15 in flight, longest first and cost-based batches; durable out-of-order checkpoints and plan-order materialization | `src/qtb/coordinator/__init__.py` (`quality`, `structural_result`), policy schema (`quality_batch_size`, capped at 25), store key |
| 4 | Verification off the commit path; baseline C1-lite eligibility of 22; require evolved verification or leave the pair unresolved; freeze the limit in the protocol and pass it to the verifier | `coordinator/__init__.py` around lines 846, 953 and 1093; policy schema; `verifier/qtb_verifier/__main__.py:60` |
| 5 | Audit first-difference detail and proven VF2-equivalence rule, with immutable evidence from the compared compiles; audit grouped by case and hash seed; preserve cache invalidation and proof artifacts | `coordinator/__init__.py:1053-1061`, worker adapter/modes, `coordinator/storage.py`, `coordinator/clean.py` |
| 6 | Structured oracle failure witnesses and per-seed shared-failure matching across quality C0, C6 and C1-lite; preserve correctness preflight | `coordinator/__init__.py` `aggregate_checks` (`:1071-1149`), `metrics/replay.py`, C0 and verifier result producers |
| 7 | Timeout retry and classification | `coordinator/process.py:113-180`, `coordinator/__init__.py:854-865` |
| 8 | C7 as one job per seed, verified as it lands; `clifford_seeds` and `clifford_full_mode` in both policies | `coordinator/checks.py:21-25`, `coordinator/stages.py` (`correctness_body`, `baseline_correctness_key`), policy schema |
| 9 | Cost: honour the companion seed-ID list in measurement, estimation and monitored evidence admission; companion case list; 8 confirm-timing cases; `memory_processes` 3; test the parallel memory panel, the memory overlap and the six-seed fallback | `coordinator/costs.py`, `evaluator/cost.py`, `extensions.py`, `lsf/cost_evidence.py`, policy schema, `tools/curate/curate.py:95`; for extra concurrent workers also `lsf/cost_monitor.py` |
| 10 | Mixed-seed paired estimator and stratified bootstrap (P8), the loader rule, report mixed panel seed counts, then profile W1 seeds, W2 roles and W3 timeout; regenerate manifest and policy with `curate.py` as version 7 | `src/qtb/evaluator/statistics.py`, `src/qtb/evaluator/__init__.py`, `src/qtb/config/__init__.py` (`load_profile`), `src/qtb/reporter/__init__.py`, `tools/curate/curate.py`, `profiles/confirm-profile/` |
| 11 | Noise-aware cap from paired seeds, selected by a policy key; both leave-out reports if iterations expands | `src/qtb/evaluator/__init__.py`, `src/qtb/reporter/`, policy schema |
| 12 | Tests: A/A smoke run in CI, shared versus candidate-only failure, audit equivalence/detail, timeout classification, tap equivalence and known routing/quality/cost regressions. Tests that pin today's behaviour change with it: the prefix jobs and their pruning, the plan-order commit, the batch cap of 25, the 25-wire limit | `tests/`, among them `test_worker_storage.py`, `test_run_speed_and_log.py`, `test_policy_protocol.py`, `test_c1_lite_budget.py` |
| 13 | Docs: C6, the audit, the C1-lite limit and the C7 seeds (`verifier.md`, `workflow/quality.md`); verdict order, timeouts and the cap (`metrics.md`); seeds, C1-lite, budgets and version (`confirm-profile.md`, `versioning.md`); companion seeds and durations (`workflow/cost.md`); the C6 prefix outputs (`output-format.md`, `workflow/clean.md`) | `docs/` |
| 14 | Upstream Qiskit issue on VF2PostLayout | none (external) |

Suggested order:
1. Items 1–2 and 5–7, which fix the A/A.
2. Items 3–4, the speed of the quality stage.
3. Items 8–9.
4. Items 10–11.
5. Items 12–14 alongside the rest.

## 10. Validation

- **V1.** Reproduce each observed audit difference, starting with `qaoa_complete_n32` L2 seeds 7 and 9, and seed 11, where the used qubit has more than two places (section 3). Check equal `D2`, `N2`, C0 and C6, then prove from the compared compiles' own evidence that the outputs differ only by the recorded VF2 physical-wire permutation. A hash or layout difference without that proof keeps `audit/determinism` unresolved. Test a deliberately changed output with equal `D2` and `N2`, a changed phase or parameter, inconsistent initial/final mappings, and missing proof artifacts; none may pass the exception.
- **V2.** For every quality case, compile seed 0 with and without taps and with the audit callback. Require identical output hash and layout or the same proven VF2-only equivalence as V1; also compare C0 and C6 where evidence is captured. Neither taps nor the callback may alter the pass-manager fingerprint or metrics. Test existing post-stage hooks, later mutation of captured state, elided permutations, ancillas and missing stages. Compare deterministic snapshot exports to the former prefix jobs.
- **V3.** Run a full A/A through the LSF manager on a quiet cluster. An A/A never reads stored baseline rows, so it compiles both halves. Expect `NO_IMPROVEMENT` with the quality gate `aa` and every stage complete: the improvement records may fail, but every required harness, correctness, guard, completeness and cost record must pass, with no unresolved required record. Record quality, correctness and cost wall time separately against 45/20/20 minutes. The cost time of an A/A is the narrow scope, 13 minutes in section 6; V6 covers the other panels. Measure `unit-tests` separately if it is started; it may delay cost even if correctness itself meets 20 minutes. A cached-baseline run needs two different builds and is a separate speed check.
- **V4.** Compare the new baseline `D2` and `N2` with the rows stored by session 0.83 (same Qiskit build). The harness changes must not move any metric.
- **V5.** Run a known-bad routing candidate, for example one that drops a SWAP, and check that C6 still catches it after Q1 on the affected seed. Check candidate-only failures on a different seed and with a different signature on the same seed of a case with a baseline failure; both must remain violations.
- **V6.** Run repeated cost A/A sessions with the trimmed panels: with serial memory, and with each memory option of section 6 that is tried. Measure every panel's elapsed time and ratio. An A/A session measures neither `timing-basis` nor `companion`, and a no-op change in their scope never opens the gate. Measure these two panels as a panel-level A/A: `measure_panel` on the cost host, under the monitor, with both arms on one build. If using six companion seeds, test seeded layout/routing slowdowns and an omitted-family slowdown as well as A/A noise. Freeze only a configuration that fits about 20 minutes and retains adequate sensitivity.
- **V7.** Confirm the C7 job ledger contains 18 cases × 2 modes × 5 seeds × 2 revisions = 360 outputs, and both required prefix checks and the full-mode diagnostics are accounted for. A known-bad Clifford mutation must still fail C7. Count C1-lite baseline-eligible, verified and timed-out pairs, baseline width exclusions and evolved width overruns. An eligible baseline paired with a wider or missing evolved output must leave `CA1/C1-lite` unresolved unless the evolved output is independently verified under a frozen fallback budget. Inject an incorrect evolved output that also widens: it must not gain a passing C1-lite record by leaving the pair set.
- **V8.** Test P8 on synthetic panels with 50- and 100-seed cases: the weighted mean and stratified SE against a direct calculation, shared-seed covariance, missing-seed fail-closed behavior, and numerical agreement with the current 100-seed estimator. Recompute the family/level, breadth and leave-out summaries on a full A/A; none may become unresolved solely because of mixed seed counts, and the report must show the true seed-count range.
- **V9.** Exercise the full monitored companion path with 10 seeds and, if adopted, 6 seeds: measurement, bundle validation, resume and `decide` after `clean`. Missing or unexpected seed IDs and incomplete measured intervals must be rejected. Replay archived 20-seed bundles unchanged. Test a noncontiguous valid seed list so no layer silently substitutes `range(len(seeds))` for the frozen IDs.

## 11. Decisions and risks

**Decisions:**
- **D1 (settled).** The 45 minutes covers the quality stage alone, with both revisions compiled and the audit included. A cached baseline only makes it faster.
- **D2. W1 threshold and seed count.** The proposal is 2.5 s per seed and 50 seeds after P8/V8. Until then, keep 100 seeds and accept the roughly 46-minute estimate rather than making the scored panels unresolved. Derive the list again from measured costs when it is frozen. The model of section 4.3 with the probe's `N2` does not give the same list: `qaoa_complete_n32` L0 and L1 and `qft_full_n64` L0 also exceed 2.5 s there, through their checks, and `qft_full_n64` L1 and L2 stay just below.
- **D3. Audit equivalence:** if V1 cannot prove that the VF2 differences are only a physical-wire permutation, keep the strict audit and treat this build's confirm A/A as `INCONCLUSIVE` until a deterministic VF2 fix is applied to both arms and validated. Equal `D2`/`N2` alone is insufficient.
- **D4. Cost coverage and scheduling:** retain the proposed one-per-family confirm-timing panel; freeze the companion cases, seed count and any second cost worker only after V6. A second worker is a new monitor contract. The table's serial 21.5-minute sum is not the target configuration.
- **D5. C7 at 5 seeds**, with both modes and all 18 cases; keep 10 if the measured 20-minute budget allows it.
- **D6. Baseline C1-lite eligibility at 22.** Preserve the requirement to verify the evolved output whenever the baseline qualifies. Wider evolved outputs may leave `CA1/C1-lite` unresolved and the quality gate closed; reporting the lost pair alone is insufficient. An optional fallback can verify evolved outputs up to 25 wires under a separately frozen timeout, but it must be measured and its time included in the budget. The candidate cannot choose which required pairs disappear.

**Risks:**
- **Estimates.** 135 of the 209 quality cases are estimated from the M1 probe, not measured on the cluster. `su2_circular_n89` is the largest uncertainty, and C1-lite is not in the model.
- **Tap passes.** A candidate that renames or restructures the stages could move them. Tap by stage name, as `drop_stage` already does, and fail loudly when a stage is missing.
- **Store.** The new harness and batch schedule invalidate existing quality entries. Rebuilding them in both profiles may require separate runs if their case definitions or protocols differ. The baseline build environment, correctness and unit-test entries are rebuilt as well (section 8).
- **Unit tests.** They sit on the critical path to cost. They are the suites of iterations, about 11 minutes there, in a job of their own.
- **Inputs.** The A/A report and two supporting probe scripts are local but untracked; the reproduction outputs and cluster session log are unavailable here. Archive these inputs before freezing numerical claims (see Evidence availability).
