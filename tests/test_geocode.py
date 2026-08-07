import io
import json
import unittest
from unittest import mock

from airloom import geocode
from airloom.geocode import GeocodeError, Place, reverse, search


def fake_response(payload):
    return mock.MagicMock(
        __enter__=lambda s: io.StringIO(json.dumps(payload)),
        __exit__=lambda s, *a: False,
    )


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


if __name__ == "__main__":
    unittest.main()
