# decide

[Documentation index](../README.md) · [Workflow](README.md)

```bash
uv run qiskit-transpile-bench decide --results-root "$S"
```

Decide reads committed stage evidence and archived profile data, writes `decision.json`,
`evidence.json`, `report.md` and the combined `progress.log`, then attempts cleanup.
It can run after compile creates a session and again after cleanup. It never runs workers.

Required stages are compile, quality and Benchpress. Optional upstream tests count once
started. Missing or unfinished required stages leave the verdict `INCONCLUSIVE`; a stage
crash gives `ERROR`. The quality gate only controls optional upstream tests.

| Verdict | Exit | Meaning |
| --- | ---: | --- |
| PASS | 0 | Quality improvement and every required check passed |
| NO_IMPROVEMENT | 10 | The quality improvement rule failed |
| CONSTRAINT_VIOLATION | 20 | A candidate guard/check failed |
| INCONCLUSIVE | 30 | Missing/unresolved evidence, unfinished stages or baseline failure |
| ERROR | 40 | Harness/build/worker error |

The report includes quality D2/N2 comparisons, paired quality runtime and the separate
Benchpress D2/N2/runtime panel. Runtime differences are descriptive. Removed correctness
and cost stage records do not participate in current sessions.

Cleanup retains observations, paired measurements and reports, and is skipped while
required work is unfinished. A cleanup failure never changes the verdict exit code.
