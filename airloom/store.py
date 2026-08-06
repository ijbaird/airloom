from __future__ import annotations

import json
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


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "airloom" / "config.json"
        self.data = self._load()

    def _load(self) -> dict:
        data = deepcopy(DEFAULT_CONFIG)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        data["favorites"] = [int(item) for item in data.get("favorites", []) if str(item).isdigit()]
        if not isinstance(data.get("alert_states"), dict):
            data["alert_states"] = {}
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def public_config(self) -> dict:
        return {
            "latitude": self.data["latitude"],
            "longitude": self.data["longitude"],
            "location_name": self.data["location_name"],
            "radius_km": self.data["radius_km"],
            "temperature_unit": self.data["temperature_unit"],
            "alert_threshold": self.data["alert_threshold"],
            "has_api_key": bool(self.data.get("api_key")),
            "api_key_hint": f"••••{self.data['api_key'][-4:]}" if self.data.get("api_key") else "",
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

