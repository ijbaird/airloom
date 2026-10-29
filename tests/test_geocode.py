import io
import json
import math
import os
import threading
import unittest
from unittest import mock

from airloom import geocode
from airloom.geocode import GeocodeError, Place, reverse, search


def fake_response(payload):
    return mock.MagicMock(
        __enter__=lambda s: io.StringIO(json.dumps(payload)),
        __exit__=lambda s, *a: False,
    )


def raw_response(text):
    return mock.MagicMock(
        __enter__=lambda s: io.StringIO(text),
        __exit__=lambda s, *a: False,
    )


class FakeTiming:
    def __init__(self, now=100.0):
        self.now = now
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def make_geocoder(**kwargs):
    timing = FakeTiming()
    client = geocode.Geocoder(clock=timing.clock, sleep=timing.sleep, **kwargs)
    return client, timing


class GeocodeTest(unittest.TestCase):
    def setUp(self):
        geocode._last_request = 0.0  # defeat the throttle between tests

    def test_search_parses_places_and_shortens_names(self):
        payload = [
            {"lat": "39.1677", "lon": "-120.1452",
             "display_name": "Tahoe City, Placer County, California, United States"},
            {"lat": "bad", "lon": "-120.0", "display_name": "Broken"},
            "not a dict",
        ]
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response(payload)) as spy:
            places = search("tahoe city")
        self.assertEqual(places, [Place("Tahoe City, Placer County, California", 39.1677, -120.1452)])
        url = spy.call_args[0][0].full_url
        self.assertIn("nominatim.openstreetmap.org/search", url)
        self.assertIn("q=tahoe+city", url)
        self.assertIn("limit=5", url)
        self.assertIn("Airloom/", spy.call_args[0][0].get_header("User-agent"))

    def test_search_empty_query_returns_empty_without_request(self):
        with mock.patch.object(geocode.urllib.request, "urlopen") as spy:
            self.assertEqual(search("   "), [])
        spy.assert_not_called()

    def test_search_wraps_network_errors(self):
        with mock.patch.object(geocode.urllib.request, "urlopen", side_effect=OSError("boom")):
            with self.assertRaises(GeocodeError):
                search("tahoe")

    def test_search_rejects_non_list_payload(self):
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response({"error": "x"})):
            with self.assertRaises(GeocodeError):
                search("tahoe")

    def test_search_uses_environment_endpoint_override(self):
        with mock.patch.dict(os.environ, {"AIRLOOM_NOMINATIM_URL": "https://geocoder.example.test/nominatim/"}):
            with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])) as spy:
                self.assertEqual(search("environment override"), [])
        self.assertTrue(
            spy.call_args[0][0].full_url.startswith(
                "https://geocoder.example.test/nominatim/search?"
            )
        )

    def test_reverse_prefers_smallest_locality(self):
        payload = {"address": {"town": "Tahoe City", "county": "Placer County", "state": "California"}}
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response(payload)) as spy:
            self.assertEqual(reverse(39.1677, -120.1452), "Tahoe City")
        # Town-level zoom: city-level (10) collapses small towns into counties.
        self.assertIn("zoom=14", spy.call_args[0][0].full_url)

    def test_reverse_falls_back_to_coordinates(self):
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response({})):
            self.assertEqual(reverse(39.1677, -120.1452), "39.17, -120.15")

    def test_throttle_spaces_requests_one_second_apart(self):
        sleeps = []
        with mock.patch.object(geocode.time, "sleep", side_effect=sleeps.append):
            with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])):
                search("one")
            with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])):
                search("two")
        self.assertTrue(sleeps and 0 < sleeps[0] <= 1.0)


class GeocoderTest(unittest.TestCase):
    def test_search_discards_nonfinite_and_out_of_range_places(self):
        payload = [
            {"lat": "NaN", "lon": "0", "display_name": "NaN latitude"},
            {"lat": "Infinity", "lon": "0", "display_name": "Infinite latitude"},
            {"lat": "0", "lon": "NaN", "display_name": "NaN longitude"},
            {"lat": "0", "lon": "-Infinity", "display_name": "Infinite longitude"},
            {"lat": "90.0001", "lon": "0", "display_name": "High latitude"},
            {"lat": "-90.0001", "lon": "0", "display_name": "Low latitude"},
            {"lat": "0", "lon": "180.0001", "display_name": "High longitude"},
            {"lat": "0", "lon": "-180.0001", "display_name": "Low longitude"},
            {"lat": "12.5", "lon": "-45.5", "display_name": "Valid place"},
        ]
        with mock.patch.object(geocode.time, "sleep"):
            with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response(payload)):
                places = search("invalid places")
        self.assertEqual(places, [Place("Valid place", 12.5, -45.5)])

    def test_search_accepts_inclusive_coordinate_boundaries(self):
        payload = [
            {"lat": "90", "lon": "180", "display_name": "Northeast boundary"},
            {"lat": "-90", "lon": "-180", "display_name": "Southwest boundary"},
        ]
        client, _ = make_geocoder()
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response(payload)):
            places = client.search("boundaries")
        self.assertEqual(
            places,
            [Place("Northeast boundary", 90.0, 180.0), Place("Southwest boundary", -90.0, -180.0)],
        )

    def test_reverse_rejects_invalid_coordinates_without_request(self):
        invalid_coordinates = [
            (math.nan, 0),
            (math.inf, 0),
            (-math.inf, 0),
            (0, math.nan),
            (90.0001, 0),
            (-90.0001, 0),
            (0, 180.0001),
            (0, -180.0001),
        ]
        client, timing = make_geocoder()
        with mock.patch.object(geocode.urllib.request, "urlopen") as spy:
            for latitude, longitude in invalid_coordinates:
                with self.subTest(latitude=latitude, longitude=longitude):
                    with self.assertRaises(GeocodeError):
                        client.reverse(latitude, longitude)
        spy.assert_not_called()
        self.assertEqual(timing.sleeps, [])

    def test_environment_base_url_is_normalized_for_search_and_reverse(self):
        with mock.patch.dict(os.environ, {"AIRLOOM_NOMINATIM_URL": "https://geocoder.example.test/api/"}):
            client, _ = make_geocoder()
            with mock.patch.object(
                geocode.urllib.request,
                "urlopen",
                side_effect=[fake_response([]), fake_response({})],
            ) as spy:
                self.assertEqual(client.search("tahoe"), [])
                self.assertEqual(client.reverse(1, 2), "1.00, 2.00")
        urls = [call[0][0].full_url for call in spy.call_args_list]
        self.assertTrue(urls[0].startswith("https://geocoder.example.test/api/search?"))
        self.assertTrue(urls[1].startswith("https://geocoder.example.test/api/reverse?"))

    def test_explicit_endpoint_urls_take_precedence_over_environment(self):
        with mock.patch.dict(os.environ, {"AIRLOOM_NOMINATIM_URL": "https://unused.example.test"}):
            client, _ = make_geocoder(
                search_url="https://search.example.test/find",
                reverse_url="https://reverse.example.test/label",
            )
            with mock.patch.object(
                geocode.urllib.request,
                "urlopen",
                side_effect=[fake_response([]), fake_response({})],
            ) as spy:
                client.search("tahoe")
                client.reverse(1, 2)
        urls = [call[0][0].full_url for call in spy.call_args_list]
        self.assertTrue(urls[0].startswith("https://search.example.test/find?"))
        self.assertTrue(urls[1].startswith("https://reverse.example.test/label?"))

    def test_repeated_query_uses_cache_without_throttling(self):
        payload = [{"lat": "1", "lon": "2", "display_name": "Cached place"}]
        client, timing = make_geocoder()
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response(payload)) as spy:
            first = client.search("tahoe")
            second = client.search("tahoe")
        self.assertEqual(first, [Place("Cached place", 1.0, 2.0)])
        self.assertEqual(second, first)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(timing.sleeps, [])

    def test_concurrent_identical_cache_misses_share_one_request(self):
        payload = [{"lat": "1", "lon": "2", "display_name": "Shared place"}]
        expected = [Place("Shared place", 1.0, 2.0)]
        client, _ = make_geocoder()
        first_started = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        call_lock = threading.Lock()
        calls = []
        results = []
        errors = []

        def controlled_urlopen(request, timeout=None):
            with call_lock:
                calls.append(request.full_url)
                call_number = len(calls)
            if call_number == 1:
                first_started.set()
                if not release_first.wait(5):
                    raise TimeoutError("timed out releasing the first request")
            return fake_response(payload)

        def run(is_second=False):
            try:
                results.append(client.search("shared"))
            except BaseException as error:
                errors.append(error)
            finally:
                if is_second:
                    second_done.set()

        first_thread = threading.Thread(target=run)
        second_thread = threading.Thread(target=run, args=(True,))
        with mock.patch.object(
            geocode.urllib.request, "urlopen", side_effect=controlled_urlopen
        ):
            first_thread.start()
            entered_request = first_started.wait(2)
            second_thread.start()
            second_finished_early = second_done.wait(0.1)
            release_first.set()
            first_thread.join(5)
            second_thread.join(5)

        self.assertTrue(entered_request, "first search did not reach the request")
        self.assertFalse(second_finished_early, "second search bypassed the in-flight miss")
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [expected, expected])
        self.assertEqual(len(calls), 1)

    def test_failed_leader_unblocks_waiter_for_retry_and_later_cache(self):
        payload = [{"lat": "1", "lon": "2", "display_name": "Recovered place"}]
        expected = [Place("Recovered place", 1.0, 2.0)]
        client, _ = make_geocoder()
        first_started = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        calls = []
        first_errors = []
        second_results = []
        second_errors = []

        def controlled_urlopen(request, timeout=None):
            with call_lock:
                calls.append(request.full_url)
                call_number = len(calls)
            if call_number == 1:
                first_started.set()
                if not release_first.wait(5):
                    raise TimeoutError("timed out releasing the failed request")
                raise OSError("leader failed")
            return fake_response(payload)

        def fail_first():
            try:
                client.search("retry concurrently")
            except BaseException as error:
                first_errors.append(error)

        def retry_second():
            try:
                second_results.append(client.search("retry concurrently"))
            except BaseException as error:
                second_errors.append(error)

        first_thread = threading.Thread(target=fail_first)
        second_thread = threading.Thread(target=retry_second)
        with mock.patch.object(
            geocode.urllib.request, "urlopen", side_effect=controlled_urlopen
        ):
            first_thread.start()
            entered_request = first_started.wait(2)
            second_thread.start()
            release_first.set()
            first_thread.join(5)
            second_thread.join(5)
            cached = client.search("retry concurrently")

        self.assertTrue(entered_request, "failed search did not reach the request")
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(first_errors), 1)
        self.assertIsInstance(first_errors[0], GeocodeError)
        self.assertEqual(second_errors, [])
        self.assertEqual(second_results, [expected])
        self.assertEqual(cached, expected)
        self.assertEqual(len(calls), 2)

    def test_cached_query_expires_after_five_minutes(self):
        client, timing = make_geocoder()
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])) as spy:
            client.search("tahoe")
            timing.advance(301)
            client.search("tahoe")
        self.assertEqual(spy.call_count, 2)

    def test_cache_is_bounded_and_evicts_the_oldest_query(self):
        client, _ = make_geocoder()
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])) as spy:
            for index in range(65):
                client.search(f"query {index}")
            client.search("query 0")
            client.search("query 63")
        self.assertEqual(spy.call_count, 66)

    def test_cache_distinguishes_result_limits(self):
        rows = [
            {"lat": "1", "lon": "2", "display_name": "First"},
            {"lat": "3", "lon": "4", "display_name": "Second"},
        ]
        client, _ = make_geocoder()
        with mock.patch.object(
            geocode.urllib.request,
            "urlopen",
            side_effect=[fake_response(rows[:1]), fake_response(rows)],
        ) as spy:
            self.assertEqual(client.search("tahoe", limit=1), [Place("First", 1.0, 2.0)])
            self.assertEqual(
                client.search("tahoe", limit=2),
                [Place("First", 1.0, 2.0), Place("Second", 3.0, 4.0)],
            )
        self.assertEqual(spy.call_count, 2)

    def test_empty_queries_make_no_requests_and_do_not_throttle(self):
        client, timing = make_geocoder()
        with mock.patch.object(geocode.urllib.request, "urlopen") as spy:
            self.assertEqual(client.search(""), [])
            self.assertEqual(client.search(" \t\n"), [])
        spy.assert_not_called()
        self.assertEqual(timing.sleeps, [])

    def test_injected_timing_throttles_distinct_queries(self):
        client, timing = make_geocoder()
        with mock.patch.object(geocode.urllib.request, "urlopen", return_value=fake_response([])) as spy:
            client.search("one")
            client.search("two")
            client.search("three")
        self.assertEqual(spy.call_count, 3)
        self.assertEqual(timing.sleeps, [1.0, 1.0])

    def test_failed_request_is_not_cached(self):
        payload = [{"lat": "1", "lon": "2", "display_name": "Recovered place"}]
        client, _ = make_geocoder()
        with mock.patch.object(
            geocode.urllib.request,
            "urlopen",
            side_effect=[OSError("boom"), fake_response(payload)],
        ) as spy:
            with self.assertRaises(GeocodeError):
                client.search("retry")
            self.assertEqual(client.search("retry"), [Place("Recovered place", 1.0, 2.0)])
        self.assertEqual(spy.call_count, 2)

    def test_malformed_json_is_not_cached_and_next_request_retries(self):
        payload = [{"lat": "1", "lon": "2", "display_name": "Recovered place"}]
        client, _ = make_geocoder()
        with mock.patch.object(
            geocode.urllib.request,
            "urlopen",
            side_effect=[raw_response("{not json"), fake_response(payload)],
        ) as spy:
            with self.assertRaises(GeocodeError):
                client.search("malformed")
            self.assertEqual(
                client.search("malformed"), [Place("Recovered place", 1.0, 2.0)])
        self.assertEqual(spy.call_count, 2)

    def test_non_list_payload_is_not_cached_and_next_request_retries(self):
        payload = [{"lat": "1", "lon": "2", "display_name": "Recovered place"}]
        client, _ = make_geocoder()
        with mock.patch.object(
            geocode.urllib.request,
            "urlopen",
            side_effect=[fake_response({"error": "bad"}), fake_response(payload)],
        ) as spy:
            with self.assertRaises(GeocodeError):
                client.search("non-list")
            self.assertEqual(
                client.search("non-list"), [Place("Recovered place", 1.0, 2.0)])
        self.assertEqual(spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
