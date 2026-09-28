# qiskit-transpile-bench: unmodified Qiskit fails the confirm profile against itself

**Reported by:** Foad · 2026-09-28
**Harness:** qiskit-transpile-bench @ `68dd8ac`, run exactly as shipped (no code changes)
**Qiskit:** `33bca5c06d8f7b04f6fbe5fc5a57804452c02078`, built from source
**Environment:** OpenShift Job, 16 CPU, confirm profile; quality stage only (cost and unit-test stages not run)

## Summary

On Qiskit 33bca5c0 with the confirm profile, an A/A session (the baseline compared with an identical copy) returns `CONSTRAINT_VIOLATION`, although the two arms produce identical outputs. All five confirm sessions we ran returned `CONSTRAINT_VIOLATION`: four patches (a CZ-order fix, a fault-tolerant stack, an error-aware placement patch, and a combined stack) plus the A/A. Because the baseline fails C6 on its own, we expect any candidate to get the same verdict.

Three things combine:

1. The C6 routing-replay check fails on the unmodified baseline at optimization levels 1 and 2.
2. With the documented verdict order, a failure the baseline shares is reported as a candidate violation.
3. The determinism audit does not pass, which leaves every panel unresolved.

A likely common cause is suggested at the end.

## Evidence (A/A session)

| | Baseline (33bca5c0) | Identical copy |
|---|---|---|
| Paired compiles | 15,589 | 15,589 |
| Output differences | 0 | 0 |
| C6 routing-replay failures | 1,041 (539 at L1, 502 at L2) | 1,038 (540 at L1, 498 at L2) |
| Verdict | `CONSTRAINT_VIOLATION` | |

This session also had 13 worker timeouts (9 on the baseline, 4 on the copy), caused by our own setup: three sessions shared one pod. They are recorded as failed completeness records, and the 4 on the copy would by themselves also yield `CONSTRAINT_VIOLATION` (see the note at the end). The CZ-order and fault-tolerant confirm sessions had no timeouts and still returned `CONSTRAINT_VIOLATION` from C6 alone: their only failed records are CA1/C6 on the baseline and on the evolved build.

## Issue 1: C6 fails on the baseline at levels 1 and 2

**Observed.** C6 recompiles each seed twice more, stopped after init and after routing, replays routing, and requires the full compile's layout to equal the routing prefix's. On 33bca5c0 this fails at levels 1 and 2 on both arms, and only at those levels, in all five sessions. The failure reason is `Full and routing-prefix layouts differ`. Examples from the A/A session:

- `qaoa_complete_n32` on `rochester_53`, level 2: 100 of 100 seeds on both arms
- `qft_full_n32` on `rochester_53`, level 1: 97 of 100 seeds on both arms

`verifier.md` handles this at level 3 only ("At level 3 a difference is recorded and skipped, because optimization may legitimately re-apply a layout").

**Expected.** Unmodified Qiskit passes C6 against itself.

**Suggested fix (one of):**
- Replay routing from the layout the full compile chose, instead of requiring the two compiles to agree on the layout.
- Or score C6 relative to the baseline: flag the candidate only on cases the baseline passes.

Extending the level-3 skip to levels 1 and 2 would also remove the failures, but it would hide real layout bugs at those levels, so we would not recommend it.

## Issue 2 (design request): shared baseline failures are reported as candidate violations

**Observed.** As documented (`metrics.md`, `evaluator.verdict()`), any failed record on the evolved build gives `CONSTRAINT_VIOLATION`, and this is checked before the rule that a failed record on the reference gives `INCONCLUSIVE`. When both arms fail the same check on the same case, the result is therefore `CONSTRAINT_VIOLATION`. The harness follows its documentation here.

**Suggestion.** For a failure the baseline shares on the same case and check, return `INCONCLUSIVE` and name the check, or use the baseline-relative scoring above. That way the verdict reads as "comparison not valid" rather than as a fault in the candidate.

## Issue 3: the determinism audit does not pass, and every panel stays unresolved

**Observed.** In the CZ-order and fault-tolerant confirm sessions, 370 of 375 constraints ended `unresolved` ("Round-trip or determinism audit evidence is missing"). Panels are gated on the quality prerequisites `harness/roundtrip` and `audit/determinism`. `harness/roundtrip` passed. `audit/determinism` was unresolved in every session, although the audit ran to completion (1,560 recompiles). It needs every recompile to match in status, output hash and layout, and half of the samples run with `PYTHONHASHSEED=1`. So at least one recompile differed, but the message does not say which sample or which field.

**Suggestion.** Report the first differing sample and field (status, output hash or layout) when the audit fails.

## Likely common cause (hypothesis)

The two identical arms give different C6 counts (1,041 and 1,038; per level 539/540 and 502/498). That suggests the full-versus-prefix layout choice is not reproducible from run to run. The same non-reproducibility would make the determinism audit fail, since half its samples use a different hash seed. A quick test: recompile one failing case (`qaoa_complete_n32`, `rochester_53`, level 2) twice, with `PYTHONHASHSEED=0` and `PYTHONHASHSEED=1`, and compare the layouts. If they differ, one cause explains both issue 1 and issue 3.

## Suggested regression test

The docs already recommend A/A runs as known-outcome controls when validating a new runner (`metrics.md`, `validation.md`). Running an A/A in CI on each supported Qiskit revision, and expecting zero constraint failures, would have surfaced them.

## Note: worker timeouts

A worker timeout on the evolved build is a failed completeness record, so it gives `CONSTRAINT_VIOLATION`. An infrastructure timeout may be better treated as `INCONCLUSIVE`.

---

Happy to share the raw session data.