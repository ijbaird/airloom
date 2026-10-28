"""Pure presentation filtering and alert-transition decisions."""

from __future__ import annotations

from .models import Sensor, passes_confidence


MAX_ALERT_OBSERVATION_AGE = 2 * 3600


def visible_sensors(
    sensors: list[Sensor],
    *,
    hidden_ids: set[int],
    confidence_filter: bool,
    location_filter: str,
) -> list[Sensor]:
    """Return sensors eligible for display without changing their order."""
    return [
        sensor
        for sensor in sensors
        if sensor.sensor_id not in hidden_ids
        and passes_confidence(sensor, confidence_filter)
        and (
            location_filter == "both"
            or sensor.indoor == (location_filter == "indoor")
        )
    ]


def alert_transitions(
    sensors: list[Sensor],
    *,
    favorites: set[int],
    previous: dict[str, bool],
    threshold: int,
    now: float,
    origin: str,
) -> tuple[list[Sensor], dict[str, bool]]:
    """Return fresh favorite threshold crossings and the next alert state.

    Callers pass display-eligible sensors so hidden, confidence, and location
    rules match presentation. Observation age comes only from ``last_seen``.
    """
    state = dict(previous)
    if origin == "demo":
        return [], state

    favorite_keys = {str(sensor_id) for sensor_id in favorites}
    for key in list(state):
        if key not in favorite_keys:
            del state[key]

    alerted = []
    for sensor in sensors:
        key = str(sensor.sensor_id)
        if (
            key not in favorite_keys
            or sensor.origin == "demo"
            or sensor.aqi is None
            or sensor.last_seen is None
            or now - sensor.last_seen > MAX_ALERT_OBSERVATION_AGE
        ):
            continue

        was_high = state.get(key, False)
        is_high = sensor.aqi >= threshold
        if is_high == was_high:
            continue
        state[key] = is_high
        if is_high:
            alerted.append(sensor)

    return alerted, state
