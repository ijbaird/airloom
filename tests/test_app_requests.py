from __future__ import annotations

import errno
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from airloom import app as app_module
except (ImportError, ValueError):
    # Headless hosts without PyGObject: the adapter tests never create
    # widgets, so a shared minimal namespace stands in for gi.
    try:
        from tests import gi_stub
    except ImportError:
        import gi_stub
    gi_stub.install()
    from airloom import app as app_module

from airloom import geocode as geocode_module
from airloom import store as store_module
from airloom.app import AirloomApplication
from airloom.cache import CacheError, SensorCache
from airloom.controller import ApplicationController
from airloom.geocode import Place
from airloom.models import Sensor
from airloom.purpleair import FetchResult, PurpleAirError
from airloom.requests import RequestRegistry
from airloom.store import SYNC_THREAD_NAME, SettingsDurabilityError, Store


CONFIG = {
    "api_key": "test-api-key",
    "latitude": 0.0,
    "longitude": 0.0,
    "location_name": "Original home",
    "home_mode": "auto",
    "radius_km": 55.5,
    "heatmap_threshold_km": 50.0,
    "refresh_minutes": 2,
    "alert_threshold": 101,
    "temperature_unit": "F",
    "confidence_filter": True,
    "location_filter": "outdoor",
    "favorites": [],
    "hidden": {},
    "alert_states": {},
}


def make_sensor(sensor_id=1, *, last_seen=100, trend=None):
    return Sensor(
        sensor_id=sensor_id,
        name=f"Sensor {sensor_id}",
        latitude=1.0,
        longitude=2.0,
        aqi=50,
        pm25=10.0,
        confidence=100,
        last_seen=last_seen,
        trend=[] if trend is None else trend,
    )


class FakeStore:
    def __init__(self, **updates):
        self.data = {**CONFIG, **updates}
        self.saves = 0
        self.flushes = []
        self.flush_result = True

    def flush(self, timeout):
        self.flushes.append(timeout)
        return self.flush_result

    def apply(self, candidate):
        self.saves += 1
        self.data = deepcopy(candidate)
        return self.data

    def save(self):
        self.apply(deepcopy(self.data))

    def public_config(self):
        return {
            "home_mode": self.data["home_mode"],
            "location_name": self.data["location_name"],
        }

    def has_custom_location(self):
        return False


class FakeCache:
    def __init__(self):
        self.stored = []
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def get_trend(self, _sensor_id, _ttl):
        return None

    def store_trend(self, sensor_id, trend):
        self.stored.append((sensor_id, trend))


class ScriptMessage:
    def __init__(self, message):
        self.message = message

    def to_json(self, _options=0):
        return json.dumps(self.message)


class FakeWindow:
    def __init__(self, active=True):
        self.active = active
        self.disconnected = []

    def is_active(self):
        return self.active

    def disconnect(self, handler_id):
        self.disconnected.append(handler_id)


class FakeLocator:
    def __init__(self):
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1


class QueuedThread:
    def __init__(self, *, target, args=(), kwargs=None, **_options):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.started = False

    def start(self):
        self.started = True

    def run(self):
        self.target(*self.args, **self.kwargs)


class ThreadQueue:
    def __init__(self):
        self.threads = []

    def factory(self, **kwargs):
        thread = QueuedThread(**kwargs)
        self.threads.append(thread)
        return thread


class IdleQueue:
    def __init__(self):
        self.callbacks = []
        self.threads = []

    def add(self, callback, *args):
        self.callbacks.append((callback, args))
        self.threads.append(threading.current_thread())
        return len(self.callbacks)

    def run(self, index):
        callback, args = self.callbacks[index]
        return callback(*args)


class RealStoreMixin:
    """Real Stores drain their settings-sync worker before their temporary
    directory is removed, so no worker outlives a test or syncs a deleted
    directory."""

    @contextmanager
    def settings_directory(self):
        self._real_stores = []
        with tempfile.TemporaryDirectory() as directory:
            try:
                yield directory
            finally:
                for store in self._real_stores:
                    store.flush(5)

    def real_store(self, path):
        store = Store(path)
        self._real_stores.append(store)
        return store


class ApplicationRequestTestCase(RealStoreMixin, unittest.TestCase):
    def make_app(self, *, sensors=(), home_mode="auto"):
        app = AirloomApplication.__new__(AirloomApplication)
        app.store = FakeStore(home_mode=home_mode)
        app.cache = FakeCache()
        app.requests = RequestRegistry()
        app.controller = app._new_controller()
        app._worker_lock = threading.Lock()
        app._active_workers = 0
        app._cache_closed = False
        app._active_kinds = {}
        app._coalesced_jobs = {}
        app._coalesced_running = set()
        app._debug_port = None
        app._auto_refresh_id = None
        app.refreshing = False
        app.pending_fetch = None
        app.sensors = list(sensors)
        app.selected_id = None
        app._trend_inflight = {}
        app._home_name_generation = 0
        app._view_name_generation = 0
        app.current_view = None
        app.window = FakeWindow()
        app.title = None
        app.locator = None
        app._locator_focus_handler = None
        app._locator_focus_fallback = None
        app._awaiting_shell_ready = False
        app.sent = []
        app.sensor_state_sends = 0
        app.refreshed_homes = []
        app._send = lambda event, payload: app.sent.append((event, payload))
        app._send_sensor_state = lambda *_args, **_kwargs: setattr(
            app, "sensor_state_sends", app.sensor_state_sends + 1
        )
        app._refresh_home = lambda *args, **_kwargs: app.refreshed_homes.append(args)
        app._arm_auto_refresh = lambda: None
        app.refresh = lambda *args, **_kwargs: None
        return app

    def make_real_store_app(self, directory, *, sensors=()):
        app = self.make_app(sensors=sensors)
        # Wired as in AirloomApplication.__init__.
        app.store = self.real_store(Path(directory) / "config.json")
        app.store.on_durability_error = app._on_settings_durability_error
        app.store.save()
        self.assertTrue(app.store.flush(5))
        return app

    def trend_events(self, app):
        return [payload for event, payload in app.sent if event == "trend"]

    def place_events(self, app):
        return [payload for event, payload in app.sent if event == "places"]


class TrendRequestTest(ApplicationRequestTestCase):
    def test_duplicate_selection_reuses_inflight_request_and_loads_once(self):
        sensor = make_sensor(last_seen=1234)
        app = self.make_app(sensors=[sensor])
        threads = ThreadQueue()
        idles = IdleQueue()
        trend = [{"label": "Now", "aqi": 50}]
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(app_module, "trend_from_values", return_value=trend),
        ):
            app._ensure_trend(1)
            app._ensure_trend(1)

            self.assertEqual(len(threads.threads), 1)
            self.assertEqual(client.fetch_rows.call_count, 0)
            loading = self.trend_events(app)
            self.assertEqual(len(loading), 1)
            self.assertEqual(loading[0]["sensor_id"], 1)
            self.assertEqual(loading[0]["status"], "loading")
            self.assertEqual(loading[0]["trend"], [])
            self.assertEqual(loading[0]["reading_last_seen"], 1234)

            threads.threads[0].run()
            idles.run(0)

        loaded = self.trend_events(app)[-1]
        self.assertEqual(loaded["status"], "loaded")
        self.assertEqual(loaded["trend"], trend)
        self.assertEqual(loaded["request_id"], loading[0]["request_id"])
        self.assertEqual(loaded["reading_last_seen"], 1234)
        self.assertEqual(sensor.trend, trend)
        self.assertEqual(client.fetch_rows.call_count, 1)

    def test_key_change_revokes_queued_trend_before_paid_io(self):
        for case, update in (("clear", {"clear_api_key": True}), ("replace", {"api_key": "replacement-key"})):
            with self.subTest(case=case):
                app = self.make_app(sensors=[make_sensor(last_seen=1234)])
                threads, idles = ThreadQueue(), IdleQueue()
                client_type = mock.Mock()
                with (mock.patch.object(app_module.threading, "Thread", new=threads.factory),
                      mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
                      mock.patch.object(app_module, "PurpleAirClient", client_type)):
                    app._ensure_trend(1)
                    app._save_settings({**TransactionalActionTest.settings_message(), **update})
                    threads.threads[0].run()
                    for index in range(len(idles.callbacks)):
                        idles.run(index)
                client_type.assert_not_called()
                self.assertNotIn("loaded", [p["status"] for p in self.trend_events(app)])
                self.assertEqual(app._trend_inflight, {})

    def run_trend_with_key_change(self, update, *, during_check=False, during_client=False):
        """Run one trend worker, committing ``update`` at the chosen interleaving.

        Returns the PurpleAirClient mock and the app. ``during_check`` commits
        right after the worker's authority check returns (the old check/capture
        gap); a commit that must wait for the atomic check runs on a real thread
        that is joined before completion. ``during_client`` commits inside the
        client constructor, after authority and key capture.
        """
        app = self.make_app(sensors=[make_sensor(last_seen=1234)])
        threads, idles = ThreadQueue(), IdleQueue()
        real_thread = threading.Thread
        settings = {**TransactionalActionTest.settings_message(), **update}
        commit = lambda: app.controller.save_settings(settings)  # noqa: E731
        waiting = []
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)

        def construct(api_key):
            if during_client:
                commit()
            return client

        client_type = mock.Mock(side_effect=construct)
        check = app.controller.trend_wanted

        def check_then_commit(plan):
            wanted = check(plan)
            lock = getattr(app.controller, "_credential_lock", None)
            if lock is not None and lock.locked():
                waiting.append(real_thread(target=commit))
                waiting[-1].start()
            else:
                commit()
            return wanted

        with (mock.patch.object(app_module.threading, "Thread", new=threads.factory),
              mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
              mock.patch.object(app_module, "PurpleAirClient", client_type),
              mock.patch.object(app_module, "trend_from_values", return_value=[{"label": "Now", "aqi": 9}])):
            if during_check:
                app.controller.trend_wanted = check_then_commit
            app._ensure_trend(1)
            threads.threads[0].run()
            for thread in waiting:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            for index in range(len(idles.callbacks)):
                idles.run(index)
        return client_type, app

    def test_key_change_after_check_never_spends_a_revoked_or_replacement_key(self):
        for case, update in (("clear", {"clear_api_key": True}), ("replace", {"api_key": "replacement-key"})):
            with self.subTest(case=case):
                client_type, app = self.run_trend_with_key_change(update, during_check=True)
                # The change is serialized after the atomic authorization, so
                # only the key that authorized the plan may be spent ...
                client_type.assert_called_once_with("test-api-key")
                # ... and the revoked completion is discarded.
                self.assertNotIn("loaded", [p["status"] for p in self.trend_events(app)])
                self.assertEqual(app._trend_inflight, {})
                self.assertEqual(app.controller.sensors[0].trend, [])
                self.assertEqual(app.cache.stored, [])

    def test_key_change_after_authorization_discards_inflight_completion(self):
        for case, update in (("clear", {"clear_api_key": True}), ("replace", {"api_key": "replacement-key"})):
            with self.subTest(case=case):
                client_type, app = self.run_trend_with_key_change(update, during_client=True)
                client_type.assert_called_once_with("test-api-key")
                self.assertNotIn("loaded", [p["status"] for p in self.trend_events(app)])
                self.assertEqual(app._trend_inflight, {})
                self.assertEqual(app.controller.sensors[0].trend, [])
                self.assertEqual(app.cache.stored, [])

    def test_trend_error_is_terminal_and_allows_manual_retry(self):
        app = self.make_app(sensors=[make_sensor(last_seen=1234)])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.side_effect = PurpleAirError("offline")

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
        ):
            app._ensure_trend(1)
            threads.threads[0].run()
            idles.run(0)
            request_id = self.trend_events(app)[0]["request_id"]

            self.assertEqual(
                self.trend_events(app)[-1],
                {
                    "sensor_id": 1,
                    "request_id": request_id,
                    "status": "error",
                    "trend": [],
                    "reading_last_seen": 1234,
                },
            )

            app._ensure_trend(1)

        self.assertEqual(len(threads.threads), 2)
        self.assertNotEqual(self.trend_events(app)[-1]["request_id"], request_id)
        self.assertEqual(self.trend_events(app)[-1]["status"], "loading")

    def test_empty_trend_is_unavailable_not_perpetually_loading(self):
        app = self.make_app(sensors=[make_sensor(last_seen=1234)])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
        ):
            app._ensure_trend(1)
            request_id = self.trend_events(app)[0]["request_id"]
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(
            self.trend_events(app)[-1],
            {
                "sensor_id": 1,
                "request_id": request_id,
                "status": "unavailable",
                "trend": [],
                "reading_last_seen": 1234,
            },
        )

    def test_trend_cache_read_failure_still_emits_loaded(self):
        sensor = make_sensor(last_seen=1234)
        app = self.make_app(sensors=[sensor])
        app.cache.get_trend = mock.Mock(side_effect=CacheError("cache read failed"))
        threads = ThreadQueue()
        idles = IdleQueue()
        trend = [{"label": "Now", "aqi": 50}]
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(app_module, "trend_from_values", return_value=trend),
        ):
            try:
                app._ensure_trend(1)
            except Exception as exc:
                self.fail(f"trend cache read escaped: {exc}")
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(self.trend_events(app)[-1]["status"], "loaded")
        self.assertEqual(self.trend_events(app)[-1]["trend"], trend)
        self.assertEqual(sensor.trend, trend)

    def test_trend_cache_write_failure_reports_degradation_and_emits_loaded(self):
        sensor = make_sensor(last_seen=1234)
        app = self.make_app(sensors=[sensor])
        app.cache.store_trend = mock.Mock(side_effect=CacheError("cache write failed"))
        threads = ThreadQueue()
        idles = IdleQueue()
        trend = [{"label": "Now", "aqi": 50}]
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(app_module, "trend_from_values", return_value=trend),
        ):
            app._ensure_trend(1)
            threads.threads[0].run()
            try:
                idles.run(0)
                threads.threads[1].run()
                idles.run(1)
            except Exception as exc:
                self.fail(f"trend cache write escaped: {exc}")

        self.assertEqual(self.trend_events(app)[-1]["status"], "loaded")
        self.assertEqual(self.trend_events(app)[-1]["trend"], trend)
        self.assertEqual(sensor.trend, trend)
        errors = [
            payload["message"] for event, payload in app.sent if event == "error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("cache", errors[0].lower())
        self.assertIn("cache write failed", errors[0])

    def test_missing_sensor_emits_idle_state(self):
        app = self.make_app()

        app._ensure_trend(99)

        self.assertEqual(len(self.trend_events(app)), 1)
        self.assertEqual(self.trend_events(app)[0]["sensor_id"], 99)
        self.assertEqual(self.trend_events(app)[0]["status"], "idle")
        self.assertEqual(self.trend_events(app)[0]["trend"], [])
        self.assertIsNone(self.trend_events(app)[0]["reading_last_seen"])
        self.assertIn("request_id", self.trend_events(app)[0])

    def test_old_reading_and_request_completions_cannot_publish(self):
        sensor = make_sensor(last_seen=100)
        app = self.make_app(sensors=[sensor])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)
        old_trend = [{"label": "Now", "aqi": 10}]
        new_trend = [{"label": "Now", "aqi": 80}]

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(
                app_module, "trend_from_values", side_effect=[new_trend, old_trend]
            ),
        ):
            app._ensure_trend(1)
            sensor.last_seen = 200
            app._ensure_trend(1)
            self.assertEqual(len(threads.threads), 2)

            threads.threads[1].run()
            threads.threads[0].run()
            idles.run(0)
            idles.run(1)

        loaded = [
            payload for payload in self.trend_events(app)
            if payload["status"] == "loaded"
        ]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["trend"], new_trend)
        self.assertEqual(loaded[0]["reading_last_seen"], 200)
        self.assertEqual(sensor.trend, new_trend)

    def test_queued_trend_rechecks_token_and_reading_before_paid_fetch(self):
        for authority in ("token", "reading"):
            with self.subTest(authority=authority):
                sensor = make_sensor(last_seen=100)
                app = self.make_app(sensors=[sensor])
                threads = ThreadQueue()
                idles = IdleQueue()
                client = mock.Mock()
                client.fetch_rows.return_value = FetchResult([], 1)

                with (
                    mock.patch.object(app_module.threading, "Thread", new=threads.factory),
                    mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
                    mock.patch.object(app_module, "PurpleAirClient", return_value=client),
                ):
                    app._ensure_trend(1)
                    if authority == "token":
                        app.requests.invalidate("trend:1")
                    else:
                        sensor.last_seen = 200
                    threads.threads[0].run()

                    client.fetch_rows.assert_not_called()
                    self.assertEqual(len(idles.callbacks), 1)
                    idles.run(0)

                self.assertNotIn(1, app._trend_inflight)

    def test_unpublishable_completion_is_not_reused_after_reselection(self):
        sensor = make_sensor(last_seen=100)
        app = self.make_app(sensors=[sensor])
        threads = ThreadQueue()
        idles = IdleQueue()
        client = mock.Mock()
        client.fetch_rows.return_value = FetchResult([{"sensor_index": 1}], 1)

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "PurpleAirClient", return_value=client),
            mock.patch.object(
                app_module,
                "trend_from_values",
                return_value=[{"label": "Now", "aqi": 50}],
            ),
        ):
            app._ensure_trend(1)
            app.sensors = []
            threads.threads[0].run()
            idles.run(0)

            app.sensors = [sensor]
            app._ensure_trend(1)

        self.assertEqual(len(threads.threads), 2)
        self.assertEqual(self.trend_events(app)[-1]["status"], "loading")
        self.assertNotEqual(
            self.trend_events(app)[-1]["request_id"],
            self.trend_events(app)[0]["request_id"],
        )

    def test_trend_admission_is_bounded_and_refusal_is_terminal(self):
        app = self.make_app(sensors=[make_sensor(i, last_seen=1000 + i) for i in range(1, 6)])
        threads = ThreadQueue()
        client_type = mock.Mock()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module, "PurpleAirClient", client_type),
        ):
            for sensor_id in range(1, 6):
                app._ensure_trend(sensor_id)

        self.assertEqual(len(threads.threads), app_module.TREND_WORKER_LIMIT)
        refused = [p for p in self.trend_events(app) if p["sensor_id"] == 5]
        # A refused admission is terminal, so the panel offers Retry.
        self.assertEqual(refused[-1]["status"], "error")
        self.assertNotIn(5, app._trend_inflight)
        client_type.assert_not_called()


MAIN_LOOP_BLOCK_BOUND = 0.25  # seconds; the production busy timeout is 5.0


class MainLoopCacheTest(ApplicationRequestTestCase):
    def test_cache_lock_never_blocks_the_main_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.db"
            cache = SensorCache(path)  # production busy_timeout=5.0
            self.addCleanup(cache.close)
            cache.store_trend(1, [{"label": "Now", "aqi": 42}])
            reader = self.make_app(
                sensors=[make_sensor(1, last_seen=1234), make_sensor(2, last_seen=99)]
            )
            startup = self.make_app()
            reader.cache = startup.cache = cache
            idles = IdleQueue()
            blocker = sqlite3.connect(path)
            self.addCleanup(blocker.close)
            blocker.execute("BEGIN EXCLUSIVE")
            client_type = mock.Mock()
            with (
                mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
                mock.patch.object(app_module, "PurpleAirClient", client_type),
            ):
                plan = reader.controller.begin_trend(2)
                reader.controller.track_trend(plan)
                for label, call in (
                    ("trend read", lambda: reader._ensure_trend(1)),
                    (
                        "trend write",
                        lambda: reader._finish_trend(
                            2, 99, plan.token, [{"label": "Now", "aqi": 7}], False
                        ),
                    ),
                    ("startup paint", startup._paint_cached_home),
                ):
                    started = time.monotonic()
                    call()
                    self.assertLess(
                        time.monotonic() - started, MAIN_LOOP_BLOCK_BOUND, label
                    )
                blocker.rollback()
                deadline = time.monotonic() + 10
                while (
                    reader._active_workers or startup._active_workers
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                for index in range(len(idles.callbacks)):
                    idles.run(index)
            self.assertEqual(reader._active_workers + startup._active_workers, 0)
            client_type.assert_not_called()
            loaded = [p for p in self.trend_events(reader) if p["sensor_id"] == 1]
            self.assertEqual(loaded[-1]["status"], "loaded")
            self.assertEqual(loaded[-1]["trend"], [{"label": "Now", "aqi": 42}])
            self.assertEqual(cache.get_trend(2, 600), [{"label": "Now", "aqi": 7}])


class PlaceSearchRequestTest(ApplicationRequestTestCase):
    def test_place_results_echo_exact_request_identity_context_and_query(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()
        raw_query = "  Paris  "

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(
                app_module,
                "place_search",
                return_value=[Place("Paris", 48.8566, 2.3522)],
            ) as search,
        ):
            app._on_place_search(
                {"request_id": "main-17", "context": "main", "query": raw_query}
            )
            threads.threads[0].run()
            idles.run(0)

        search.assert_called_once_with("Paris")
        self.assertEqual(
            self.place_events(app),
            [
                {
                    "request_id": "main-17",
                    "context": "main",
                    "query": raw_query,
                    "results": [
                        {"name": "Paris", "latitude": 48.8566, "longitude": 2.3522}
                    ],
                }
            ],
        )

    def test_obsolete_queued_search_is_dropped_before_network_io(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "place_search") as search,
        ):
            app._on_place_search(
                {"request_id": 1, "context": "main", "query": "old"}
            )
            app._on_place_search(
                {"request_id": 2, "context": "main", "query": "new"}
            )
            threads.threads[0].run()

        search.assert_not_called()
        self.assertEqual(idles.callbacks, [])
        self.assertEqual(self.place_events(app), [])

    def test_cancel_place_search_invalidates_queued_home_work_before_admission(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "place_search") as search,
        ):
            app._on_place_search(
                {"request_id": "home-old", "context": "home", "query": "old"}
            )
            app._on_script_message(
                None,
                ScriptMessage(
                    {"action": "cancel-place-search", "context": "home"}
                ),
            )
            threads.threads[0].run()

        search.assert_not_called()
        self.assertEqual(idles.callbacks, [])
        self.assertEqual(self.place_events(app), [])

    def test_superseded_search_waiting_in_geocoder_queue_never_reaches_network(self):
        app = self.make_app()
        idles = IdleQueue()
        client = geocode_module.Geocoder(clock=lambda: 100.0, sleep=lambda _seconds: None)
        base_url = geocode_module._base_url()
        old_waiting = threading.Event()
        queue_lock = threading.Lock()
        queue_lock.acquire()

        class SignallingLock:
            def __enter__(self):
                old_waiting.set()
                return queue_lock.__enter__()

            def __exit__(self, *args):
                return queue_lock.__exit__(*args)

        client._search_lock = SignallingLock()
        real_thread = threading.Thread
        threads = []

        def recording_thread(**kwargs):
            thread = real_thread(**kwargs)
            threads.append(thread)
            return thread

        requests = []

        def empty_response(request, timeout=None):
            requests.append(request.full_url)
            return mock.MagicMock(
                __enter__=lambda _response: io.StringIO("[]"),
                __exit__=lambda _response, *_args: False,
            )

        with (
            mock.patch.object(geocode_module, "_default_geocoder", client),
            mock.patch.object(geocode_module, "_default_base_url", base_url),
            mock.patch.object(app_module.threading, "Thread", new=recording_thread),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(
                geocode_module.urllib.request, "urlopen", side_effect=empty_response
            ),
        ):
            try:
                app._on_place_search(
                    {"request_id": 1, "context": "main", "query": "old"}
                )
                self.assertTrue(old_waiting.wait(2), "old search did not enter geocoder queue")
                app._on_place_search(
                    {"request_id": 2, "context": "main", "query": "new"}
                )
            finally:
                if queue_lock.locked():
                    queue_lock.release()
                for thread in threads:
                    thread.join(5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(requests), 1)
        self.assertIn("q=new", requests[0])

    def test_obsolete_search_response_cannot_publish(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "place_search", return_value=[]),
        ):
            app._on_place_search(
                {"request_id": 1, "context": "home", "query": "old"}
            )
            threads.threads[0].run()
            app._on_place_search(
                {"request_id": 2, "context": "home", "query": "new"}
            )
            idles.run(0)

        self.assertEqual(self.place_events(app), [])

    def test_main_and_home_searches_have_independent_lifetimes(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "place_search", return_value=[]),
        ):
            app._on_place_search(
                {"request_id": "m", "context": "main", "query": "main query"}
            )
            app._on_place_search(
                {"request_id": "h", "context": "home", "query": "home query"}
            )
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(len(self.place_events(app)), 1)
        self.assertEqual(self.place_events(app)[0]["request_id"], "m")


class ReverseLabelRequestTest(ApplicationRequestTestCase):
    def test_slow_home_a_cannot_reverse_newer_home_b(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(
                app_module, "reverse_geocode", return_value="B home"
            ) as reverse_geocode,
        ):
            app._on_location_fix(10.0, 20.0, 50.0)
            app._on_location_fix(30.0, 40.0, 50.0)
            self.assertEqual(len(threads.threads), 1)
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(reverse_geocode.call_args_list, [mock.call(30.0, 40.0)])
        self.assertEqual(app.store.data["location_name"], "B home")
        self.assertEqual(
            (app.store.data["latitude"], app.store.data["longitude"]),
            (30.0, 40.0),
        )
        labels = [
            payload["location_name"]
            for event, payload in app.sent
            if event == "config"
        ]
        self.assertEqual(labels, ["B home"])

    def test_superseded_label_never_reaches_geocoder(self):
        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "reverse_geocode") as reverse_geocode,
        ):
            app._start_view_label(10.0, 20.0)
            app.controller.shutdown()
            threads.threads[0].run()

        reverse_geocode.assert_not_called()
        self.assertEqual(idles.callbacks, [])

        app = self.make_app()
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "reverse_geocode") as reverse_geocode,
        ):
            app._on_location_fix(30.0, 40.0, 50.0)
            app.requests.invalidate("home-label")
            threads.threads[0].run()

        reverse_geocode.assert_not_called()
        self.assertEqual(idles.callbacks, [])

    def test_label_superseded_during_throttle_wait_never_sends(self):
        app = self.make_app()
        app.current_view = (None, (10.0, 20.0))
        threads = ThreadQueue()
        idles = IdleQueue()
        client = geocode_module.Geocoder(
            clock=lambda: 100.0,
            sleep=lambda _seconds: app.requests.invalidate("view-label"),
        )
        client._last_request = 100.0
        base_url = geocode_module._base_url()

        with (
            mock.patch.object(geocode_module, "_default_geocoder", client),
            mock.patch.object(geocode_module, "_default_base_url", base_url),
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(geocode_module.urllib.request, "urlopen") as urlopen,
        ):
            app._start_view_label(10.0, 20.0)
            threads.threads[0].run()

        urlopen.assert_not_called()
        self.assertEqual(idles.callbacks, [])

    def test_queued_view_labels_coalesce_to_newest_target(self):
        app = self.make_app()
        app.current_view = (None, (12.0, 22.0))
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(
                app_module, "reverse_geocode", return_value="Newest view"
            ) as reverse_geocode,
        ):
            app._start_view_label(10.0, 20.0)
            app._start_view_label(11.0, 21.0)
            app._start_view_label(12.0, 22.0)
            self.assertEqual(len(threads.threads), 1)
            threads.threads[0].run()
            for index in range(len(idles.callbacks)):
                idles.run(index)

        self.assertEqual(reverse_geocode.call_args_list, [mock.call(12.0, 22.0)])
        self.assertEqual(
            [payload for event, payload in app.sent if event == "view-name"],
            [{"name": "Newest view"}],
        )

    def test_view_label_requires_matching_coordinates_as_well_as_generation(self):
        app = self.make_app()
        app.current_view = (None, (30.0, 40.0))
        threads = ThreadQueue()
        idles = IdleQueue()

        with (
            mock.patch.object(app_module.threading, "Thread", new=threads.factory),
            mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            mock.patch.object(app_module, "reverse_geocode", return_value="Old view"),
        ):
            app._start_view_label(10.0, 20.0)
            threads.threads[0].run()
            idles.run(0)

        self.assertEqual(
            [payload for event, payload in app.sent if event == "view-name"], []
        )


class LocationModeRequestTest(ApplicationRequestTestCase):
    def settings_message(self):
        return {
            "radius_km": 55.5,
            "heatmap_threshold_km": 50.0,
            "alert_threshold": 101,
            "home_mode": "fixed",
            "temperature_unit": "F",
            "confidence_filter": True,
            "refresh_minutes": 2,
            "home_lat": 30.0,
            "home_lon": 40.0,
            "location_name": "Pinned home",
        }

    def test_leaving_auto_cancels_focus_wait_and_inflight_location(self):
        app = self.make_app(home_mode="auto")
        locator = FakeLocator()
        app.locator = locator
        app._locator_focus_handler = 41
        app._locator_focus_fallback = 42

        with mock.patch.object(app_module.GLib, "source_remove") as source_remove:
            app._save_settings(self.settings_message())

        self.assertEqual(locator.cancel_calls, 1)
        self.assertEqual(app.window.disconnected, [41])
        source_remove.assert_called_once_with(42)
        self.assertIsNone(app._locator_focus_handler)
        self.assertIsNone(app._locator_focus_fallback)

    def test_focus_fallback_rechecks_mode_before_deferred_start(self):
        app = self.make_app(home_mode="fixed")
        app._locator_focus_fallback = 42
        app._start_locator = mock.Mock()

        app._on_locator_focus_fallback()

        app._start_locator.assert_not_called()

    def test_focus_signal_rechecks_mode_before_deferred_start(self):
        app = self.make_app(home_mode="fixed")
        app._clear_locator_focus_wait = mock.Mock()
        app._start_locator = mock.Mock()

        app._on_window_active_for_locator(app.window, None)

        app._start_locator.assert_not_called()

    def test_focus_wait_entry_does_nothing_in_fixed_mode(self):
        app = self.make_app(home_mode="fixed")
        app._start_locator = mock.Mock()

        app._start_locator_when_focused()

        app._start_locator.assert_not_called()
        self.assertIsNone(app._locator_focus_handler)
        self.assertIsNone(app._locator_focus_fallback)


class TransactionalActionTest(ApplicationRequestTestCase):
    @staticmethod
    def capture_os_error(callback):
        try:
            callback()
        except OSError as exc:
            return exc
        return None

    @staticmethod
    def settings_message():
        return {
            "request_id": "settings-17",
            "radius_km": 55.5,
            "heatmap_threshold_km": 50.0,
            "alert_threshold": 101,
            "home_mode": "fixed",
            "temperature_unit": "F",
            "confidence_filter": True,
            "refresh_minutes": 2,
            "home_lat": 30.0,
            "home_lon": 40.0,
            "location_name": "Pinned home",
        }

    def test_settings_permission_and_space_failures_preserve_memory_disk_and_acknowledge(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for failure in failures:
            with self.subTest(error=failure.errno), self.settings_directory() as directory:
                app = self.make_real_store_app(directory)
                path = app.store.path
                previous = deepcopy(app.store.data)
                with mock.patch.object(app.store, "_write", side_effect=failure):
                    caught = self.capture_os_error(
                        lambda: app._save_settings(self.settings_message())
                    )

                self.assertIsNone(caught)
                self.assertEqual(app.store.data, previous)
                self.assertEqual(Store(path).data, previous)
                self.assertEqual(
                    [payload for event, payload in app.sent if event == "config"], []
                )
                results = [
                    payload
                    for event, payload in app.sent
                    if event == "settings-save-result"
                ]
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["request_id"], "settings-17")
                self.assertEqual(results[0]["status"], "error")
                self.assertIn(str(failure), results[0]["error"])
                self.assertIn("try again", results[0]["error"].lower())

    def test_automatic_coordinate_save_failures_are_transactional(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for failure in failures:
            with self.subTest(error=failure.errno), self.settings_directory() as directory:
                app = self.make_real_store_app(directory)
                previous = deepcopy(app.store.data)
                threads = ThreadQueue()
                with (
                    mock.patch.object(app_module.threading, "Thread", new=threads.factory),
                    mock.patch.object(app.store, "_write", side_effect=failure),
                ):
                    caught = self.capture_os_error(
                        lambda: app._on_location_fix(10.0, 20.0, 50.0)
                    )

                self.assertIsNone(caught)
                self.assertEqual(app.store.data, previous)
                self.assertEqual(Store(app.store.path).data, previous)
                self.assertEqual(threads.threads, [])
                self.assertEqual(app.refreshed_homes, [])
                self.assertEqual(
                    [
                        payload
                        for event, payload in app.sent
                        if event in ("location", "config")
                    ],
                    [],
                )
                errors = [
                    payload["message"]
                    for event, payload in app.sent
                    if event == "error"
                ]
                self.assertEqual(len(errors), 1)
                self.assertIn("location", errors[0].lower())
                self.assertIn(str(failure), errors[0])

    def test_automatic_label_save_failures_are_transactional_and_settle_token(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for failure in failures:
            with self.subTest(error=failure.errno), self.settings_directory() as directory:
                app = self.make_real_store_app(directory)
                app.store.data.update({"latitude": 10.0, "longitude": 20.0})
                app.store.save()
                previous = deepcopy(app.store.data)
                app._home_name_generation = 1
                token = app.requests.begin("home-label", 1)
                app.title = mock.Mock()
                with mock.patch.object(app.store, "_write", side_effect=failure):
                    caught = self.capture_os_error(
                        lambda: app._apply_location_name(
                            "Detected home", 10.0, 20.0, 1, token
                        )
                    )

                self.assertIsNone(caught)
                self.assertEqual(app.store.data, previous)
                self.assertEqual(Store(app.store.path).data, previous)
                self.assertFalse(app.requests.is_current(token))
                app.title.set_subtitle.assert_not_called()
                self.assertEqual(
                    [payload for event, payload in app.sent if event == "config"],
                    [],
                )
                errors = [
                    payload["message"]
                    for event, payload in app.sent
                    if event == "error"
                ]
                self.assertEqual(len(errors), 1)
                self.assertIn("location", errors[0].lower())
                self.assertIn(str(failure), errors[0])

    def test_successful_settings_save_publishes_config_and_matching_success(self):
        with self.settings_directory() as directory:
            app = self.make_real_store_app(directory)

            app._save_settings(self.settings_message())

            self.assertEqual(app.store.data["location_name"], "Pinned home")
            self.assertEqual(Store(app.store.path).data, app.store.data)
            published = [
                (event, payload)
                for event, payload in app.sent
                if event in ("config", "settings-save-result")
            ]
            self.assertEqual([event for event, _payload in published], [
                "config",
                "settings-save-result",
            ])
            self.assertEqual(
                published[1][1],
                {"request_id": "settings-17", "status": "success", "error": None},
            )

    def test_favorite_permission_and_space_failures_are_actionable_and_unchanged(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for failure in failures:
            with self.subTest(error=failure.errno), self.settings_directory() as directory:
                sensor = make_sensor(1)
                app = self.make_real_store_app(directory, sensors=[sensor])
                with mock.patch.object(app.store, "_write", side_effect=failure):
                    caught = self.capture_os_error(
                        lambda: app._on_script_message(
                            None, ScriptMessage({"action": "favorite", "id": 1})
                        )
                    )

                self.assertIsNone(caught)
                self.assertEqual(app.store.data["favorites"], [])
                self.assertEqual(Store(app.store.path).data["favorites"], [])
                self.assertFalse(sensor.favorite)
                self.assertEqual(app.sensor_state_sends, 0)
                errors = [
                    payload["message"]
                    for event, payload in app.sent
                    if event == "error"
                ]
                self.assertEqual(len(errors), 1)
                self.assertIn("favorite", errors[0].lower())
                self.assertIn(str(failure), errors[0])
                self.assertIn("try again", errors[0].lower())

    def test_hide_permission_and_space_failures_are_actionable_and_unchanged(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for failure in failures:
            with self.subTest(error=failure.errno), self.settings_directory() as directory:
                sensor = make_sensor(1)
                app = self.make_real_store_app(directory, sensors=[sensor])
                with mock.patch.object(app.store, "_write", side_effect=failure):
                    caught = self.capture_os_error(
                        lambda: app._on_script_message(
                            None, ScriptMessage({"action": "hide", "id": 1})
                        )
                    )

                self.assertIsNone(caught)
                self.assertEqual(app.store.data["hidden"], {})
                self.assertEqual(Store(app.store.path).data["hidden"], {})
                self.assertEqual(app.sensor_state_sends, 0)
                errors = [
                    payload["message"]
                    for event, payload in app.sent
                    if event == "error"
                ]
                self.assertEqual(len(errors), 1)
                self.assertIn("hide", errors[0].lower())
                self.assertIn(str(failure), errors[0])
                self.assertIn("try again", errors[0].lower())

    def test_invalid_settings_also_receive_matching_error_result(self):
        app = self.make_app()
        previous = deepcopy(app.store.data)
        message = self.settings_message()
        del message["radius_km"]

        app._save_settings(message)

        self.assertEqual(app.store.data, previous)
        results = [
            payload
            for event, payload in app.sent
            if event == "settings-save-result"
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["request_id"], "settings-17")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("radius_km", results[0]["error"])
        self.assertEqual(
            [payload for event, payload in app.sent if event == "config"], []
        )
        # The inline Preferences error is the only report; no toast as well.
        self.assertEqual(
            [payload for event, payload in app.sent if event == "error"], []
        )

    def test_malformed_settings_payload_gets_matching_error_result(self):
        malformed = (
            {"radius_km": "NaN", "confidence_filter": "false"},
            {"home_mode": "auto", "location_name": {"x": 1}},
        )
        for changes in malformed:
            with self.subTest(changes=changes):
                app = self.make_app()
                message = {**self.settings_message(), **changes}

                app._on_script_message(
                    None, ScriptMessage({"action": "save-settings", **message})
                )

                results = [
                    payload
                    for event, payload in app.sent
                    if event == "settings-save-result"
                ]
                self.assertEqual(
                    results,
                    [{"request_id": "settings-17", "status": "error", "error": mock.ANY}],
                )
                self.assertEqual(app.store.saves, 0)
                self.assertEqual(
                    [payload for event, payload in app.sent if event == "config"], []
                )
                self.assertEqual(
                    [payload for event, payload in app.sent if event == "error"], []
                )

    def test_unhide_permission_and_space_failures_are_actionable_and_unchanged(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for action in ("unhide", "unhide-all"):
            for failure in failures:
                with self.subTest(action=action, error=failure.errno), self.settings_directory() as directory:
                    app = self.make_real_store_app(directory)
                    app.store.data["hidden"] = {"1": "Sensor 1"}
                    app.store.save()
                    with mock.patch.object(app.store, "_write", side_effect=failure):
                        caught = self.capture_os_error(
                            lambda: app._on_script_message(
                                None, ScriptMessage({"action": action, "id": 1})
                            )
                        )

                    self.assertIsNone(caught)
                    self.assertEqual(app.store.data["hidden"], {"1": "Sensor 1"})
                    self.assertEqual(
                        Store(app.store.path).data["hidden"], {"1": "Sensor 1"}
                    )
                    self.assertEqual(app.sensor_state_sends, 0)
                    errors = [
                        payload["message"]
                        for event, payload in app.sent
                        if event == "error"
                    ]
                    self.assertEqual(len(errors), 1)
                    self.assertIn("unhide", errors[0].lower())
                    self.assertIn(str(failure), errors[0])
                    self.assertIn("try again", errors[0].lower())

    def test_filter_permission_and_space_failures_are_actionable_and_unchanged(self):
        failures = (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left on device"),
        )
        for failure in failures:
            with self.subTest(error=failure.errno), self.settings_directory() as directory:
                app = self.make_real_store_app(directory)
                refreshes = []
                app.refresh = lambda *args, **_kwargs: refreshes.append(args)
                with mock.patch.object(app.store, "_write", side_effect=failure):
                    caught = self.capture_os_error(
                        lambda: app._set_location_filter({"value": "indoor"})
                    )

                self.assertIsNone(caught)
                self.assertEqual(app.store.data["location_filter"], "outdoor")
                self.assertEqual(Store(app.store.path).data["location_filter"], "outdoor")
                self.assertEqual(refreshes, [])
                self.assertEqual(
                    [payload for event, payload in app.sent if event == "config"], []
                )
                errors = [
                    payload["message"]
                    for event, payload in app.sent
                    if event == "error"
                ]
                self.assertEqual(len(errors), 1)
                self.assertIn("location filter", errors[0].lower())
                self.assertIn(str(failure), errors[0])
                self.assertIn("try again", errors[0].lower())


class SettingsDurabilityWarningTest(ApplicationRequestTestCase):
    settings_message = staticmethod(TransactionalActionTest.settings_message)

    @staticmethod
    def error_messages(app):
        return [payload["message"] for event, payload in app.sent if event == "error"]

    @staticmethod
    def record_send_threads(app):
        threads = []
        send = app._send

        def recording_send(event, payload):
            threads.append((event, threading.current_thread()))
            send(event, payload)

        app._send = recording_send
        return threads

    def failing_directory_sync(self):
        return mock.patch.object(
            store_module, "_fsync_directory", side_effect=OSError(errno.EIO, "I/O error")
        )

    def assert_one_main_loop_warning(self, app, idles, send_threads):
        # The worker only queues the warning; nothing is sent until the main
        # loop runs the idle callback.
        self.assertEqual(self.error_messages(app), [])
        self.assertEqual(len(idles.callbacks), 1)
        self.assertEqual(idles.threads[0].name, SYNC_THREAD_NAME)
        idles.run(0)
        errors = self.error_messages(app)
        self.assertEqual(len(errors), 1)
        self.assertIn("could not confirm", errors[0])
        self.assertFalse(errors[0].startswith("Could not save"))
        self.assertEqual(
            [thread for event, thread in send_threads if event == "error"],
            [threading.main_thread()],
        )

    def test_settings_durability_failure_acknowledges_success_and_toasts_from_main_loop(self):
        with self.settings_directory() as directory:
            app = self.make_real_store_app(directory)
            send_threads = self.record_send_threads(app)
            idles = IdleQueue()
            with (
                self.failing_directory_sync(),
                mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            ):
                app._save_settings(self.settings_message())
                self.assertFalse(app.store.flush(5))

            results = [
                payload for event, payload in app.sent if event == "settings-save-result"
            ]
            self.assertEqual(
                results, [{"request_id": "settings-17", "status": "success", "error": None}]
            )
            self.assertIn("config", [event for event, _payload in app.sent])
            self.assertEqual(app.store.data["location_name"], "Pinned home")
            self.assertEqual(Store(app.store.path).data["location_name"], "Pinned home")
            self.assert_one_main_loop_warning(app, idles, send_threads)

    def test_favorite_durability_failure_applies_favorite_and_toasts_once(self):
        with self.settings_directory() as directory:
            sensor = make_sensor(1)
            app = self.make_real_store_app(directory, sensors=[sensor])
            send_threads = self.record_send_threads(app)
            idles = IdleQueue()
            with (
                self.failing_directory_sync(),
                mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            ):
                app._on_script_message(None, ScriptMessage({"action": "favorite", "id": 1}))
                self.assertFalse(app.store.flush(5))

            self.assertEqual(app.store.data["favorites"], [1])
            self.assertEqual(Store(app.store.path).data["favorites"], [1])
            self.assertTrue(sensor.favorite)
            self.assert_one_main_loop_warning(app, idles, send_threads)

    def test_blocked_directory_sync_leaves_the_main_loop_responsive(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_fsync(_path):
            entered.set()
            release.wait(5)

        with self.settings_directory() as directory:
            app = self.make_real_store_app(directory)
            idles = IdleQueue()
            with (
                mock.patch.object(store_module, "_fsync_directory", side_effect=blocked_fsync),
                mock.patch.object(app_module.GLib, "idle_add", new=idles.add),
            ):
                started = time.monotonic()
                try:
                    app._save_settings(self.settings_message())
                    elapsed = time.monotonic() - started
                    # The save returned while the worker is inside the sync.
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(app.store.flush(0.05))
                finally:
                    release.set()
                self.assertTrue(app.store.flush(5))

            self.assertLess(elapsed, 0.5)
            self.assertEqual(
                [payload for event, payload in app.sent if event == "settings-save-result"],
                [{"request_id": "settings-17", "status": "success", "error": None}],
            )
            self.assertEqual(idles.callbacks, [])
            self.assertEqual(self.error_messages(app), [])


class BridgeSensorIdTest(ApplicationRequestTestCase):
    SAFE_MAX = 2**53 - 1
    INVALID = (
        True,
        False,
        1.9,
        1.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        "7",
        "abc",
        None,
        0,
        -1,
        2**53,
        2**63 - 1,
        [7],
    )

    def test_every_sensor_id_action_rejects_hostile_ids_without_side_effects(self):
        for action in ("select", "favorite", "hide", "unhide"):
            for value in self.INVALID:
                with self.subTest(action=action, value=value):
                    with self.settings_directory() as directory:
                        sensor = make_sensor(1)
                        app = self.make_app(sensors=[sensor])
                        app.store = self.real_store(Path(directory) / "config.json")
                        app.store.data["hidden"] = {"1": "Sensor 1"}
                        app.store.save()
                        previous = deepcopy(app.store.data)
                        # JSON carries NaN/Infinity literals exactly as JSC would.
                        raw = json.dumps({"action": action, "id": value})
                        app._on_script_message(
                            None, SimpleNamespace(to_json=lambda _o, raw=raw: raw)
                        )

                        self.assertIsNone(app.selected_id)
                        self.assertEqual(app.store.data, previous)
                        self.assertEqual(Store(app.store.path).data, previous)
                        self.assertEqual(app.sent, [])
                        self.assertEqual(app.sensor_state_sends, 0)

    def test_safe_maximum_id_is_ingested_exactly(self):
        sensor = make_sensor(self.SAFE_MAX)
        sensor.origin = "demo"
        app = self.make_app(sensors=[sensor])

        app._on_script_message(
            None, ScriptMessage({"action": "select", "id": self.SAFE_MAX})
        )

        self.assertEqual(app.selected_id, self.SAFE_MAX)
        self.assertEqual(self.trend_events(app)[-1]["sensor_id"], self.SAFE_MAX)


class ShutdownLifecycleTest(ApplicationRequestTestCase):
    def test_shutdown_cleans_every_owned_resource_exactly_once(self):
        app = self.make_app()
        locator = FakeLocator()
        debug_stops = []
        removed_sources = []
        app.locator = locator
        app._debug_port = SimpleNamespace(stop=lambda: debug_stops.append(True))
        app._auto_refresh_id = 11
        app._locator_focus_handler = 21
        app._locator_focus_fallback = 22
        app.pending_fetch = object()
        app.refreshing = True
        shutdown = getattr(app, "_on_shutdown", None)
        self.assertIsNotNone(shutdown)

        with mock.patch.object(
            app_module.GLib, "source_remove", new=removed_sources.append
        ):
            shutdown()
            shutdown()

        self.assertEqual(removed_sources, [11, 22])
        self.assertEqual(app.window.disconnected, [21])
        self.assertEqual(locator.cancel_calls, 1)
        self.assertEqual(debug_stops, [True])
        self.assertEqual(app.cache.close_calls, 1)
        self.assertIsNone(app._auto_refresh_id)
        self.assertIsNone(app._locator_focus_handler)
        self.assertIsNone(app._locator_focus_fallback)
        self.assertIsNone(app.pending_fetch)
        self.assertFalse(app.refreshing)

    def test_shutdown_flushes_settings_once_with_a_bounded_timeout_before_cache_close(self):
        app = self.make_app()
        order = []
        flush = app.store.flush

        def recording_flush(timeout):
            # No transaction can persist once shutdown has begun.
            order.append(("flush", app._shutting_down))
            return flush(timeout)

        app.store.flush = recording_flush
        app.cache.close = lambda: order.append(("close", app._shutting_down))

        app._on_shutdown()
        app._on_shutdown()

        self.assertEqual(order, [("flush", True), ("close", True)])
        self.assertEqual(len(app.store.flushes), 1)
        self.assertGreater(app.store.flushes[0], 0)
        self.assertLessEqual(app.store.flushes[0], 2.0)

    def test_unconfirmed_settings_at_shutdown_print_one_line(self):
        app = self.make_app()
        app.store.flush_result = False

        with mock.patch.object(sys, "stderr", new_callable=io.StringIO) as stderr:
            app._on_shutdown()

        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("settings", lines[0].lower())

    def test_durability_warning_after_shutdown_begins_sends_nothing(self):
        error = SettingsDurabilityError(
            "Settings were saved, but the system could not confirm they reached "
            "the disk: [Errno 5] I/O error",
            deepcopy(CONFIG),
        )
        app = self.make_app()
        idles = IdleQueue()
        with mock.patch.object(app_module.GLib, "idle_add", new=idles.add):
            # Queued before shutdown, delivered after it began.
            app._on_settings_durability_error(error)
            app._on_shutdown()
            # Reported after shutdown began.
            app._on_settings_durability_error(error)
            for index in range(len(idles.callbacks)):
                idles.run(index)

        self.assertEqual([event for event, _payload in app.sent if event == "error"], [])

    def test_shutdown_rejects_new_work_and_invalidates_old_callbacks(self):
        sensor = make_sensor(1)
        app = self.make_app(sensors=[sensor])
        app.controller = ApplicationController(app.store.data)
        app.webview = object()
        old_refresh = app.controller.begin_refresh()
        old_search = app.requests.begin("place-search:main")
        shutdown = getattr(app, "_on_shutdown", None)
        self.assertIsNotNone(shutdown)
        shutdown()
        previous = deepcopy(app.store.data)
        threads = ThreadQueue()
        replies = []

        with mock.patch.object(app_module.threading, "Thread", new=threads.factory):
            AirloomApplication.refresh(app, force=True)
            app._on_place_search(
                {"request_id": "new", "context": "main", "query": "new work"}
            )
            app._ensure_trend(1)
            app._start_view_label(10.0, 20.0)
            app._on_location_fix(30.0, 40.0, 50.0)
            app._on_script_message(
                None,
                ScriptMessage({
                    "action": "save-settings",
                    **TransactionalActionTest.settings_message(),
                }),
            )
            app._dispatch_debug_command({"cmd": "ping"}, replies.append)

        self.assertEqual(threads.threads, [])
        self.assertFalse(app.requests.is_current(old_search))
        self.assertFalse(
            app.controller.complete_refresh(
                old_refresh, [sensor], "live", "network"
            ).publish
        )
        self.assertEqual(app.store.data, previous)
        self.assertEqual(
            [
                payload
                for event, payload in app.sent
                if event == "settings-save-result"
            ],
            [],
        )
        self.assertEqual(len(replies), 1)
        self.assertFalse(replies[0]["ok"])
        self.assertIn("shutting down", replies[0]["error"])

    def test_shutdown_closes_cache_after_active_worker_drains(self):
        app = self.make_app()
        threads = ThreadQueue()
        completed = []
        start_worker = getattr(app, "_start_worker", None)
        self.assertIsNotNone(start_worker)

        with mock.patch.object(app_module.threading, "Thread", new=threads.factory):
            admitted = start_worker(
                lambda: completed.append(True), name="airloom-test-worker"
            )
            self.assertTrue(admitted)
            app._on_shutdown()
            self.assertEqual(app.cache.close_calls, 0)
            threads.threads[0].run()

        self.assertEqual(completed, [True])
        self.assertEqual(app._active_workers, 0)
        self.assertEqual(app.cache.close_calls, 1)
        app._on_shutdown()
        self.assertEqual(app.cache.close_calls, 1)

    def test_process_exit_drains_every_worker_class_and_closes_cache_once(self):
        script = textwrap.dedent(
            r"""
            import sys
            import threading
            import time
            from pathlib import Path
            from types import SimpleNamespace

            from tests import test_app_requests as h
            from airloom.cache import SensorCache

            cache_path = Path(sys.argv[1])
            marker_path = Path(sys.argv[2])
            app_module = h.app_module
            app_module.GLib.idle_add = lambda *_args: None

            class RecordingCache(SensorCache):
                def close(self):
                    with marker_path.open("a", encoding="utf-8") as marker:
                        marker.write("close\n")
                    super().close()

                def sensors_in(self, bounds):
                    block("startup-cache")
                    return super().sensors_in(bounds)

                def store_trend(self, sensor_id, trend):
                    block("trend-cache")
                    super().store_trend(sensor_id, trend)

            lock = threading.Lock()
            started = []
            all_started = threading.Event()
            release = threading.Event()

            def block(name):
                with lock:
                    started.append(name)
                    if len(started) == 7:
                        all_started.set()
                if not release.wait(5):
                    raise RuntimeError(f"{name} was not released")
                # Main must return while every admitted worker is still active.
                time.sleep(0.25)
                with marker_path.open("a", encoding="utf-8") as marker:
                    marker.write(name + "\n")

            def fetch_readings(_config, _cache, _request, _bounds, _ttl):
                block("refresh")
                return SimpleNamespace()

            def reverse_geocode(latitude, longitude):
                name = "home-label" if (latitude, longitude) == (30.0, 40.0) else "view-label"
                block(name)
                return name

            def place_search(_query):
                block("place-search")
                return []

            class Client:
                def __init__(self, _api_key):
                    pass

                def fetch_rows(self, **_kwargs):
                    block("trend")
                    return h.FetchResult([], 1)

            app_module._fetch_readings = fetch_readings
            app_module.reverse_geocode = reverse_geocode
            app_module.place_search = place_search
            app_module._install_place_search_admission = lambda: None
            app_module.PurpleAirClient = Client

            app = h.AirloomApplication.__new__(h.AirloomApplication)
            app.store = h.FakeStore()
            app.cache = RecordingCache(cache_path)
            app.requests = h.RequestRegistry()
            app.controller = app._new_controller()
            app._worker_lock = threading.Lock()
            app._active_workers = 0
            app._cache_closed = False
            app._active_kinds = {}
            app._coalesced_jobs = {}
            app._coalesced_running = set()
            app._shutting_down = False
            app._debug_port = None
            app._auto_refresh_id = None
            app.refreshing = False
            app.pending_fetch = None
            app._paint_cached_home()
            app.sensors = [h.make_sensor(1)]
            app._queue_trend_write(1, [])
            app._trend_inflight = {}
            app._home_name_generation = 0
            app._view_name_generation = 0
            app.current_view = None
            app.window = None
            app.title = None
            app.locator = None
            app._locator_focus_handler = None
            app._locator_focus_fallback = None
            app.webview = object()
            app._send = lambda *_args: None
            app._refresh_home = lambda *_args, **_kwargs: None

            app._on_location_fix(30.0, 40.0, 50.0)
            app._start_view_label(10.0, 20.0)
            app._on_place_search({"context": "main", "query": "Portland"})
            app._ensure_trend(1)
            app.refresh()
            if not all_started.wait(5):
                raise RuntimeError(f"workers did not all start: {started!r}")
            app._on_shutdown()
            release.set()
            # Returning from this script is the process-lifetime regression:
            # daemon workers are terminated before their finally blocks run.
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "lifecycle.txt"
            result = subprocess.run(
                [
                    sys.executable,
                    "-W",
                    "error::ResourceWarning",
                    "-c",
                    script,
                    str(Path(directory) / "cache.db"),
                    str(marker),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            lines = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []

        diagnostic = f"stdout={result.stdout!r}\nstderr={result.stderr!r}\nmarkers={lines!r}"
        self.assertEqual(len(lines), 8, diagnostic)
        self.assertEqual(lines.count("close"), 1, diagnostic)
        self.assertEqual(lines[-1], "close", diagnostic)
        self.assertEqual(
            set(lines[:-1]),
            {
                "refresh",
                "trend",
                "place-search",
                "home-label",
                "view-label",
                "startup-cache",
                "trend-cache",
            },
            diagnostic,
        )
        self.assertEqual(result.returncode, 0, diagnostic)
        self.assertEqual(result.stderr, "", diagnostic)


class DebugDispatcherRequestTest(ApplicationRequestTestCase):
    def test_non_string_command_receives_immediate_error_reply(self):
        app = self.make_app()
        replies = []

        app._handle_debug_command({"cmd": []}, replies.append)

        self.assertEqual(len(replies), 1)
        self.assertIs(replies[0]["ok"], False)
        self.assertIn("command", replies[0]["error"])

    def test_async_handler_exception_becomes_error_reply(self):
        app = self.make_app()
        idles = IdleQueue()
        replies = []
        app._handle_debug_command = mock.Mock(side_effect=RuntimeError("boom"))

        with mock.patch.object(app_module.GLib, "idle_add", new=idles.add):
            app._dispatch_debug_command({"cmd": "ping"}, replies.append)
            idles.run(0)

        self.assertEqual(replies, [{"ok": False, "error": "dispatcher error: boom"}])


if __name__ == "__main__":
    unittest.main()
