from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path

from .validation import sensor_id


DEFAULT_CONFIG = {
    "api_key": "",
    "latitude": 45.5152,
    "longitude": -122.6784,
    "location_name": "Portland, Oregon",
    "radius_km": 22.0,
    "heatmap_threshold_km": 40.0,
    "interface_scale": 1.0,
    "temperature_unit": "F",
    "home_mode": "auto",
    "location_filter": "outdoor",
    "alert_threshold": 101,
    "refresh_minutes": 2,
    "confidence_filter": True,
    "favorites": [],
    "alert_states": {},
    "hidden": {},
}


SYNC_THREAD_NAME = "airloom-settings-sync"


class SettingsDurabilityError(OSError):
    """The new settings replaced the old file, but the rename may not survive a crash.

    ``saved`` is the settings dict, already applied and on disk, whose
    durability could not be confirmed."""

    def __init__(self, message: str, saved: dict):
        super().__init__(message)
        self.saved = saved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
            try:
                value = float(value)
            except OverflowError:
                return
            if math.isfinite(value) and low <= value <= high:
                clean[key] = value

    number("latitude", -90.0, 90.0)
    number("longitude", -180.0, 180.0)
    number("radius_km", 2.0, 100.0)
    number("heatmap_threshold_km", 5.0, 1000.0)
    number("alert_threshold", 1, 500)
    clean["alert_threshold"] = int(clean["alert_threshold"])
    scale = data.get("interface_scale")
    if isinstance(scale, float) and scale in (1.0, 1.25, 1.5):
        clean["interface_scale"] = scale
    if isinstance(data.get("api_key"), str):
        clean["api_key"] = data["api_key"].strip()
    if isinstance(data.get("location_name"), str) and data["location_name"].strip():
        clean["location_name"] = data["location_name"].strip()[:80]
    if data.get("temperature_unit") in ("F", "C"):
        clean["temperature_unit"] = data["temperature_unit"]
    if data.get("home_mode") in ("auto", "fixed"):
        clean["home_mode"] = data["home_mode"]
    if data.get("location_filter") in ("outdoor", "indoor", "both"):
        clean["location_filter"] = data["location_filter"]
    if data.get("refresh_minutes") in (2, 5, 10, 30) and not isinstance(data.get("refresh_minutes"), bool):
        clean["refresh_minutes"] = int(data["refresh_minutes"])
    if isinstance(data.get("confidence_filter"), bool):
        clean["confidence_filter"] = data["confidence_filter"]
    favorites = data.get("favorites")
    if isinstance(favorites, list):
        valid_favorites = set()
        for item in favorites:
            normalized = sensor_id(item)
            if normalized is not None:
                valid_favorites.add(normalized)
        clean["favorites"] = sorted(valid_favorites)
    if isinstance(data.get("alert_states"), dict):
        clean["alert_states"] = {str(key): bool(value) for key, value in data["alert_states"].items()}
    hidden = data.get("hidden")
    if isinstance(hidden, dict):
        for key, value in hidden.items():
            normalized = sensor_id(key) if isinstance(key, str) else None
            if normalized is not None and isinstance(value, str):
                clean["hidden"][str(normalized)] = value.strip()[:80]
    return clean


class Store:
    """Airloom's settings, persisted as private JSON.

    Threading contract. Mutators (``save``, ``apply``, ``toggle_favorite``,
    ``hide``, ``unhide``, ``unhide_all``) run on the caller's thread, which in
    Airloom is the GTK main loop, and do only the blocking work 0.13 did there:
    write a private exclusive temporary file, fsync it, and rename it over the
    settings file. A failure before the rename raises OSError and leaves the
    previous settings in place; once a mutator returns, the new settings are
    applied and on disk.

    A rename survives a crash only once its directory, and the entries of any
    directories this process created, are fsynced. That work runs on a
    Store-owned worker thread (``SYNC_THREAD_NAME``), started when a rename
    needs it and exiting once idle. Each pass starts after the renames it
    covers, so one pass may confirm several saves. A directory whose sync
    fails stays unconfirmed and is retried by the pass after the next save.

    When a pass leaves anything unconfirmed, ``on_durability_error`` is
    called on the worker thread with a SettingsDurabilityError whose
    ``saved`` is the newest saved settings dict; a GTK caller must hand it
    to the main loop itself. Without a callback, one warning line goes to
    stderr. An exception from the callback is logged to stderr.

    ``flush`` waits for the worker; it is for tests and shutdown only.
    """

    def __init__(self, path: Path | None = None, *, on_durability_error=None):
        self.path = path or _default_config_dir() / "config.json"
        self.on_durability_error = on_durability_error
        self._save_lock = threading.RLock()
        # Guards everything below, shared with the sync worker.
        self._sync_condition = threading.Condition()
        # Directories whose entries are not yet confirmed durable, in sync
        # order; until they are, no save is reported durable.
        self._unconfirmed_directories: list[Path] = []
        # The newest saved settings that the next pass must confirm.
        self._unsynced_settings: dict | None = None
        self._sync_worker: threading.Thread | None = None
        self._exited_workers: list[threading.Thread] = []
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
        with self._save_lock:
            self.apply(deepcopy(self.data))

    def apply(self, candidate: dict) -> dict:
        clean = _sanitize(candidate)
        with self._save_lock:
            self._write(clean)
            self.data = clean
        return clean

    def flush(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for pending directory syncs. True
        only if every saved setting is confirmed durable."""
        deadline = time.monotonic() + timeout
        with self._sync_condition:
            if not self._sync_condition.wait_for(lambda: self._sync_worker is None, timeout):
                return False
            exited, self._exited_workers = self._exited_workers, []
            confirmed = not self._unconfirmed_directories
        for worker in exited:
            worker.join(max(0.0, deadline - time.monotonic()))
        return confirmed

    def _write(self, data: dict) -> None:
        created = []
        directory = self.path.parent
        while not directory.exists():
            created.insert(0, directory)
            directory = directory.parent
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            # Each new directory is only reachable after a crash once the
            # entry in its parent is synced, even if this write then fails.
            self._mark_unconfirmed([d.parent for d in created])
        # mkstemp uses O_EXCL and mode 0600, so preexisting files and links
        # cannot redirect or expose the API key while it is being written.
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        open_descriptor: int | None = descriptor
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            open_descriptor = None
            with handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if open_descriptor is not None:
                os.close(open_descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self._schedule_sync(deepcopy(data))

    def _mark_unconfirmed(self, directories: list[Path]) -> None:
        with self._sync_condition:
            for directory in directories:
                if directory not in self._unconfirmed_directories:
                    self._unconfirmed_directories.append(directory)

    def _schedule_sync(self, saved: dict) -> None:
        """Queue a sync of the renamed file's directory; called after the rename."""
        self._mark_unconfirmed([self.path.parent])
        with self._sync_condition:
            self._unsynced_settings = saved
            if self._sync_worker is None:
                self._sync_worker = threading.Thread(
                    target=self._sync_loop, name=SYNC_THREAD_NAME, daemon=True)
                self._sync_worker.start()

    def _sync_loop(self) -> None:
        while True:
            with self._sync_condition:
                saved = self._unsynced_settings
                if saved is None:
                    self._exited_workers = [
                        worker for worker in self._exited_workers if worker.is_alive()
                    ]
                    self._exited_workers.append(threading.current_thread())
                    self._sync_worker = None
                    self._sync_condition.notify_all()
                    return
                self._unsynced_settings = None
                directories = self._unconfirmed_directories
                self._unconfirmed_directories = []
            failed: list[Path] = []
            first_error: OSError | None = None
            for directory in directories:
                try:
                    _fsync_directory(directory)
                except OSError as exc:
                    failed.append(directory)
                    first_error = first_error or exc
            if first_error is None:
                continue
            with self._sync_condition:
                queued = self._unconfirmed_directories
                self._unconfirmed_directories = failed + [d for d in queued if d not in failed]
            error = SettingsDurabilityError(
                "Settings were saved, but the system could not confirm they reached "
                f"the disk: {first_error}",
                saved,
            )
            error.__cause__ = first_error
            self._report_durability_error(error)

    def _report_durability_error(self, error: SettingsDurabilityError) -> None:
        callback = self.on_durability_error
        if callback is None:
            print(f"Airloom: {error}", file=sys.stderr)
            return
        try:
            callback(error)
        except Exception as exc:
            print(f"Airloom: settings durability callback failed: {exc!r}", file=sys.stderr)

    def public_config(self) -> dict:
        api_key = self.data.get("api_key")
        has_key = isinstance(api_key, str) and bool(api_key)
        return {
            "latitude": self.data["latitude"],
            "longitude": self.data["longitude"],
            "location_name": self.data["location_name"],
            "radius_km": self.data["radius_km"],
            "heatmap_threshold_km": self.data["heatmap_threshold_km"],
            "interface_scale": self.data["interface_scale"],
            "temperature_unit": self.data["temperature_unit"],
            "home_mode": self.data["home_mode"],
            "location_filter": self.data["location_filter"],
            "alert_threshold": self.data["alert_threshold"],
            "refresh_minutes": self.data["refresh_minutes"],
            "confidence_filter": self.data["confidence_filter"],
            "has_api_key": has_key,
            "api_key_hint": f"••••{api_key[-4:]}" if has_key and len(api_key) >= 8 else "",
            "hidden": sorted(
                ({"id": int(key), "name": name} for key, name in self.data["hidden"].items()),
                key=lambda item: (item["name"].lower(), item["id"]),
            ),
        }

    def has_custom_location(self) -> bool:
        """True once the stored location differs from the shipped default."""
        return (self.data["latitude"], self.data["longitude"]) != (
            DEFAULT_CONFIG["latitude"],
            DEFAULT_CONFIG["longitude"],
        )

    def toggle_favorite(self, sensor_id: int) -> bool:
        with self._save_lock:
            candidate = deepcopy(self.data)
            favorites = set(candidate.get("favorites", []))
            if sensor_id in favorites:
                favorites.remove(sensor_id)
                enabled = False
            else:
                favorites.add(sensor_id)
                enabled = True
            candidate["favorites"] = sorted(favorites)
            self.apply(candidate)
        return enabled

    def hide(self, sensor_id: int, name: str) -> None:
        with self._save_lock:
            candidate = deepcopy(self.data)
            candidate["hidden"][str(sensor_id)] = str(name).strip()[:80]
            self.apply(candidate)

    def unhide(self, sensor_id: int) -> None:
        with self._save_lock:
            candidate = deepcopy(self.data)
            if candidate["hidden"].pop(str(sensor_id), None) is not None:
                self.apply(candidate)

    def unhide_all(self) -> None:
        with self._save_lock:
            candidate = deepcopy(self.data)
            if candidate["hidden"]:
                candidate["hidden"] = {}
                self.apply(candidate)

    def is_hidden(self, sensor_id: int) -> bool:
        return str(sensor_id) in self.data["hidden"]

    def hidden_ids(self) -> set[int]:
        return {int(key) for key in self.data["hidden"]}
