from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from pathlib import Path


DEFAULT_CONFIG = {
    "api_key": "",
    "latitude": 45.5152,
    "longitude": -122.6784,
    "location_name": "Portland, Oregon",
    "radius_km": 22.0,
    "temperature_unit": "F",
    "alert_threshold": 101,
    "favorites": [],
    "alert_states": {},
}


def _default_config_dir() -> Path:
    # An empty or relative XDG_CONFIG_HOME must be ignored per the XDG spec;
    # honoring it would write the API key relative to the current directory.
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return base / "airloom"


def _sanitize(data: dict) -> dict:
    clean = deepcopy(DEFAULT_CONFIG)

    def number(key, low, high):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value) and low <= value <= high:
                clean[key] = value

    number("latitude", -90.0, 90.0)
    number("longitude", -180.0, 180.0)
    number("radius_km", 2.0, 100.0)
    number("alert_threshold", 1, 500)
    clean["alert_threshold"] = int(clean["alert_threshold"])
    if isinstance(data.get("api_key"), str):
        clean["api_key"] = data["api_key"].strip()
    if isinstance(data.get("location_name"), str) and data["location_name"].strip():
        clean["location_name"] = data["location_name"].strip()[:80]
    if data.get("temperature_unit") in ("F", "C"):
        clean["temperature_unit"] = data["temperature_unit"]
    favorites = data.get("favorites")
    if isinstance(favorites, list):
        clean["favorites"] = sorted(
            {
                int(item)
                for item in favorites
                if (isinstance(item, (int, float)) and not isinstance(item, bool))
                or (isinstance(item, str) and item.isdigit())
            }
        )
    if isinstance(data.get("alert_states"), dict):
        clean["alert_states"] = {str(key): bool(value) for key, value in data["alert_states"].items()}
    return clean


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or _default_config_dir() / "config.json"
        self.data = self._load()

    def _load(self) -> dict:
        loaded: dict = {}
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                loaded = parsed
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            pass
        return _sanitize(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        # The config holds the API key, so it must never touch disk with
        # permissive modes — create the file 0600 from the start.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.data, indent=2, sort_keys=True))
        temporary.replace(self.path)

    def public_config(self) -> dict:
        api_key = self.data.get("api_key")
        has_key = isinstance(api_key, str) and bool(api_key)
        return {
            "latitude": self.data["latitude"],
            "longitude": self.data["longitude"],
            "location_name": self.data["location_name"],
            "radius_km": self.data["radius_km"],
            "temperature_unit": self.data["temperature_unit"],
            "alert_threshold": self.data["alert_threshold"],
            "has_api_key": has_key,
            "api_key_hint": f"••••{api_key[-4:]}" if has_key and len(api_key) >= 8 else "",
        }

    def toggle_favorite(self, sensor_id: int) -> bool:
        favorites = set(self.data.get("favorites", []))
        if sensor_id in favorites:
            favorites.remove(sensor_id)
            enabled = False
        else:
            favorites.add(sensor_id)
            enabled = True
        self.data["favorites"] = sorted(favorites)
        self.save()
        return enabled
