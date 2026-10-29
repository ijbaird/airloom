"""Shared validation for external sensor identifiers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def sensor_id(value) -> int | None:
    """Return a positive, signed-64-bit sensor ID, or None for hostile input."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= 2**63 - 1 else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not parsed.is_finite()
        or parsed <= 0
        or parsed > 2**63 - 1
        or parsed != parsed.to_integral_value()
    ):
        return None
    return int(parsed)
