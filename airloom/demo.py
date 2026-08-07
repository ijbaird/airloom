"""Deterministic demo readings used until a PurpleAir read key is configured."""

from __future__ import annotations

import math
import time

from .aqi import aqi_from_pm25, band_for_aqi
from .models import Sensor


NAMES = (
    "Alberta Arts",
    "Laurelhurst Park",
    "Mount Tabor",
    "Sellwood Garden",
    "Buckman School",
    "Overlook Bluff",
    "St. Johns North",
    "Hawthorne Ridge",
    "Woodstock Library",
    "Council Crest",
    "Irvington Air",
    "Rose City Park",
    "Brooklyn Yard",
    "Kenton Community",
    "South Waterfront",
    "Cully Grove",
    "Montavilla East",
    "Lents Green Ring",
)


def demo_sensors(center_lat: float, center_lon: float) -> list[Sensor]:
    sensors: list[Sensor] = []
    now = int(time.time())
    for index, name in enumerate(NAMES):
        ring = 0.018 + (index % 4) * 0.012
        angle = index * 2.399963
        lat = center_lat + math.sin(angle) * ring
        lon = center_lon + math.cos(angle) * ring * 1.35
        pm = max(1.5, 4.8 + (index * 4.7) % 33 + math.sin(index) * 4.0)
        aqi = aqi_from_pm25(pm)
        trend = []
        for point, label in enumerate(("1w", "1d", "6h", "1h", "30m", "10m", "Now")):
            shifted = max(1.0, pm + math.sin(index * 0.8 + point * 0.9) * 5.5)
            trend.append({"label": label, "aqi": aqi_from_pm25(shifted)})
        sensors.append(
            Sensor(
                sensor_id=900000 + index,
                name=name,
                latitude=round(lat, 6),
                longitude=round(lon, 6),
                aqi=aqi,
                pm25=round(pm, 1),
                temperature_f=round(64 + (index * 3) % 17 + math.sin(index) * 2, 1),
                humidity=round(42 + (index * 7) % 32, 1),
                pm1=round(pm * 0.67, 1),
                pm10=round(pm * 1.28, 1),
                last_seen=now - (index % 6) * 45,
                trend=trend,
                indoor=index % 5 == 2,
            )
        )
    # Keep the summary pleasantly varied while making demo results deterministic.
    sensors.sort(key=lambda item: (band_for_aqi(item.aqi).aqi_low, item.name))
    return sensors

