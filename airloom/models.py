from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .aqi import band_for_aqi


@dataclass(slots=True)
class Sensor:
    sensor_id: int
    name: str
    latitude: float
    longitude: float
    aqi: int | None
    pm25: float | None
    temperature_f: float | None = None
    humidity: float | None = None
    pm1: float | None = None
    pm10: float | None = None
    confidence: int | None = None
    last_seen: int | None = None
    trend: list[dict[str, int | str | None]] = field(default_factory=list)
    favorite: bool = False
    indoor: bool = False
    origin: str = "live"
    delivery: str = "network"

    def to_dict(self) -> dict:
        result = asdict(self)
        band = band_for_aqi(self.aqi)
        result.update(
            {
                "id": result.pop("sensor_id"),
                "category": band.label,
                "color": band.color,
                "foreground": band.foreground,
                "guidance": band.guidance,
            }
        )
        return result


CONFIDENCE_THRESHOLD = 90


def passes_confidence(sensor: Sensor, enabled: bool) -> bool:
    """Display-time filter for poor A/B channel agreement. Fails open on a
    missing score (demo sensors, rows cached before the field existed)."""
    if not enabled or sensor.confidence is None:
        return True
    return sensor.confidence >= CONFIDENCE_THRESHOLD


MAX_OBSERVATION_AGE = 2 * 3600
FUTURE_SKEW_TOLERANCE = 10 * 60


def plausible_observation_time(value: int | None, now: float) -> int | None:
    """Drop an observation time too far in the future to be clock skew.
    Old times stay plausible; freshness is a separate question."""
    if value is None or value > now + FUTURE_SKEW_TOLERANCE:
        return None
    return value


def is_fresh_observation(last_seen: int | None, now: float) -> bool:
    """True when ``last_seen`` is at most MAX_OBSERVATION_AGE old and at most
    FUTURE_SKEW_TOLERANCE ahead of ``now``, both bounds inclusive."""
    if last_seen is None:
        return False
    return -FUTURE_SKEW_TOLERANCE <= now - last_seen <= MAX_OBSERVATION_AGE
