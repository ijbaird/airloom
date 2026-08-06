"""Small, dependency-free client for PurpleAir's read API."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .aqi import aqi_from_pm25, epa_corrected_pm25
from .models import Sensor


API_URL = "https://api.purpleair.com/v1/sensors"
FIELDS = (
    "sensor_index",
    "name",
    "latitude",
    "longitude",
    "last_seen",
    "humidity",
    "temperature",
    "pm1.0",
    "pm2.5_cf_1",
    "pm2.5_10minute",
    "pm2.5_30minute",
    "pm2.5_60minute",
    "pm2.5_6hour",
    "pm2.5_24hour",
    "pm2.5_1week",
    "pm10.0",
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
    radius = max(1.0, min(100.0, radius_km))
    lat_delta = radius / 111.0
    lon_scale = max(0.1, math.cos(math.radians(latitude)))
    lon_delta = radius / (111.0 * lon_scale)
    return Bounds(latitude + lat_delta, longitude - lon_delta, latitude - lat_delta, longitude + lon_delta)


class PurpleAirError(RuntimeError):
    pass


class PurpleAirClient:
    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key.strip()
        self.timeout = timeout

    def fetch_sensors(self, bounds: Bounds) -> list[Sensor]:
        if not self.api_key:
            raise PurpleAirError("A PurpleAir read key is required for live data.")
        query = urlencode(
            {
                "fields": ",".join(FIELDS),
                "location_type": 0,
                "nwlat": f"{bounds.north:.6f}",
                "nwlng": f"{bounds.west:.6f}",
                "selat": f"{bounds.south:.6f}",
                "selng": f"{bounds.east:.6f}",
            }
        )
        request = Request(
            f"{API_URL}?{query}",
            headers={"X-API-Key": self.api_key, "User-Agent": "Airloom/0.1"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:
            raise PurpleAirError(f"PurpleAir request failed: {exc}") from exc
        return parse_sensor_payload(payload)


def parse_sensor_payload(payload: dict) -> list[Sensor]:
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        message = payload.get("description") or payload.get("error") or "Unexpected PurpleAir response."
        raise PurpleAirError(str(message))

    sensors: list[Sensor] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        values = dict(zip(fields, row, strict=False))
        lat = _number(values.get("latitude"))
        lon = _number(values.get("longitude"))
        sensor_id = values.get("sensor_index")
        if lat is None or lon is None or sensor_id is None:
            continue

        humidity = _number(values.get("humidity"))
        raw_pm = _number(values.get("pm2.5_cf_1"))
        corrected_pm = epa_corrected_pm25(raw_pm, humidity)
        trend = []
        for label, key in TREND_FIELDS:
            point_pm = epa_corrected_pm25(_number(values.get(key)), humidity)
            trend.append({"label": label, "aqi": aqi_from_pm25(point_pm)})

        temperature = _number(values.get("temperature"))
        # PurpleAir documents the temperature as being about 8°F above ambient
        # because the sensor electronics warm the enclosure.
        ambient_temperature = temperature - 8.0 if temperature is not None else None
        sensors.append(
            Sensor(
                sensor_id=int(sensor_id),
                name=str(values.get("name") or f"Sensor {sensor_id}"),
                latitude=lat,
                longitude=lon,
                aqi=aqi_from_pm25(corrected_pm),
                pm25=_rounded(corrected_pm),
                temperature_f=_rounded(ambient_temperature),
                humidity=_rounded(humidity),
                pm1=_rounded(_number(values.get("pm1.0"))),
                pm10=_rounded(_number(values.get("pm10.0"))),
                last_seen=int(values["last_seen"]) if values.get("last_seen") else None,
                trend=trend,
            )
        )
    return sensors


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
