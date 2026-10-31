"""Contracts between 0.14.0 workstreams, each exercised through both sides.

Alert eligibility (controller) and the observation freshness rule (models),
reverse-label workers (adapter) and the shared Geocoder's cache (data), and
the Store's background durability reporting (data) and the adapter's toast.
"""

from __future__ import annotations

import errno
import importlib.util
import io
import json
import threading
import time
import unittest
from pathlib import Path
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

# Modules, not names: importing their TestCases here would run them twice.
try:
    from tests import test_app_requests as app_requests
    from tests import test_controller as controller_tests
except ImportError:
    import test_app_requests as app_requests
    import test_controller as controller_tests

from airloom import geocode as geocode_module
from airloom import models, presentation
from airloom.store import SYNC_THREAD_NAME, Store


class AlertFreshnessContractTest(unittest.TestCase):
    """Alerts use models.is_fresh_observation with the controller's wall clock."""

    CASES = (
        (7200, True),
        (-600, True),
        (7201, False),
        (-601, False),
        (None, False),
    )

    def test_alert_eligibility_is_the_freshness_rule_at_the_controller_clock(self):
        self.assertIs(presentation.is_fresh_observation, models.is_fresh_observation)
        for age, eligible in self.CASES:
            with self.subTest(age=age):
                h = controller_tests.Harness(favorites=[1], alert_states={})
                last_seen = None if age is None else int(h.wall) - age
                h.finish(
                    h.controller.refresh(),
                    [controller_tests.reading(1, aqi=150, last_seen=last_seen)],
                )
                with mock.patch.object(
                    presentation, "is_fresh_observation", wraps=models.is_fresh_observation
                ) as fresh:
                    decision = h.controller.evaluate_alerts()

                self.assertEqual(
                    [sensor.sensor_id for sensor in decision.notify], [1] if eligible else []
                )
                self.assertEqual(h.config["alert_states"].get("1", False), eligible)
                calls = fresh.call_args_list
                self.assertTrue(calls)
                self.assertEqual(calls, [mock.call(last_seen, h.wall)] * len(calls))


LABEL = "Fasttown"


def label_response(request, timeout=None):
    return mock.MagicMock(
        __enter__=lambda _response: io.StringIO(json.dumps({"address": {"town": LABEL}})),
        __exit__=lambda _response, *_args: False,
    )


class ReverseLabelCacheContractTest(app_requests.ApplicationRequestTestCase):
    """The adapter's label workers on the real shared Geocoder, fake transport."""

    def setUp(self):
        self.app = self.make_app()
        self.idles = app_requests.IdleQueue()
        self.client = geocode_module.Geocoder(clock=lambda: 100.0, sleep=lambda _s: None)
        self.urls = []

        def transport(request, timeout=None):
            self.urls.append(request.full_url)
            return self.respond(request, timeout)

        self.respond = label_response
        for patch in (
            mock.patch.object(geocode_module, "_default_geocoder", self.client),
            mock.patch.object(geocode_module, "_default_base_url", geocode_module._base_url()),
            mock.patch.object(app_module.GLib, "idle_add", new=self.idles.add),
            mock.patch.object(geocode_module.urllib.request, "urlopen", side_effect=transport),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def worker(self, kind):
        """The label worker for ``kind`` and a fresh token it may run under."""
        token = self.app.requests.begin(kind, 1)
        run = {
            "home-label": self.app._reverse_label_worker,
            "view-label": self.app._view_label_worker,
        }[kind]
        return token, lambda: run(10.0, 20.0, 1, token)

    def labels(self):
        return [args[0] for _callback, args in self.idles.callbacks]

    def check_superseded_at_the_throttle(self, kind):
        token, run = self.worker(kind)
        # The throttle must wait; the token is superseded while it does.
        self.client._last_request = 100.0
        self.client._sleep = lambda _s: self.app.requests.invalidate(kind)
        run()
        self.assertFalse(self.app.requests.is_current(token))
        self.assertEqual(self.urls, [])
        self.assertEqual(self.idles.callbacks, [])

        # A current worker still makes exactly one request.
        self.client._sleep = lambda _s: None
        _token, run = self.worker(kind)
        run()
        self.assertEqual(len(self.urls), 1)
        self.assertIn("lat=10.000000", self.urls[0])
        self.assertEqual(self.labels(), [LABEL])

    def test_home_label_superseded_at_the_throttle_makes_no_request(self):
        self.check_superseded_at_the_throttle("home-label")

    def test_view_label_superseded_at_the_throttle_makes_no_request(self):
        self.check_superseded_at_the_throttle("view-label")

    def test_cached_label_makes_no_request(self):
        _token, home = self.worker("home-label")
        home()
        _token, view = self.worker("view-label")
        view()

        self.assertEqual(len(self.urls), 1)
        self.assertEqual(self.labels(), [LABEL, LABEL])

    def test_coalesced_duplicate_label_makes_no_request(self):
        first_sending = threading.Event()
        second_waiting = threading.Event()
        real_lock = self.client._reverse_lock

        class SignallingLock:
            def __enter__(self):
                if first_sending.is_set():
                    second_waiting.set()
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        def held_response(request, timeout=None):
            first_sending.set()
            # Answer once the duplicate is queued behind this request.
            second_waiting.wait(5)
            return label_response(request, timeout)

        self.client._reverse_lock = SignallingLock()
        self.respond = held_response
        _token, home = self.worker("home-label")
        _token, view = self.worker("view-label")
        threads = [threading.Thread(target=home)]
        threads[0].start()
        try:
            self.assertTrue(first_sending.wait(5))
            threads.append(threading.Thread(target=view))
            threads[1].start()
        finally:
            for thread in threads:
                thread.join(5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertTrue(second_waiting.is_set())
        self.assertEqual(len(self.urls), 1)
        self.assertEqual(self.labels(), [LABEL, LABEL])


class SettingsInspectionWarningContractTest(app_requests.ApplicationRequestTestCase):
    """A Store that cannot inspect its directory still reaches the adapter's toast.

    SettingsDurabilityWarningTest (test_app_requests) already pins a failed and
    a blocked directory fsync through a real Store and this adapter; this is
    the inspection-failure path added to the Store for 0.14.0."""

    def test_inspection_failure_toasts_once_on_the_main_loop_and_the_next_save_recovers(self):
        with self.settings_directory() as directory:
            app = self.make_real_store_app(directory)
            settings_dir = app.store.path.parent
            real_is_dir = Path.is_dir
            denied = []
            send_threads = []
            send = app._send

            def recording_send(event, payload):
                send_threads.append((event, threading.current_thread()))
                send(event, payload)

            def is_dir(path, *args, **kwargs):
                if (threading.current_thread().name == SYNC_THREAD_NAME
                        and path == settings_dir and not denied):
                    denied.append(path)
                    raise PermissionError(errno.EACCES, "Permission denied", str(path))
                return real_is_dir(path, *args, **kwargs)

            app._send = recording_send
            idles = app_requests.IdleQueue()
            with mock.patch.object(app_module.GLib, "idle_add", new=idles.add):
                with mock.patch.object(Path, "is_dir", is_dir):
                    started = time.monotonic()
                    app._save_settings(app_requests.TransactionalActionTest.settings_message())
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertFalse(app.store.flush(5))

                self.assertEqual(denied, [settings_dir])
                self.assertEqual(app.store.data["location_name"], "Pinned home")
                self.assertEqual(Store(app.store.path).data["location_name"], "Pinned home")
                self.assertEqual(
                    [p for e, p in app.sent if e == "settings-save-result"],
                    [{"request_id": "settings-17", "status": "success", "error": None}],
                )
                # Nothing is sent until the main loop runs the queued warning.
                self.assertEqual(self.errors(app), [])
                self.assertEqual(len(idles.callbacks), 1)
                self.assertEqual(idles.threads[0].name, SYNC_THREAD_NAME)
                idles.run(0)
                self.assertEqual(len(self.errors(app)), 1)
                self.assertIn("could not confirm", self.errors(app)[0])
                self.assertEqual(
                    [thread for event, thread in send_threads if event == "error"],
                    [threading.main_thread()],
                )

                # Access restored: the next save is confirmed, with no warning.
                app.store.save()
                self.assertTrue(app.store.flush(5))
                self.assertEqual(len(idles.callbacks), 1)

    @staticmethod
    def errors(app):
        return [payload["message"] for event, payload in app.sent if event == "error"]


def load_shared_favorite_fixture():
    path = Path(__file__).resolve().parent / "js" / "fixtures" / "shared_favorite_trend.py"
    spec = importlib.util.spec_from_file_location("shared_favorite_trend", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SharedFavoriteTrendContractTest(unittest.TestCase):
    """A favorite also in the area is published, and trended, as one reading.

    The page keys trend state per published reading (W3), so the backend's
    trend events must carry the published favorite's own last_seen. The
    page side of this contract is tests/js/state.test.js ("a shared-id
    favorite renders the trend ..."), fed by the same fixture."""

    def setUp(self):
        self.fixture = load_shared_favorite_fixture()

    def sensor_events(self, events):
        return [payload for event, payload in events if event == "sensors"]

    def trend_events(self, events, sensor_id):
        return [
            payload for event, payload in events
            if event == "trend" and payload["sensor_id"] == sensor_id
        ]

    def test_the_shared_favorite_is_the_area_reading_in_every_collection(self):
        fixture = self.fixture
        result = fixture.capture([])

        favorite_fetches = [call["ids"] for call in result["calls"] if call["kind"] == "favorites"]
        self.assertEqual(favorite_fetches, [[fixture.REMOTE_ID]])
        publications = self.sensor_events(result["events"])
        self.assertTrue(publications)
        for payload in publications:
            for collection in ("items", "area", "favorites"):
                with self.subTest(collection=collection):
                    shared = [s for s in payload[collection] if s["id"] == fixture.SHARED_ID]
                    self.assertEqual(len(shared), 1)
                    self.assertEqual(shared[0]["last_seen"], fixture.AREA_SEEN)
                    self.assertIs(shared[0]["remote_favorite"], False)
                    self.assertIs(shared[0]["favorite"], True)

    def test_selecting_a_favorite_trends_the_reading_it_was_published_as(self):
        fixture = self.fixture
        for sensor_id in (fixture.SHARED_ID, fixture.REMOTE_ID):
            with self.subTest(sensor_id=sensor_id):
                result = fixture.capture([{"action": "select", "id": sensor_id}])
                published = self.sensor_events(result["events"])[-1]
                favorite = next(s for s in published["favorites"] if s["id"] == sensor_id)
                trends = self.trend_events(result["events"], sensor_id)

                self.assertEqual([t["status"] for t in trends], ["loading", "loaded"])
                self.assertEqual(
                    {t["reading_last_seen"] for t in trends}, {favorite["last_seen"]}
                )
                self.assertEqual(published["selected_id"], sensor_id)
                self.assertIn({"kind": "trend", "ids": [sensor_id]}, result["calls"])


if __name__ == "__main__":
    unittest.main()
