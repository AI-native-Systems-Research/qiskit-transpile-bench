# How metrics and verdicts are calculated

[Documentation index](README.md)

This page follows one number from a compiled circuit to the final verdict. The code lives in
`src/qtb/metrics/` (per-circuit metrics), `src/qtb/evaluator/statistics.py` (quality
estimators), `src/qtb/evaluator/cost.py` (time and memory) and `src/qtb/evaluator/__init__.py`
(guards and verdict). None of it imports Qiskit.

## 1. Per-circuit metrics: `D2` and `N2`

Both are computed from the canonical output file the worker exports, using the list of native
two-qubit gate names stored in the frozen target (`native_2q_names`, for example `["cz"]`).

- **`N2`**: the number of operations whose name is in `native_2q_names` and that act on
  exactly two qubits.
- **`D2`** (the objective): the length of the longest dependency path when only those
  operations count. Every operation is still a node on all of its qubit and classical-bit
  wires. Counted operations weigh 1, and all others (single-qubit gates, barriers,
  measurements) weigh 0 but still synchronize their wires.

```python
level, n2 = {}, 0                      # wire -> counted layers so far
for name, qubits, clbits, params, payload in ops:          # circuit order
    wires = [("q", q) for q in qubits] + [("c", c) for c in clbits]
    counted = name in native_2q_names and len(qubits) == 2
    new = max(level.get(w, 0) for w in wires) + counted
    for w in wires:
        level[w] = new
    n2 += counted
D2 = max(level.values())
```

This matches `QuantumCircuit.depth(filter_function=...)` semantics, but it is computed by the
harness from the exported file, so the candidate cannot change its own score by changing
`depth()`.

**Example.** On four qubits, `cz(0,1); cz(2,3); rz(1); cz(1,2); cz(0,1)` has `N2 = 4` and
`D2 = 3`: the first two gates share a layer.

**Why `D2` and not total depth.** `D2` approximates duration when two-qubit gates dominate, and
`N2` tracks the dominant gate error. Total depth counts a layer of virtual, zero-duration `rz`
gates the same as a layer of entangling gates, so it can fall without any physical benefit.
A routing SWAP translated into three `cz` adds 3 to `N2` but adds to `D2` only on the critical
path. That is why a candidate could buy depth with extra gates, and why `N2` is guarded.

`D2` is undefined for dynamic circuits (control flow). Those appear only in the correctness
fixtures, never in scored cases.

The same streaming pass also runs the **C0 structural check** (`StructuralChecker`) and
computes the SHA-256 of the output. See [verifier.md](verifier.md#c0-structure-every-output).

## 2. One case: the per-case ratio

For a positive metric `m` (`D2` or `N2`), case `c` and the 100 seeds `s` of block B0:

```text
d(c, s)       = ln m_evolved(c, s) − ln m_baseline(c, s)       (paired by seed)
case_ratio(c) = exp( mean_s d(c, s) )
              = geomean_s m_evolved(c, s) / geomean_s m_baseline(c, s)
```

Logarithms are used because cases differ in size by orders of magnitude, effects are
multiplicative, and a ratio and its inverse should count symmetrically. A ratio of 0.98 means
a 2% reduction in the geometric mean.

## 3. A panel: the score and its paired standard error

A panel is a set of cases with frozen weights `w_c` that sum to one (the scored panel, a basis
panel, a family, a level). For each seed, collapse the panel to one number:

```text
delta_s   = Σ_c w_c · d(c, s)                  weighted log change at seed s
ln(score) = mean_s delta_s
score     = exp(ln(score))
SE        = sample_sd(delta_s) / sqrt(|S|)     (ddof = 1, |S| = 100)
```

Because each seed's delta already mixes all cases, the standard error keeps whatever
correlation exists between cases and between the two revisions at a shared seed. It does not
assume independence. Implemented in `paired_panel()`.

**Cases with different seed counts.** A scored case may use 50 or 100 seeds
(`SCORED_SEED_COUNTS`; the loader refuses others). Each case `c` then uses seeds
`0..n_c − 1`:

```text
ln(score) = Σ_c w_c · mean_{s < n_c} d(c, s)        each case over its own seeds
X_s       = Σ_{c with seed s} w_c · d(c, s) / n_c
SE²       = Σ_strata n_stratum · sample_var(X_s over the stratum)
```

Seeds are grouped into strata of identical case availability (0–49 and 50–99 when some cases
have 50 seeds). Cases that share a seed keep their covariance. With every case at 100 seeds
there is one stratum and the result is exactly the formula above. A stratum of fewer than two
seeds, or a missing seed of any case, makes the record `unresolved`; nothing is filled or
reweighted, and mixed counts alone never make a record unresolved. The report shows a mixed
panel's seed range ("50–100 per case"), and each panel summary in `decision.json` carries
`seed_counts` and `strata`. The report-only instance bootstrap resamples seeds within each
stratum. The shipped profiles still use 100 seeds on every scored case.

**Worked example** (three cases, weights 1/3, four seeds; `tests/test_statistics.py`
reproduces these digits):

| Case | Baseline, seeds 1–4 | Evolved, seeds 1–4 | `case_ratio` |
| --- | --- | --- | --- |
| A | 400, 420, 380, 410 | 390, 400, 385, 395 | 0.9757 |
| B | 1500, 1550, 1480, 1600 | 1490, 1500, 1500, 1540 | 0.9841 |
| C | 1850, 1800, 1900, 1820 | 1800, 1810, 1830, 1790 | 0.9812 |

`delta_s = (−0.01980, −0.02535, −0.00368, −0.03070)`, so `ln(score) = −0.01988`,
`score = 0.9803`, `SE = 0.00584` and `ln(score) + 2·SE = −0.00820 < 0`. This counts as an
improvement.

### Weights

- **Iterations profile:** the three scored cases have weight 1/3 each. The basis panels use
  equal weights.
- **Confirm profile:** a tree. Each family gets 1/8, split equally by level, then size band,
  topology, basis, input group and variant (`hierarchical_weights()`). The weights are frozen
  in the manifest; `load_profile()` refuses scored weights that do not sum to 1.
- **Sub-panels** (a family, a level): restrict to member cases and renormalize.

## 4. Decision rules

| Rule | Inequality | Used for |
| --- | --- | --- |
| Improvement, iterations | `ln(D2 score) + 2·SE < 0` | `IA2/improvement` |
| Improvement, confirm | `ln(D2 score) + 2·SE < ln(0.99)` | `CA2/improvement` (at least 1% practical gain) |
| Breadth, confirm | ≥ 4 of 8 families with `ln(D2) + 2·SE < 0`, and every leave-one-family-out score < 1 | `CA3/breadth` |
| Regression guard, overall and basis panels | `ln(score) ≤ 3·SE` | `N2` of the scored panel; `D2` and `N2` of basis panels |
| Regression guard, confirm family and level | `ln(score) ≤ ln(1.05) + 3·SE` | `D2` and `N2` of each family and optimization level |
| Per-case quality cap | `case_ratio ≤ 2.0` (seed-aggregated, not per seed); with `quality_cap_rule: noise_aware` a case breaches only when also `ln(case_ratio) > 3·SE_case` | Every scored and guard case, `D2` and `N2` |
| Per-case SE guard | `ln(case_ratio) ≤ ln(1.15) + 3·SE_case` in confirm; zero margin in iterations | Guard cases outside a basis panel |
| Deterministic | Evolved `≤ 1.25 × baseline` on every seed in confirm; evolved `≤ baseline` in iterations | Ordinary deterministic cases, each of `D2` and `N2` independently |
| Exact zero | Evolved `= 0` | Zero-baseline cases |
| Canary | Both revisions `=` the frozen constant on every seed | Canaries |

The multipliers (2.0 for improvement, 3.0 for guards), the practical ratio and the cap come
from `policy.json`.

**Practical guard margins (confirm version 11).** `quality_guard_tolerances` contains
fractional allowances: `aggregate: 0.05` for family and optimization-level summaries,
`individual: 0.15` for guard cases outside basis panels, and `deterministic: 0.25` for
ordinary deterministic cases. A statistical guard fails only when
`ln(ratio) - guard_multiplier · SE > ln(1 + tolerance)`. This is a margin beyond seed
uncertainty, not a hard maximum on the observed ratio. The independent per-case quality
cap still applies. The deterministic allowance is a hard per-seed bound with no averaging
or compensation between metrics; a zero baseline must remain zero.

Overall N2, basis-panel guards, improvement and breadth, canaries, zero-baseline cases and
correctness checks retain their previous rules. Missing tolerance fields default to zero,
so archived policies and the iterations profile retain strict regression guards. Record
IDs under `CA4/exact/` are retained for compatibility even though ordinary deterministic
cases now have an allowance. Quality reports expose the active tolerances.

**Cap rule.** `quality_cap_rule` is `plain` in both shipped profiles (and for any policy
without the key): a case breaches when its ratio exceeds `quality_cap`. `noise_aware` also
requires `ln(case_ratio) > guard_multiplier · SE_case`, with `SE_case` from the case's paired
per-seed log differences over the same seed block. It reduces false cap breaches when a
neutral change decorrelates seed outcomes, at some loss of sensitivity near 2.0; enable it
only after measuring the false-breach rate and injected regressions around the configured
cap. The original 5% cap proposal is in design/updated_confirm_plan.md, section 7.
Under both rules the independent per-case SE
guard still applies to guard cases outside a basis panel, and each cap record carries
`cap_rule`.

**Why 3·SE for guards.** A profile has many guards. At 2·SE each would reject a neutral
candidate about 2.3% of the time, and about 45 guards (confirm) would reject a neutral candidate
most of the time. At 3·SE the per-guard rate is about 0.13%. The real, correlated family-wise
rate is not measured (section 5).

### Missing values and zeros

- A panel with any missing observation, or with a non-positive value in a log metric, is
  **incomplete**. Its record is `unresolved`, never `passed` or `failed`.
- Guards and improvement are evaluated only when `harness/roundtrip` and `audit/determinism`
  both passed. Otherwise every quality record is `unresolved`.
- Failures are never dropped from a denominator, and weights are never silently changed.

## 5. Quality runtime

Every quality case and B0 seed receives a fresh uninstrumented end-to-end transpilation
on both revisions. A single warmup per case/revision is excluded. Imports, fixture loading,
output serialization and checks are outside the clock. Workers run sequentially; arm order
alternates across cases. Machine load is neither monitored nor corrected.

For each case, reports show arithmetic mean seconds for each revision, the difference
`evolved - baseline`, ratio `evolved / baseline`, and improvement percentage
`100 * (baseline - evolved) / baseline`. Aggregate seconds sum the entire paired workload;
the ratio of those sums is workload-weighted. The geometric mean case ratio is also shown.
Missing or invalid measurements are counted and leave the aggregate unavailable.

Runtime differences are descriptive. `quality/runtime` requires complete measurements,
without applying the retired cost-panel thresholds. Memory panels, C1–C5, C7 and stage
coverage requirements are removed from current profiles.

## 6. Benchpress

Benchpress compares D2/N2 and a single-call `pm.run` runtime on 50 frozen circuit/target
entries at levels 2 and 3 and seeds 0, 1 and 2. It reports per-case median seed differences
and ratios, and aggregate comparisons by level. Preset construction is outside its timer.
These numbers are descriptive; `benchpress/completeness` requires the panel to finish.

## 8. From records to verdict

Every check produces a **constraint record**: `{id, kind, subject, result, ...}`.

- `kind`: `harness`, `correctness`, `guard`, `cost`, `improvement`, `completeness`
- `subject`: `evolved` (the candidate or the comparison) or `reference` (the baseline alone)
- `result`: `passed`, `failed`, `passed_on_rerun`, `unresolved`, `not_evaluated`

The set of required IDs comes from the policy, plus every panel the evaluator creates, plus
the cost panels the change scope requires, plus `*1/upstream` once the optional `unit-tests`
stage has started. It never depends on which records happen to exist. The verdict
(`evaluator.verdict()`) is decided in this order:

```text
any failed harness record                        → ERROR                 (exit 40)
any failed evolved record that is not improvement → CONSTRAINT_VIOLATION  (exit 20)
any failed reference record                       → INCONCLUSIVE          (exit 30)
any failed improvement record                     → NO_IMPROVEMENT        (exit 10)
every required ID present and passed              → PASS                  (exit 0)
otherwise (missing / unresolved)                  → INCONCLUSIVE          (exit 30)
```

`verdict()` checks evolved failures before reference failures, so two rules keep a failure
the candidate did not cause from reading as a violation, without weakening that order:

- **Shared failures.** When both revisions fail C0 or C6 on the same case and seed
  with the same failure witness, the evolved side is not recorded as a failure; the baseline
  side stays a failed reference record, and the aggregate record stays `unresolved` and names
  the pair. The verdict is `INCONCLUSIVE`. With both sides merely `unresolved`, a failed
  improvement record would have given `NO_IMPROVEMENT`. Any other evolved failure stays
  failed ([quality](workflow/quality.md#failures-both-revisions-share)).
- **Timeouts.** A candidate quality seed that timed out is `failed` only when it timed out
  twice (the second time alone, in a fresh process), the same baseline seed completed within
  half the timeout without a retry, and the host was quiet at both timeouts; otherwise it is
  `unresolved` ([quality](workflow/quality.md#timeouts)).

In the correctness stage a failed baseline preflight already stops the evolved checks and
gives `INCONCLUSIVE`.

A missing record can never produce `PASS`. There is no qualification record: a session ends
`PASS` when every required record passes. A/A runs and the known-outcome mutations remain the
way to validate the harness on a new runner
([known-outcome controls](validation.md#known-outcome-controls)), but they are practices, not
required records.

### Workflow conditions

Compile, quality and Benchpress are required. Optional unit-tests binds the verdict once
started. Missing/unfinished required stages keep the verdict inconclusive; harness errors
remain errors. The quality gate controls optional unit-tests only.

## Diagnostics that never enter a decision

Total depth, pipeline fingerprints (pass names and search budgets per stage), per-pass times,
the pass trace of each quality compile, routing SWAP counts from replay, worst seeds and
`U_instance` are recorded for review only. (The determinism audit reads the pass trace to
locate where two compiles of one seed first differ.)
