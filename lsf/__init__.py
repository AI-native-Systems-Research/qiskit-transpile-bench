"""LSF orchestration for benchmark sessions.

The launcher submits a manager, which runs compile, quality, Benchpress and optional
unit tests, then decides and cleans the session. The allocation adapter supplies worker
counts and invocation identity. Legacy cost-monitor modules remain for historical evidence.
"""

from pathlib import Path

from qtb.canonical import digest, file_hash

# The modules whose code decides whether cost evidence is admissible.
MEASUREMENT_MODULES = ("context.py", "cost_monitor.py", "cost_evidence.py")


def measurement_identity():
    """Content identity of the measurement-facing modules, recorded with every bundle."""
    root = Path(__file__).resolve().parent
    return digest({name: file_hash(root / name) for name in MEASUREMENT_MODULES})


def lsf_directory(session):
    """Orchestration records of a session: a sibling of it, outside its cleanup scope."""
    session = Path(session).resolve()
    return session.with_name(session.name + ".lsf")
