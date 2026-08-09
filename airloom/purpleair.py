"""Small, dependency-free client for PurpleAir's read API."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import __version__
from .aqi import aqi_from_pm25, epa_corrected_pm25
from .models import Sensor


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
)
DATA_FIELDS = (
    "sensor_index",
    "last_seen",
    "humidity",
    "temperature",
    "pm1.0",
    "pm2.5_cf_1",
    "pm10.0",
)
TREND_FETCH_FIELDS = (
    "sensor_index",
    "humidity",
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


@dataclass(frozen=True, slots=True)
class Bounds:
    north: float
    west: float
    south: float
    east: float


def bounds_around(latitude: float, longitude: float, radius_km: float) -> Bounds:
    radius = max(2.0, min(100.0, radius_km))
    lat_delta = radius / 111.0
    lon_scale = max(0.1, math.cos(math.radians(latitude)))
    lon_delta = radius / (111.0 * lon_scale)
    return Bounds(
        min(90.0, latitude + lat_delta),
        max(-180.0, longitude - lon_delta),
        max(-90.0, latitude - lat_delta),
        min(180.0, longitude + lon_delta),
    )


def bounds_contains(outer: Bounds, inner: Bounds) -> bool:
    """True when `inner` lies entirely within `outer`."""
    return (
        inner.north <= outer.north
        and inner.south >= outer.south
        and inner.west >= outer.west
        and inner.east <= outer.east
    )


MAX_FETCH_SPAN_KM = 200.0


def cap_bounds(bounds: Bounds, center: tuple[float, float]) -> Bounds:
    """Clamp a viewport to a fetchable area so one zoomed-out scroll can't
    request thousands of sensor rows."""
    height_km = (bounds.north - bounds.south) * 111.0
    mid_lat = (bounds.north + bounds.south) / 2
    width_km = (bounds.east - bounds.west) * 111.0 * max(0.1, math.cos(math.radians(mid_lat)))
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
    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key.strip()
        self.timeout = timeout

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
            show_only = [i for i in (_integer(value) for value in show_only) if i is not None]
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
            return parse_rows(payload)
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
        return _sensors_from_rows(result.rows)


def parse_rows(payload: dict) -> FetchResult:
    if not isinstance(payload, dict):
        raise PurpleAirError("Unexpected PurpleAir response.")
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        message = payload.get("description") or payload.get("error") or "Unexpected PurpleAir response."
        raise PurpleAirError(str(message))
    values_list = [dict(zip(fields, row, strict=False)) for row in rows if isinstance(row, list)]
    return FetchResult(values_list, _integer(payload.get("time_stamp")))


def sensor_from_values(values: dict) -> Sensor | None:
    lat = _number(values.get("latitude"))
    lon = _number(values.get("longitude"))
    sensor_id = _integer(values.get("sensor_index"))
    if lat is None or lon is None or sensor_id is None:
        return None
    humidity = _number(values.get("humidity"))
    corrected_pm = epa_corrected_pm25(_number(values.get("pm2.5_cf_1")), humidity)
    temperature = _number(values.get("temperature"))
    # PurpleAir documents the temperature as being about 8°F above ambient
    # because the sensor electronics warm the enclosure.
    ambient_temperature = temperature - 8.0 if temperature is not None else None
    return Sensor(
        sensor_id=sensor_id,
        name=str(values.get("name") or f"Sensor {sensor_id}"),
        latitude=lat,
        longitude=lon,
        aqi=aqi_from_pm25(corrected_pm),
        pm25=_rounded(corrected_pm),
        temperature_f=_rounded(ambient_temperature),
        humidity=_rounded(humidity),
        pm1=_rounded(_number(values.get("pm1.0"))),
        pm10=_rounded(_number(values.get("pm10.0"))),
        last_seen=_integer(values.get("last_seen")),
        trend=[],
        indoor=_integer(values.get("location_type")) == 1,
    )


def trend_from_values(values: dict) -> list[dict]:
    humidity = _number(values.get("humidity"))
    trend = []
    for label, key in TREND_FIELDS:
        point_pm = epa_corrected_pm25(_number(values.get(key)), humidity)
        trend.append({"label": label, "aqi": aqi_from_pm25(point_pm)})
    return trend


def _sensors_from_rows(rows: list[dict]) -> list[Sensor]:
    sensors = []
    for values in rows:
        sensor = sensor_from_values(values)
        if sensor is not None:
            sensor.trend = trend_from_values(values)
            sensors.append(sensor)
    return sensors


def parse_sensor_payload(payload: dict) -> list[Sensor]:
    return _sensors_from_rows(parse_rows(payload).rows)


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
