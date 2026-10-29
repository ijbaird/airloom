"""Minimal, dependency-free Nominatim client for place search and labels."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass

from . import __version__


_BASE_URL = "https://nominatim.openstreetmap.org"
SEARCH_URL = f"{_BASE_URL}/search"
REVERSE_URL = f"{_BASE_URL}/reverse"
# Nominatim usage policy: at most one request per second, identifying UA.
_MIN_INTERVAL = 1.0
_CACHE_TTL = 5 * 60.0
_CACHE_MAXSIZE = 64
_last_request = 0.0
_throttle_lock = threading.Lock()


class GeocodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    latitude: float
    longitude: float


class Geocoder:
    def __init__(
        self,
        search_url=None,
        reverse_url=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        base_url = _base_url()
        self.search_url = search_url or _endpoint(base_url, "search")
        self.reverse_url = reverse_url or _endpoint(base_url, "reverse")
        self._clock = clock
        self._sleep = sleep
        self._last_request = None
        self._throttle_lock = threading.Lock()
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._search_lock = threading.Lock()

    def search(self, query: str, limit: int = 5, timeout: int = 10) -> list[Place]:
        query = query.strip()
        if not query:
            return []
        key = (query, limit)
        cached = self._cached(key)
        if cached is not None:
            return cached
        with self._search_lock:
            # A concurrent miss may have populated this key while we waited.
            cached = self._cached(key)
            if cached is not None:
                return cached
            payload = self._request(
                self.search_url,
                {"q": query, "format": "jsonv2", "limit": str(limit)},
                timeout,
            )
            if not isinstance(payload, list):
                raise GeocodeError("Unexpected geocoder response.")
            places = []
            for row in payload:
                if isinstance(row, dict) and (place := _place_from(row)):
                    places.append(place)
            self._store_cached(key, places)
            return places

    def reverse(self, latitude: float, longitude: float, timeout: int = 10) -> str:
        coordinates = _coordinates(latitude, longitude)
        if coordinates is None:
            raise GeocodeError("Invalid geocoder coordinates.")
        latitude, longitude = coordinates
        # Zoom 14 resolves small towns (village/town keys) that city-level zoom
        # collapses into their county; the key preference below still favors the
        # town/city name over broader divisions for urban areas.
        payload = self._request(
            self.reverse_url,
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

    def _cached(self, key):
        now = self._clock()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            cached_at, places = entry
            if now - cached_at >= _CACHE_TTL:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return list(places)

    def _store_cached(self, key, places):
        with self._cache_lock:
            self._cache[key] = (self._clock(), tuple(places))
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_MAXSIZE:
                self._cache.popitem(last=False)

    def _request(self, url: str, params: dict, timeout: int):
        self._throttle()
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": f"Airloom/{__version__}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:
            raise GeocodeError(f"Place lookup failed: {exc}") from exc

    def _throttle(self) -> None:
        with self._throttle_lock:
            if self._last_request is not None:
                wait = _MIN_INTERVAL - (self._clock() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._clock()


class _ModuleGeocoder(Geocoder):
    """Retain the original module-level throttle state for compatibility."""

    def _throttle(self) -> None:
        _throttle()


_default_geocoder = None
_default_base_url = None
_default_lock = threading.Lock()


def _default_client():
    global _default_geocoder, _default_base_url
    base_url = _base_url()
    with _default_lock:
        if _default_geocoder is None or _default_base_url != base_url:
            _default_geocoder = _ModuleGeocoder(
                search_url=_endpoint(base_url, "search"),
                reverse_url=_endpoint(base_url, "reverse"),
            )
            _default_base_url = base_url
        return _default_geocoder


def search(query: str, limit: int = 5, timeout: int = 10) -> list[Place]:
    return _default_client().search(query, limit, timeout)


def reverse(latitude: float, longitude: float, timeout: int = 10) -> str:
    return _default_client().reverse(latitude, longitude, timeout)


def _base_url() -> str:
    return os.environ.get("AIRLOOM_NOMINATIM_URL", "").strip() or _BASE_URL


def _endpoint(base_url: str, operation: str) -> str:
    return f"{base_url.rstrip('/')}/{operation}"


def _coordinates(latitude: float, longitude: float) -> tuple[float, float] | None:
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return latitude, longitude


def _place_from(row: dict) -> Place | None:
    try:
        coordinates = _coordinates(row["lat"], row["lon"])
    except KeyError:
        return None
    if coordinates is None:
        return None
    latitude, longitude = coordinates
    name = row.get("display_name") or row.get("name") or ""
    if not isinstance(name, str) or not name.strip():
        return None
    parts = [part.strip() for part in name.split(",")]
    return Place(", ".join(parts[:3])[:80], latitude, longitude)


def _throttle() -> None:
    global _last_request
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
