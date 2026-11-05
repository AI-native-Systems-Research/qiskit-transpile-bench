"""Canonical weight identities and explicit decay-plus-readiness configurations."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

DEFAULT_WEIGHTS = "0,1e-8,1e-7,1e-6,3e-6,1e-5,2.2e-5,5e-5,1e-4,3e-4,1e-3"
DECAY = {"arm": "decay", "weight": None, "weight_decimal": None}


def parse_weights(weights=DEFAULT_WEIGHTS):
    """Return sorted configurations; reject duplicate decimal or binary coefficients."""
    values = weights.split(",") if isinstance(weights, str) else list(weights)
    if not values:
        raise ValueError("At least one wait-time weight is required")
    parsed, seen = [], set()
    for value in values:
        try:
            decimal = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError(f"Invalid wait-time weight: {value!r}") from exc
        if not decimal.is_finite() or decimal < 0:
            raise ValueError("Wait-time weights must be finite and nonnegative")
        coefficient = float(decimal)
        if not math.isfinite(coefficient) or (decimal != 0 and coefficient == 0):
            raise ValueError(f"Unrepresentable wait-time weight: {value!r}")
        if coefficient in seen:
            raise ValueError(f"Duplicate wait-time weight: {value!r}")
        seen.add(coefficient)
        # normalize() uses the Decimal context's precision; avoid rounding long inputs.
        canonical = format(decimal, "f").rstrip("0").rstrip(".") if decimal else "0"
        if decimal and decimal.as_tuple().exponent >= 0:
            canonical = format(decimal, "f")
        parsed.append(
            {"arm": f"wait_time:{canonical}", "weight": coefficient, "weight_decimal": canonical}
        )
    return sorted(parsed, key=lambda config: config["weight"])


def resolved_components(config, width):
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise ValueError("The physical target width must be a positive integer")
    return {
        "basic": {"weight": 1.0, "scaling": "Constant"},
        "lookahead": {"weights": [0.5 / width], "scaling": "Constant"},
        "decay": {"increment": 0.001, "reset": 5},
        "wait_time": None if config["weight"] is None else {"weight": config["weight"]},
        "attempt_limit": 10 * width,
        "best_epsilon": 1e-10,
        "physical_width": width,
    }


def make_heuristic(config, width):
    """Import the candidate lazily, after collection has selected its frozen build."""
    from qiskit._accelerate.sabre import Heuristic, SetScaling

    components = resolved_components(config, width)
    heuristic = (
        Heuristic(attempt_limit=components["attempt_limit"], best_epsilon=1e-10)
        .with_basic(1.0, SetScaling.Constant)
        .with_lookahead([0.5 / width], SetScaling.Constant)
        .with_decay(0.001, 5)
    )
    if config["weight"] is not None:
        heuristic = heuristic.with_wait_time(config["weight"])
    return heuristic
