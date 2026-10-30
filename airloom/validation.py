"""Shared validation for external sensor identifiers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# IDs cross the bridge as JSON numbers; above JavaScript's
# Number.MAX_SAFE_INTEGER, distinct IDs alias once the web UI parses them.
MAX_SENSOR_ID = 2**53 - 1


def sensor_id(value) -> int | None:
    """Return a positive, JavaScript-safe sensor ID, or None for hostile input."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= MAX_SENSOR_ID else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not parsed.is_finite()
        or parsed <= 0
        or parsed > MAX_SENSOR_ID
        or parsed != parsed.to_integral_value()
    ):
        return None
    return int(parsed)


def bridge_sensor_id(value) -> int | None:
    """Accept only a JSON integer from the web bridge, within the shared domain.

    The web UI always posts IDs it received as numbers; strings, floats, and
    booleans are never legitimate there even when they look integral.
    """
    if type(value) is not int:
        return None
    return sensor_id(value)
