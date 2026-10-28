"""Canonical, dependency-free geographic primitives."""

from __future__ import annotations

import math
from dataclasses import dataclass


def _finite(value: float, name: str) -> float:
    try:
        valid = math.isfinite(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not valid:
        raise ValueError(f"{name} must be a finite number")
    return value


def _latitude(value: float, name: str) -> float:
    _finite(value, name)
    if not -90.0 <= value <= 90.0:
        raise ValueError(f"{name} must be between -90 and 90")
    return value


def _longitude(value: float, name: str) -> float:
    _finite(value, name)
    if not -180.0 <= value <= 180.0:
        raise ValueError(f"{name} must be between -180 and 180")
    return value


@dataclass(frozen=True, slots=True)
class Bounds:
    north: float
    west: float
    south: float
    east: float

    def __post_init__(self) -> None:
        _latitude(self.north, "north")
        _latitude(self.south, "south")
        _longitude(self.west, "west")
        _longitude(self.east, "east")
        if self.north < self.south:
            raise ValueError("north must not be below south")


def _wrap_longitude(longitude: float) -> float:
    return (longitude + 180.0) % 360.0 - 180.0


def bounds_around(latitude: float, longitude: float, radius_km: float) -> Bounds:
    _latitude(latitude, "latitude")
    _longitude(longitude, "longitude")
    _finite(radius_km, "radius_km")
    radius = max(2.0, min(100.0, radius_km))
    lat_delta = radius / 111.0
    lon_scale = max(0.1, math.cos(math.radians(latitude)))
    lon_delta = radius / (111.0 * lon_scale)
    return Bounds(
        min(90.0, latitude + lat_delta),
        _wrap_longitude(longitude - lon_delta),
        max(-90.0, latitude - lat_delta),
        _wrap_longitude(longitude + lon_delta),
    )


def split_bounds(bounds: Bounds) -> tuple[Bounds, ...]:
    """Split antimeridian-crossing bounds into canonical query rectangles."""
    if bounds.west <= bounds.east:
        return (bounds,)
    return (
        Bounds(bounds.north, bounds.west, bounds.south, 180.0),
        Bounds(bounds.north, -180.0, bounds.south, bounds.east),
    )


def bounds_contains(outer: Bounds, inner: Bounds) -> bool:
    """True when `inner` lies entirely within `outer`."""
    outer_parts = split_bounds(outer)
    for inner_part in split_bounds(inner):
        if not any(
            inner_part.north <= outer_part.north
            and inner_part.south >= outer_part.south
            and inner_part.west >= outer_part.west
            and inner_part.east <= outer_part.east
            for outer_part in outer_parts
        ):
            return False
    return True


def nearest_longitude(longitude: float, reference: float) -> float:
    """Project a canonical longitude into the world nearest `reference`."""
    _longitude(longitude, "longitude")
    _finite(reference, "reference")
    world = math.floor((reference - longitude) / 360.0 + 0.5)
    return float(longitude + 360.0 * world)
