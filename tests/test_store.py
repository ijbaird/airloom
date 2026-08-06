import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from airloom.store import Store, _default_config_dir


class StoreTest(unittest.TestCase):
    def test_round_trip_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.data["api_key"] = "secret-1234"
            store.toggle_favorite(42)
            loaded = Store(path)
            self.assertEqual(loaded.data["api_key"], "secret-1234")
            self.assertEqual(loaded.data["favorites"], [42])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(loaded.public_config()["api_key_hint"], "••••1234")
            self.assertNotIn("api_key", loaded.public_config())

    def test_malformed_config_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("not json", encoding="utf-8")
            store = Store(path)
            self.assertEqual(store.data["location_name"], "Portland, Oregon")

    def test_wrong_typed_config_values_fall_back_without_raising(self):
        corrupt = {
            "favorites": 5,
            "latitude": "abc",
            "longitude": None,
            "radius_km": float("inf"),
            "alert_threshold": "high",
            "temperature_unit": "kelvin",
            "location_name": 7,
            "api_key": 12345678,
            "alert_states": "wrong",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(corrupt), encoding="utf-8")
            store = Store(path)
            self.assertEqual(store.data["favorites"], [])
            self.assertEqual(store.data["latitude"], 45.5152)
            self.assertEqual(store.data["longitude"], -122.6784)
            self.assertEqual(store.data["radius_km"], 22.0)
            self.assertEqual(store.data["alert_threshold"], 101)
            self.assertEqual(store.data["temperature_unit"], "F")
            self.assertEqual(store.data["location_name"], "Portland, Oregon")
            self.assertEqual(store.data["alert_states"], {})
            public = store.public_config()
            self.assertFalse(public["has_api_key"])
            self.assertEqual(public["api_key_hint"], "")

    def test_string_and_mixed_favorites_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"favorites": ["123", 7, "abc", True, 7.0]}), encoding="utf-8")
            store = Store(path)
            self.assertEqual(store.data["favorites"], [7, 123])

    def test_short_api_key_is_not_hinted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.data["api_key"] = "abc"
            self.assertEqual(store.public_config()["api_key_hint"], "")
            self.assertTrue(store.public_config()["has_api_key"])

    def test_temp_file_is_private_from_creation(self):
        real_open = os.open
        seen_modes = []

        def spy_open(path, flags, mode=0o777):
            seen_modes.append(mode)
            return real_open(path, flags, mode)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            with mock.patch("airloom.store.os.open", spy_open):
                store.save()
            self.assertEqual(seen_modes, [0o600])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_empty_or_relative_xdg_config_home_is_ignored(self):
        for value in ("", "relative/path"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": value}):
                    self.assertTrue(_default_config_dir().is_absolute())

    def test_absolute_xdg_config_home_is_used(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg-test"}):
            self.assertEqual(_default_config_dir(), Path("/tmp/xdg-test/airloom"))

    def test_home_mode_defaults_to_auto_and_survives_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["home_mode"], "auto")
            self.assertEqual(store.public_config()["home_mode"], "auto")
            store.data["home_mode"] = "fixed"
            store.save()
            self.assertEqual(Store(path).data["home_mode"], "fixed")

    def test_invalid_home_mode_falls_back_to_auto(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"home_mode": "sometimes"}), encoding="utf-8")
            self.assertEqual(Store(path).data["home_mode"], "auto")


if __name__ == "__main__":
    unittest.main()

