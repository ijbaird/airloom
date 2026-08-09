import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from airloom import store
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

    def test_confidence_filter_defaults_on_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertTrue(store.data["confidence_filter"])
            self.assertTrue(store.public_config()["confidence_filter"])
            store.data["confidence_filter"] = False
            store.save()
            loaded = Store(path)
            self.assertFalse(loaded.data["confidence_filter"])
            self.assertFalse(loaded.public_config()["confidence_filter"])

    def test_confidence_filter_rejects_non_bool(self):
        for bad in ("yes", 1, 0, None, [], {}):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps({"confidence_filter": bad}), encoding="utf-8")
                    self.assertTrue(Store(path).data["confidence_filter"])

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

    def test_has_custom_location_flips_once_coordinates_change(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            self.assertFalse(store.has_custom_location())
            store.data.update({"latitude": 39.1677, "longitude": -120.1452})
            self.assertTrue(store.has_custom_location())

    def test_invalid_home_mode_falls_back_to_auto(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"home_mode": "sometimes"}), encoding="utf-8")
            self.assertEqual(Store(path).data["home_mode"], "auto")

    def test_location_filter_defaults_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["location_filter"], "outdoor")
            self.assertEqual(store.public_config()["location_filter"], "outdoor")
            store.data["location_filter"] = "both"
            store.save()
            self.assertEqual(Store(path).data["location_filter"], "both")

    def test_invalid_location_filter_falls_back_to_outdoor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"location_filter": "underwater"}), encoding="utf-8")
            self.assertEqual(Store(path).data["location_filter"], "outdoor")

    def test_heatmap_threshold_defaults_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["heatmap_threshold_km"], 40.0)
            self.assertEqual(store.public_config()["heatmap_threshold_km"], 40.0)
            store.data["heatmap_threshold_km"] = 120.0
            store.save()
            self.assertEqual(Store(path).data["heatmap_threshold_km"], 120.0)

    def test_heatmap_threshold_invalid_values_fall_back(self):
        for bad in ("wide", 4, 1001, float("inf"), True):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps({"heatmap_threshold_km": bad}), encoding="utf-8")
                    self.assertEqual(Store(path).data["heatmap_threshold_km"], 40.0)

    def test_hidden_defaults_empty_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["hidden"], {})
            self.assertEqual(store.public_config()["hidden"], [])
            store.hide(4242, "Backyard PurpleAir")
            self.assertTrue(store.is_hidden(4242))
            self.assertEqual(store.hidden_ids(), {4242})
            loaded = Store(path)
            self.assertEqual(loaded.data["hidden"], {"4242": "Backyard PurpleAir"})
            self.assertEqual(
                loaded.public_config()["hidden"],
                [{"id": 4242, "name": "Backyard PurpleAir"}],
            )

    def test_unhide_and_unhide_all(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.hide(1, "One")
            store.hide(2, "Two")
            store.unhide(1)
            self.assertFalse(store.is_hidden(1))
            self.assertEqual(Store(path).hidden_ids(), {2})
            store.unhide(999)  # no-op on unknown id, must not raise
            store.unhide_all()
            self.assertEqual(store.hidden_ids(), set())
            self.assertEqual(Store(path).data["hidden"], {})

    def test_hidden_sanitize_drops_garbage_and_truncates(self):
        corrupt = {
            "hidden": {
                "123": "Valid",
                "007": "Padded key",
                "abc": "bad key",
                "9": 42,
                "10": "x" * 200,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(corrupt), encoding="utf-8")
            store = Store(path)
            self.assertEqual(
                store.data["hidden"],
                {"123": "Valid", "7": "Padded key", "10": "x" * 80},
            )

    def test_hidden_unicode_digit_keys_are_dropped_not_crashing(self):
        # "²".isdigit() is True but int("²") raises; a hand-edited config
        # must never make _sanitize (and thus app startup) crash.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"hidden": {"²": "x", "5": "ok"}}), encoding="utf-8")
            self.assertEqual(Store(path).data["hidden"], {"5": "ok"})

    def test_hidden_wrong_type_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"hidden": ["12"]}), encoding="utf-8")
            self.assertEqual(Store(path).data["hidden"], {})

    def test_hidden_names_sort_case_insensitively_in_public_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.hide(3, "zebra")
            store.hide(1, "Alpha")
            store.hide(2, "beta")
            self.assertEqual(
                [item["name"] for item in store.public_config()["hidden"]],
                ["Alpha", "beta", "zebra"],
            )

    def test_hide_keeps_favorite_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.toggle_favorite(77)
            store.hide(77, "Starred and hidden")
            self.assertEqual(store.data["favorites"], [77])
            store.unhide(77)
            self.assertEqual(Store(path).data["favorites"], [77])

    def test_hide_truncates_and_strips_name(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.hide(5, "  padded  ")
            self.assertEqual(store.data["hidden"]["5"], "padded")
            store.hide(6, "y" * 200)
            self.assertEqual(store.data["hidden"]["6"], "y" * 80)


class RefreshMinutesTest(unittest.TestCase):
    def test_defaults_to_two(self):
        self.assertEqual(store._sanitize({})["refresh_minutes"], 2)

    def test_accepts_allowed_values(self):
        for minutes in (2, 5, 10, 30):
            self.assertEqual(store._sanitize({"refresh_minutes": minutes})["refresh_minutes"], minutes)

    def test_rejects_everything_else(self):
        for bad in (7, 0, -5, 2.5, "10", True, None, [10]):
            self.assertEqual(store._sanitize({"refresh_minutes": bad})["refresh_minutes"], 2)

    def test_appears_in_public_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = store.Store(Path(tmp) / "config.json")
            self.assertEqual(s.public_config()["refresh_minutes"], 2)


if __name__ == "__main__":
    unittest.main()

