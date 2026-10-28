# Confirm profile

[Documentation index](README.md) · [Workflow](workflow/README.md)

`confirm-profile`, version 14, has 109 quality cases and
7,390 B0 seeds per revision.
Every case runs at optimization level 2 or 3. There are 31 scored L2 cases and 30
scored L3 cases across eight families. Use `--profile confirm-profile` when compiling a
new session. The workload is public; reserve it for confirmation after iteration.

| Role | Cases |
| --- | ---: |
| scored | 61 |
| zero_baseline | 4 |
| deterministic | 30 |
| guard | 10 |
| canary | 4 |

Quality measures native two-qubit depth and count, checks output legality and routing
replay, audits determinism, and times every case/seed pair using fresh uninstrumented
calls for both revisions. Runtime reports show seconds, differences and ratios. There
are no separate timing/memory panels and no host-load checks.

Scored weights sum to one. Weights split equally by family, then optimization level,
size band, topology, native basis, input group and variant. Removing L0/L1 from confirm
recalculates weights over the retained cases; each family still carries one eighth.

The quality gate uses D2 improvement, N2/basis guards, per-case caps, exact-case checks,
C0/C6, determinism and completeness. Confirm also requires breadth across families and
uses a practical 1% D2 improvement threshold. Confirm family/level guards allow 5% plus
seed uncertainty, individual guards 15% plus uncertainty, ordinary deterministic cases
25%; zero-baseline and canary checks remain strict. The frozen policy is authoritative.

`quality/runtime` requires complete paired runtime observations. Runtime regression is
reported descriptively. `benchpress/completeness` requires the independent Benchpress
panel to finish; its D2/N2/runtime changes are also descriptive. Optional upstream tests
count in the final verdict once started. C1–C5, C7 and the old cost requirements are removed.

The Benchpress stage runs the same 50-entry L2/L3 panel under either profile.
See [quality](workflow/quality.md) and [Benchpress](workflow/benchpress.md) for timing
scope and retained reports. Coverage limitations remain in the manifest and final report.
