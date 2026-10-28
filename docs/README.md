# Documentation

The [manual workflow](workflow/README.md) describes the stage graph and commands.
The [LSF guide](cluster.md) covers cluster jobs, resources and resume behavior.

| Stage | Purpose |
| --- | --- |
| [compile](workflow/compile.md) | Build both revisions and round-trip quality inputs |
| [quality](workflow/quality.md) | D2/N2, C0/C6, determinism, paired runtime and the quality gate |
| [benchpress](workflow/benchpress.md) | External circuit quality and runtime panel, L2/L3 only |
| [unit-tests](workflow/unit-tests.md) | Optional upstream tests when the quality gate opens |
| [decide](workflow/decide.md) | Merge committed evidence and write the verdict |
| [clean](workflow/clean.md) | Remove bulk session files and retain reports |

Profiles: [iterations](iterations-profile.md), [confirm](confirm-profile.md).
Technical references: [environments](environments.md), [sessions](sessions.md),
[baseline store](store.md), [metrics](metrics.md), [output formats](output-format.md),
[architecture](architecture.md), [versioning](versioning.md).

The standalone correctness and cost stages are retired. Their legacy implementation and
archived report readers remain for historical artifacts; current sessions run neither.
The [verifier reference](verifier.md), [validation history](validation.md),
[issue history](fable-issues-resolution.md) and [design plans](../design/) include historical
protocols. Current commands and profiles are described in the guides above.
