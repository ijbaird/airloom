"""Small, dependency-free client for PurpleAir's read API."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import __version__
from .aqi import aqi_from_pm25, epa_corrected_pm25
from .geography import Bounds, bounds_around, bounds_contains, split_bounds
from .models import Sensor, plausible_observation_time
from .validation import sensor_id


API_URL = "https://api.purpleair.com/v1/sensors"
MAP_FIELDS = (
    "sensor_index",
    "name",
    "latitude",
    "longitude",
    "last_seen",
    "location_type",
    "humidity",
    "temperature",
    "pm1.0",
    "pm2.5_cf_1",
    "pm10.0",
    "confidence",
)
DATA_FIELDS = (
    "sensor_index",
    "last_seen",
    "humidity",
    "temperature",
    "pm1.0",
    "pm2.5_cf_1",
    "pm10.0",
    "confidence",
)
TREND_FETCH_FIELDS = (
    "sensor_index",
    "humidity",
    "location_type",
    "pm2.5_cf_1",
    "pm2.5_10minute",
    "pm2.5_30minute",
    "pm2.5_60minute",
    "pm2.5_6hour",
    "pm2.5_24hour",
    "pm2.5_1week",
)
TREND_FIELDS = (
    ("1w", "pm2.5_1week"),
    ("1d", "pm2.5_24hour"),
    ("6h", "pm2.5_6hour"),
    ("1h", "pm2.5_60minute"),
    ("30m", "pm2.5_30minute"),
    ("10m", "pm2.5_10minute"),
    ("Now", "pm2.5_cf_1"),
)


MAX_FETCH_SPAN_KM = 200.0
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def cap_bounds(bounds: Bounds, center: tuple[float, float]) -> Bounds:
    """Clamp a viewport to a fetchable area so one zoomed-out scroll can't
    request thousands of sensor rows."""
    height_km = (bounds.north - bounds.south) * 111.0
    mid_lat = (bounds.north + bounds.south) / 2
    longitude_span = sum(part.east - part.west for part in split_bounds(bounds))
    width_km = longitude_span * 111.0 * max(0.1, math.cos(math.radians(mid_lat)))
    if height_km <= MAX_FETCH_SPAN_KM and width_km <= MAX_FETCH_SPAN_KM:
        return bounds
    return bounds_around(center[0], center[1], MAX_FETCH_SPAN_KM / 2)


class PurpleAirError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FetchResult:
    rows: list[dict]
    time_stamp: int | None


class PurpleAirClient:
    def __init__(self, api_key: str, timeout: int = 20,
                 clock: Callable[[], float] = time.time):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.clock = clock

    def fetch_rows(
        self,
        bounds: Bounds | None = None,
        show_only: list[int] | None = None,
        location_filter: str = "outdoor",
        fields: tuple[str, ...] = MAP_FIELDS,
        modified_since: int | None = None,
    ) -> FetchResult:
        if not self.api_key:
            raise PurpleAirError("A PurpleAir read key is required for live data.")
        if show_only:
            show_only = [i for i in (sensor_id(value) for value in show_only) if i is not None]
        if bounds is None and not show_only:
            raise PurpleAirError("A sensor query needs bounds or sensor ids.")
        params: dict[str, str] = {"fields": ",".join(fields)}
        if show_only:
            params["show_only"] = ",".join(str(sensor_id) for sensor_id in show_only)
        else:
            location_type = {"outdoor": "0", "indoor": "1"}.get(location_filter)
            if location_type is not None:
                params["location_type"] = location_type
            params.update(
                {
                    "nwlat": f"{bounds.north:.6f}",
                    "nwlng": f"{bounds.west:.6f}",
                    "selat": f"{bounds.south:.6f}",
                    "selng": f"{bounds.east:.6f}",
                }
            )
        if modified_since is not None:
            params["modified_since"] = str(modified_since)
        query = urlencode(params)
        request = Request(
            f"{API_URL}?{query}",
            headers={"X-API-Key": self.api_key, "User-Agent": f"Airloom/{__version__}"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:
            raise PurpleAirError(f"PurpleAir request failed: {exc}") from exc
        try:
            return parse_rows(payload, now=self.clock())
        except PurpleAirError:
            raise
        except Exception as exc:
            raise PurpleAirError(f"PurpleAir returned unparseable data: {exc}") from exc

    def fetch_sensors(
        self,
        bounds: Bounds | None = None,
        show_only: list[int] | None = None,
        location_filter: str = "outdoor",
    ) -> list[Sensor]:
        result = self.fetch_rows(bounds=bounds, show_only=show_only, location_filter=location_filter)
        return _sensors_from_rows(result.rows, now=self.clock())


def parse_rows(payload: dict, requested: tuple[str, ...] | None = None, *,
               now: float | None = None) -> FetchResult:
    """Pair each data row with its field names. Observation times and the
    checkpoint stamp too far in the future to be clock skew become missing."""
    if now is None:
        now = time.time()
    if not isinstance(payload, dict):
        raise PurpleAirError("Unexpected PurpleAir response.")
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        message = payload.get("description") or payload.get("error") or "Unexpected PurpleAir response."
        raise PurpleAirError(str(message))
    values_list = [dict(zip(fields, row, strict=False)) for row in rows if isinstance(row, list)]
    for values in values_list:
        if "last_seen" in values:
            values["last_seen"] = plausible_observation_time(_integer(values["last_seen"]), now)
    time_stamp = plausible_observation_time(_integer(payload.get("time_stamp")), now)
    return FetchResult(values_list, time_stamp)


def sensor_from_values(values: dict, now: float | None = None) -> Sensor | None:
    lat = _number(values.get("latitude"))
    lon = _number(values.get("longitude"))
    normalized_id = sensor_id(values.get("sensor_index"))
    if (
        lat is None
        or lon is None
        or normalized_id is None
        or not -90.0 <= lat <= 90.0
        or not -180.0 <= lon <= 180.0
    ):
        return None
    humidity = _number(values.get("humidity"))
    corrected_pm = epa_corrected_pm25(_number(values.get("pm2.5_cf_1")), humidity)
    temperature = _number(values.get("temperature"))
    # PurpleAir documents the temperature as being about 8°F above ambient
    # because the sensor electronics warm the enclosure.
    ambient_temperature = temperature - 8.0 if temperature is not None else None
    return Sensor(
        sensor_id=normalized_id,
        name=str(values.get("name") or f"Sensor {normalized_id}"),
        latitude=lat,
        longitude=lon,
        aqi=aqi_from_pm25(corrected_pm),
        pm25=_rounded(corrected_pm),
        temperature_f=_rounded(ambient_temperature),
        humidity=_rounded(humidity),
        pm1=_rounded(_number(values.get("pm1.0"))),
        pm10=_rounded(_number(values.get("pm10.0"))),
        confidence=_integer(values.get("confidence")),
        last_seen=plausible_observation_time(
            _integer(values.get("last_seen")), time.time() if now is None else now),
        trend=[],
        indoor=_integer(values.get("location_type")) == 1,
    )


def trend_from_values(values: dict) -> list[dict]:
    """Correct historical averages using the sensor's reported basis.

    PurpleAir's simple running averages are ATM outdoors and CF1 indoors
    (API discussion posts 17–19). Applying the current humidity to historical
    averages is an approximation because historical humidity is unavailable.
    """
    humidity = _number(values.get("humidity"))
    average_basis = "CF1" if _integer(values.get("location_type")) == 1 else "ATM"
    trend = []
    for label, key in TREND_FIELDS:
        basis = "CF1" if label == "Now" else average_basis
        point_pm = epa_corrected_pm25(_number(values.get(key)), humidity, basis=basis)
        trend.append({"label": label, "aqi": aqi_from_pm25(point_pm)})
    return trend


def _sensors_from_rows(rows: list[dict], now: float | None = None) -> list[Sensor]:
    sensors = []
    for values in rows:
        sensor = sensor_from_values(values, now)
        if sensor is not None:
            sensor.trend = trend_from_values(values)
            sensors.append(sensor)
    return sensors


def parse_sensor_payload(payload: dict, now: float | None = None) -> list[Sensor]:
    return _sensors_from_rows(parse_rows(payload, now=now).rows, now)


def _number(value) -> float | None:
    # JSON booleans are never measurements, though Python treats them as ints.
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value) if _INT64_MIN <= value <= _INT64_MAX else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not parsed.is_finite()
        or parsed < _INT64_MIN
        or parsed > _INT64_MAX
        or parsed != parsed.to_integral_value()
    ):
        return None
    return int(parsed)


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
