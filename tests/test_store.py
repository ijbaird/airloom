import errno
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from airloom import store
from airloom.store import Store, _default_config_dir


class StoreThreadsTestCase(unittest.TestCase):
    """Flushes every Store a test creates and checks no sync worker lingers.

    Stores built without an explicit callback collect durability errors
    rather than warning on stderr: a worker can race a test's temporary
    directory removal."""

    def setUp(self):
        created = []
        real_init = Store.__init__

        def init(settings, *args, **kwargs):
            kwargs.setdefault("on_durability_error", lambda error: None)
            real_init(settings, *args, **kwargs)
            created.append(settings)

        patch = mock.patch.object(Store, "__init__", init)
        patch.start()
        self.addCleanup(self._join_workers, created)
        self.addCleanup(patch.stop)

    def _join_workers(self, created):
        for settings in created:
            settings.flush(5)
        lingering = [
            thread for thread in threading.enumerate()
            if thread.name == store.SYNC_THREAD_NAME
        ]
        self.assertEqual(lingering, [])


class StoreTest(StoreThreadsTestCase):
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

    def test_huge_json_integer_falls_back_per_field_on_file_load(self):
        defaults = {
            "latitude": 45.5152,
            "longitude": -122.6784,
            "radius_km": 22.0,
            "heatmap_threshold_km": 40.0,
            "alert_threshold": 101,
        }
        for key, expected in defaults.items():
            with self.subTest(field=key):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps({
                        key: 10**400,
                        "location_name": "Valid setting",
                        "favorites": [42, "123"],
                    }), encoding="utf-8")
                    loaded = Store(path)
                    self.assertEqual(loaded.data[key], expected)
                    self.assertEqual(loaded.data["location_name"], "Valid setting")
                    self.assertEqual(loaded.data["favorites"], [42, 123])

    def test_huge_json_integer_falls_back_per_field_on_apply(self):
        defaults = {
            "latitude": 45.5152,
            "longitude": -122.6784,
            "radius_km": 22.0,
            "heatmap_threshold_km": 40.0,
            "alert_threshold": 101,
        }
        for key, expected in defaults.items():
            with self.subTest(field=key):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    settings = Store(path)
                    candidate = deepcopy(settings.data)
                    candidate.update({
                        key: 10**400,
                        "location_name": "Valid setting",
                        "favorites": [42, "123"],
                    })
                    saved = settings.apply(candidate)
                    self.assertEqual(saved[key], expected)
                    self.assertEqual(saved["location_name"], "Valid setting")
                    self.assertEqual(saved["favorites"], [42, 123])
                    self.assertEqual(Store(path).data, saved)

    def test_short_api_key_is_not_hinted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.data["api_key"] = "abc"
            self.assertEqual(store.public_config()["api_key_hint"], "")
            self.assertTrue(store.public_config()["has_api_key"])

    def test_temp_file_is_private_from_creation(self):
        real_open = os.open
        creations = []

        def spy_open(path, flags, mode=0o777):
            # Read-only directory opens for fsync create nothing; every
            # creating open must be the exclusive, private temp file.
            if flags & os.O_CREAT:
                creations.append((flags & (os.O_CREAT | os.O_EXCL), mode))
            else:
                self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR), 0)
            return real_open(path, flags, mode)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            with mock.patch("airloom.store.os.open", spy_open):
                store.save()
                self.assertTrue(store.flush(5))
            self.assertEqual(creations, [(os.O_CREAT | os.O_EXCL, 0o600)])
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

    def test_hostile_favorites_are_dropped_without_losing_valid_entries(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "²"):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(
                        json.dumps(
                            {
                                "favorites": [42, bad, "123"],
                                "location_name": "Valid setting",
                            }
                        ),
                        encoding="utf-8",
                    )
                    loaded = Store(path)
                    self.assertEqual(loaded.data["favorites"], [42, 123])
                    self.assertEqual(loaded.data["location_name"], "Valid setting")

    def test_permissive_fixed_temp_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            decoy = path.with_suffix(".tmp")
            decoy.write_text("decoy", encoding="utf-8")
            decoy.chmod(0o666)
            settings = Store(path)
            settings.save()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Store(path).data, settings.data)
            self.assertEqual(decoy.read_text(encoding="utf-8"), "decoy")
            self.assertEqual(decoy.stat().st_mode & 0o777, 0o666)

    def test_fixed_temp_symlink_and_its_target_are_not_damaged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            victim = Path(directory) / "victim.json"
            victim.write_text("do not change", encoding="utf-8")
            decoy = path.with_suffix(".tmp")
            decoy.symlink_to(victim)
            settings = Store(path)
            settings.save()
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change")
            self.assertTrue(decoy.is_symlink())
            self.assertFalse(path.is_symlink())
            self.assertEqual(Store(path).data, settings.data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_apply_sanitizes_persists_and_returns_saved_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            settings = Store(path)
            settings.data["api_key"] = "keep-secret"
            settings.save()
            candidate = deepcopy(settings.data)
            candidate.update(
                {
                    "location_name": "  Candidate  ",
                    "favorites": [42, "007", float("nan"), "²"],
                    "interface_scale": 1.25,
                }
            )
            saved = settings.apply(candidate)
            self.assertEqual(saved["location_name"], "Candidate")
            self.assertEqual(saved["favorites"], [7, 42])
            self.assertEqual(saved["interface_scale"], 1.25)
            self.assertEqual(saved["api_key"], "keep-secret")
            self.assertEqual(settings.data, saved)
            self.assertEqual(Store(path).data, saved)
            candidate["favorites"].append(999)
            self.assertEqual(settings.data["favorites"], [7, 42])
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_apply_permission_failure_preserves_memory_disk_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            settings = Store(path)
            settings.data["location_name"] = "Original"
            settings.save()
            original = deepcopy(settings.data)
            original_bytes = path.read_bytes()
            candidate = deepcopy(settings.data)
            candidate.update({"location_name": "Changed", "favorites": [9]})
            with mock.patch("airloom.store.os.replace", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    settings.apply(candidate)
            self.assertEqual(settings.data, original)
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_save_fsync_failure_preserves_disk_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            settings = Store(path)
            settings.save()
            original_bytes = path.read_bytes()
            settings.data["location_name"] = "Unsaved"
            with mock.patch("airloom.store.os.fsync", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    settings.save()
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(settings.data["location_name"], "Unsaved")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_existing_mutators_roll_back_on_permission_failure(self):
        cases = (
            ("favorite", lambda settings: settings.toggle_favorite(3)),
            ("hide", lambda settings: settings.hide(3, "Three")),
            ("unhide", lambda settings: settings.unhide(1)),
            ("unhide_all", lambda settings: settings.unhide_all()),
        )
        for name, operation in cases:
            with self.subTest(operation=name):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    settings = Store(path)
                    settings.data["favorites"] = [1]
                    settings.data["hidden"] = {"1": "One", "2": "Two"}
                    settings.save()
                    original = deepcopy(settings.data)
                    original_bytes = path.read_bytes()
                    with mock.patch(
                        "airloom.store.os.replace", side_effect=PermissionError("denied")
                    ):
                        with self.assertRaises(PermissionError):
                            operation(settings)
                    self.assertEqual(settings.data, original)
                    self.assertEqual(path.read_bytes(), original_bytes)
                    self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_overlapping_applies_are_serialized_without_temp_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            settings = Store(path)
            settings.save()
            first_candidate = deepcopy(settings.data)
            first_candidate.update({"location_name": "first", "interface_scale": 1.25})
            second_candidate = deepcopy(settings.data)
            second_candidate.update({"location_name": "second", "interface_scale": 1.5})
            real_fsync = os.fsync
            first_in_fsync = threading.Event()
            release_first = threading.Event()
            second_done = threading.Event()
            second_ready = threading.Event()
            first_thread = None
            errors = []

            def controlled_fsync(fd):
                if threading.current_thread() is first_thread:
                    first_in_fsync.set()
                    if not release_first.wait(5):
                        raise TimeoutError("timed out waiting to release first save")
                return real_fsync(fd)

            def apply(candidate, done=None, ready=None):
                if ready is not None:
                    ready.set()
                try:
                    settings.apply(candidate)
                except BaseException as error:
                    errors.append(error)
                finally:
                    if done is not None:
                        done.set()

            first_thread = threading.Thread(target=apply, args=(first_candidate,))
            second_thread = threading.Thread(
                target=apply, args=(second_candidate, second_done, second_ready))
            second_started = False
            second_ready_to_apply = False
            with mock.patch("airloom.store.os.fsync", controlled_fsync):
                first_thread.start()
                entered_fsync = first_in_fsync.wait(2)
                second_finished_early = None
                if entered_fsync:
                    second_thread.start()
                    second_started = True
                    second_ready_to_apply = second_ready.wait(2)
                    second_finished_early = second_done.wait(0.1)
                release_first.set()
                first_thread.join(5)
                if second_started:
                    second_thread.join(5)

            self.assertTrue(entered_fsync, "first apply did not reach fsync")
            self.assertTrue(second_ready_to_apply, "second apply was not ready to contend")
            self.assertFalse(
                second_finished_early,
                "second apply completed while the first save was blocked",
            )
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(settings.data["location_name"], "second")
            self.assertEqual(settings.data["interface_scale"], 1.5)
            self.assertEqual(Store(path).data, settings.data)
            self.assertEqual(list(Path(directory).iterdir()), [path])


class DurabilityTest(StoreThreadsTestCase):
    def record(self, fail=None):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            if fail is not None and fail(fd, events):
                raise OSError(errno.EIO, "I/O error")
            events.append(("fsync", os.fstat(fd).st_ino))
            real_fsync(fd)

        def replace(src, dst):
            events.append(("replace",))
            real_replace(src, dst)

        patches = (
            mock.patch("airloom.store.os.fsync", side_effect=fsync),
            mock.patch("airloom.store.os.replace", side_effect=replace),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return events

    def collecting_store(self, path):
        errors = []

        def collect(error):
            errors.append((error, threading.current_thread()))

        return Store(path, on_durability_error=collect), errors

    def test_save_syncs_directory_after_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            settings = Store(path)
            events = self.record()
            settings.save()
            self.assertTrue(settings.flush(5))
            self.assertEqual(
                events,
                [("fsync", path.stat().st_ino), ("replace",), ("fsync", path.parent.stat().st_ino)],
            )

    def test_first_save_syncs_new_directory_entries_after_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "airloom" / "config.json"
            settings = Store(path)
            events = self.record()
            settings.save()
            self.assertTrue(settings.flush(5))

            def ino(directory):
                return directory.stat().st_ino

            self.assertEqual(
                events,
                [
                    ("fsync", path.stat().st_ino),
                    ("replace",),
                    ("fsync", ino(Path(tmp))),
                    ("fsync", ino(Path(tmp) / "new")),
                    ("fsync", ino(Path(tmp) / "new" / "airloom")),
                ],
            )

    def test_directory_fsync_failure_after_replace_keeps_new_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            settings, errors = self.collecting_store(path)
            settings.save()
            self.assertTrue(settings.flush(5))
            self.record(
                fail=lambda fd, events: stat.S_ISDIR(os.fstat(fd).st_mode)
                and ("replace",) in events
            )
            saved = settings.apply({**settings.data, "location_name": "Changed"})
            self.assertFalse(settings.flush(5))
            self.assertEqual(len(errors), 1)
            exc, thread = errors[0]
            self.assertIsInstance(exc, store.SettingsDurabilityError)
            self.assertIsInstance(exc, OSError)
            self.assertIsNot(thread, threading.current_thread())
            self.assertEqual(exc.saved, saved)
            self.assertEqual(exc.saved["location_name"], "Changed")
            self.assertEqual(settings.data, exc.saved)
            self.assertEqual(Store(path).data, exc.saved)
            self.assertEqual(list(Path(tmp).iterdir()), [path])
            self.assertIn("saved", str(exc))

            settings.toggle_favorite(5)
            self.assertFalse(settings.flush(5))
            self.assertEqual(len(errors), 2)
            self.assertIn(5, settings.data["favorites"])
            self.assertEqual(settings.data, errors[1][0].saved)
            self.assertEqual(Store(path).data, errors[1][0].saved)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def record_new_directory_failure(self):
        """Fails every directory fsync until the returned flag is cleared."""
        failing = [True]
        events = self.record(
            fail=lambda fd, events: failing[0] and stat.S_ISDIR(os.fstat(fd).st_mode)
        )
        return events, failing

    def test_new_directory_sync_failure_commits_but_is_not_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "airloom" / "config.json"
            settings, errors = self.collecting_store(path)
            self.record_new_directory_failure()
            saved = settings.apply({**settings.data, "location_name": "Changed"})
            self.assertFalse(settings.flush(5))
            self.assertEqual([error.saved for error, _ in errors], [saved])
            self.assertEqual(saved["location_name"], "Changed")
            self.assertEqual(settings.data, saved)
            self.assertEqual(Store(path).data, saved)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_retry_syncs_unconfirmed_directory_entries_before_durable_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "airloom" / "config.json"
            settings, errors = self.collecting_store(path)
            events, failing = self.record_new_directory_failure()
            settings.apply({**settings.data, "location_name": "Changed"})
            self.assertFalse(settings.flush(5))
            self.assertEqual(len(errors), 1)
            # Nothing is retried, or confirmed, until the next save.
            failing[0] = False
            self.assertFalse(settings.flush(5))
            events.clear()
            settings.save()
            self.assertTrue(settings.flush(5))
            self.assertEqual(len(errors), 1)

            def ino(directory):
                return directory.stat().st_ino

            self.assertEqual(
                events,
                [
                    ("fsync", path.stat().st_ino),
                    ("replace",),
                    ("fsync", ino(Path(tmp))),
                    ("fsync", ino(Path(tmp) / "new")),
                    ("fsync", ino(path.parent)),
                ],
            )
            # Once confirmed, later saves no longer resync the ancestors.
            events.clear()
            settings.save()
            self.assertTrue(settings.flush(5))
            self.assertEqual(
                events,
                [("fsync", path.stat().st_ino), ("replace",), ("fsync", ino(path.parent))],
            )

    def test_retry_with_ancestor_sync_still_failing_is_not_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "airloom" / "config.json"
            settings, errors = self.collecting_store(path)
            unconfirmed = {Path(tmp).stat().st_ino}
            self.record(fail=lambda fd, events: os.fstat(fd).st_ino in unconfirmed)
            settings.save()
            self.assertFalse(settings.flush(5))
            saved = settings.apply({**settings.data, "location_name": "Retried"})
            self.assertFalse(settings.flush(5))
            self.assertEqual(len(errors), 2)
            self.assertEqual(errors[1][0].saved, saved)
            self.assertEqual(Store(path).data["location_name"], "Retried")
            unconfirmed.clear()
            settings.save()
            self.assertTrue(settings.flush(5))
            self.assertEqual(len(errors), 2)

    def test_failed_write_after_creating_directories_still_syncs_them_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "airloom" / "config.json"
            settings, errors = self.collecting_store(path)
            events = self.record()
            with mock.patch("airloom.store.os.replace", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    settings.save()
            self.assertFalse(settings.flush(5))
            events.clear()
            settings.save()
            self.assertTrue(settings.flush(5))
            self.assertEqual(errors, [])
            self.assertEqual(
                [event for event in events if event != ("replace",)][1:],
                [
                    ("fsync", Path(tmp).stat().st_ino),
                    ("fsync", (Path(tmp) / "new").stat().st_ino),
                    ("fsync", path.parent.stat().st_ino),
                ],
            )


class BackgroundDurabilityTest(StoreThreadsTestCase):
    def blocked_directory_sync(self):
        """Blocks every directory fsync until the returned event is set."""
        release = threading.Event()
        entered = threading.Event()
        threads = []
        real_sync = store._fsync_directory

        def blocked(path):
            threads.append(threading.current_thread())
            entered.set()
            if not release.wait(10):
                raise TimeoutError("directory sync was never released")
            real_sync(path)

        patch = mock.patch("airloom.store._fsync_directory", side_effect=blocked)
        patch.start()
        self.addCleanup(patch.stop)
        # Release before the patch stops, so cleanup flushes never hang.
        self.addCleanup(release.set)
        return release, entered, threads

    def test_save_returns_while_directory_sync_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            settings = Store(path)
            release, entered, threads = self.blocked_directory_sync()
            started = time.monotonic()
            saved = settings.apply({**settings.data, "location_name": "Prompt"})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
            self.assertEqual(Store(path).data, saved)
            self.assertTrue(entered.wait(5))
            self.assertFalse(settings.flush(0.05))
            release.set()
            self.assertTrue(settings.flush(5))
            self.assertTrue(threads)
            self.assertNotIn(threading.current_thread(), threads)

    def test_one_sync_after_the_last_rename_covers_queued_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            settings = Store(path)
            events = []
            real_replace = os.replace
            release, entered, _ = self.blocked_directory_sync()
            sync = store._fsync_directory.side_effect

            def replace(src, dst):
                real_replace(src, dst)
                events.append("replace")

            def recorded_sync(directory):
                events.append("sync start")
                sync(directory)
                events.append("sync done")

            store._fsync_directory.side_effect = recorded_sync
            with mock.patch("airloom.store.os.replace", side_effect=replace):
                settings.apply({**settings.data, "location_name": "First"})
                self.assertTrue(entered.wait(5))
                settings.apply({**settings.data, "location_name": "Second"})
                settings.apply({**settings.data, "location_name": "Third"})
                release.set()
                self.assertTrue(settings.flush(5))
            self.assertEqual(
                events,
                ["replace", "sync start", "replace", "replace", "sync done",
                 "sync start", "sync done"],
            )
            self.assertEqual(Store(path).data["location_name"], "Third")

    def test_without_callback_one_warning_line_goes_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            settings = Store(path, on_durability_error=None)
            stderr = io.StringIO()
            with mock.patch("airloom.store._fsync_directory",
                            side_effect=OSError(errno.EIO, "I/O error")), \
                    mock.patch("sys.stderr", stderr):
                settings.save()
                self.assertFalse(settings.flush(5))
            lines = stderr.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("could not confirm", lines[0])
            self.assertIn("I/O error", lines[0])

    def test_callback_exception_is_logged_and_does_not_stop_the_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            received = []

            def callback(error):
                received.append(error.saved["location_name"])
                raise RuntimeError("callback broke")

            settings = Store(path, on_durability_error=callback)
            stderr = io.StringIO()
            with mock.patch("airloom.store._fsync_directory",
                            side_effect=OSError(errno.EIO, "I/O error")), \
                    mock.patch("sys.stderr", stderr):
                settings.apply({**settings.data, "location_name": "One"})
                self.assertFalse(settings.flush(5))
                settings.apply({**settings.data, "location_name": "Two"})
                self.assertFalse(settings.flush(5))
            self.assertEqual(received, ["One", "Two"])
            self.assertEqual(stderr.getvalue().count("callback broke"), 2)


class SensorIdTest(unittest.TestCase):
    def test_accepts_positive_integral_ids_through_javascript_safe_maximum(self):
        maximum = 2**53 - 1
        for value, expected in ((1, 1), (1.0, 1), ("007", 7), ("1e3", 1000), (maximum, maximum)):
            with self.subTest(value=value):
                self.assertEqual(store.sensor_id(value), expected)

    def test_rejects_nonintegral_nonfinite_nonpositive_and_out_of_range_ids(self):
        maximum = 2**53 - 1
        for bad in (
            None,
            True,
            False,
            0,
            -1,
            1.5,
            "1.5",
            float("nan"),
            float("inf"),
            "²",
            maximum + 1,
            str(maximum + 1),
            2**63 - 1,
            "1e999999999",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(store.sensor_id(bad))


class InterfaceScaleTest(unittest.TestCase):
    def test_defaults_to_one_and_appears_in_public_config(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Store(Path(directory) / "config.json")
            self.assertEqual(settings.data["interface_scale"], 1.0)
            self.assertEqual(settings.public_config()["interface_scale"], 1.0)

    def test_accepts_exactly_the_three_supported_scales(self):
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps({"interface_scale": scale}), encoding="utf-8")
                    settings = Store(path)
                    self.assertEqual(settings.data["interface_scale"], scale)
                    self.assertEqual(settings.public_config()["interface_scale"], scale)

    def test_invalid_scales_fall_back_to_one(self):
        for bad in (1, 0.5, 1.2, 2.0, "1.25", True, False, None, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps({"interface_scale": bad}), encoding="utf-8")
                    self.assertEqual(Store(path).data["interface_scale"], 1.0)


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

