"""Minimal, dependency-free Nominatim client for place search and labels."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__


SEARCH_URL = "https://nominatim.openstreetmap.org/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim usage policy: at most one request per second, identifying UA.
_MIN_INTERVAL = 1.0
_last_request = 0.0
_throttle_lock = threading.Lock()


class GeocodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    latitude: float
    longitude: float


def search(query: str, limit: int = 5, timeout: int = 10) -> list[Place]:
    query = query.strip()
    if not query:
        return []
    payload = _request(SEARCH_URL, {"q": query, "format": "jsonv2", "limit": str(limit)}, timeout)
    if not isinstance(payload, list):
        raise GeocodeError("Unexpected geocoder response.")
    places = []
    for row in payload:
        if isinstance(row, dict) and (place := _place_from(row)):
            places.append(place)
    return places


def reverse(latitude: float, longitude: float, timeout: int = 10) -> str:
    # Zoom 14 resolves small towns (village/town keys) that city-level zoom
    # collapses into their county; the key preference below still favors the
    # town/city name over broader divisions for urban areas.
    payload = _request(
        REVERSE_URL,
        {"lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}", "format": "jsonv2", "zoom": "14"},
        timeout,
    )
    if isinstance(payload, dict):
        address = payload.get("address")
        if isinstance(address, dict):
            for key in ("village", "town", "city", "municipality", "county", "state"):
                value = address.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:80]
        name = payload.get("name") or payload.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.split(",")[0].strip()[:80]
    return f"{latitude:.2f}, {longitude:.2f}"


def _place_from(row: dict) -> Place | None:
    try:
        latitude = float(row["lat"])
        longitude = float(row["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    name = row.get("display_name") or row.get("name") or ""
    if not isinstance(name, str) or not name.strip():
        return None
    parts = [part.strip() for part in name.split(",")]
    return Place(", ".join(parts[:3])[:80], latitude, longitude)


def _request(url: str, params: dict, timeout: int):
    _throttle()
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": f"Airloom/{__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:
        raise GeocodeError(f"Place lookup failed: {exc}") from exc


def _throttle() -> None:
    global _last_request
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
