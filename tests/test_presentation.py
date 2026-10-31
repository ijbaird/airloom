import unittest

from airloom.models import Sensor
from airloom.presentation import (
    MAX_ALERT_OBSERVATION_AGE,
    alert_transitions,
    visible_sensors,
)


NOW = 10_000.0


def make_sensor(
    sensor_id=1,
    *,
    aqi=120,
    confidence=100,
    indoor=False,
    favorite=False,
    last_seen=NOW - 60,
    origin="live",
    delivery="network",
):
    return Sensor(
        sensor_id=sensor_id,
        name=f"Sensor {sensor_id}",
        latitude=45.5,
        longitude=-122.6,
        aqi=aqi,
        pm25=9.6,
        confidence=confidence,
        last_seen=last_seen,
        favorite=favorite,
        indoor=indoor,
        origin=origin,
        delivery=delivery,
    )


class VisibleSensorsTest(unittest.TestCase):
    def test_all_filters_apply_to_network_cached_and_favorite_paths(self):
        # A missing confidence score fails open; an explicit poor score does not.
        for delivery, favorite in (
            ("network", False),
            ("network", True),
            ("cache", False),
            ("cache", True),
        ):
            with self.subTest(delivery=delivery, favorite=favorite):
                sensors = [
                    make_sensor(1, confidence=None, favorite=favorite, delivery=delivery),
                    make_sensor(2, favorite=favorite, delivery=delivery),
                    make_sensor(3, confidence=89, favorite=favorite, delivery=delivery),
                    make_sensor(4, indoor=True, favorite=favorite, delivery=delivery),
                ]
                visible = visible_sensors(
                    sensors,
                    hidden_ids={2},
                    confidence_filter=True,
                    location_filter="outdoor",
                )
                self.assertEqual([sensor.sensor_id for sensor in visible], [1])

    def test_disabled_confidence_and_both_location_filters_keep_readings(self):
        sensors = [
            make_sensor(1, confidence=0),
            make_sensor(2, indoor=True, confidence=89),
        ]
        visible = visible_sensors(
            sensors,
            hidden_ids=set(),
            confidence_filter=False,
            location_filter="both",
        )
        self.assertEqual([sensor.sensor_id for sensor in visible], [1, 2])

    def test_indoor_location_filter_keeps_only_indoor_readings(self):
        sensors = [make_sensor(1), make_sensor(2, indoor=True)]
        visible = visible_sensors(
            sensors,
            hidden_ids=set(),
            confidence_filter=True,
            location_filter="indoor",
        )
        self.assertEqual([sensor.sensor_id for sensor in visible], [2])


class AlertTransitionsTest(unittest.TestCase):
    def test_fresh_real_crossings_alert_once_for_network_and_cache(self):
        for delivery in ("network", "cache"):
            with self.subTest(delivery=delivery):
                reading = make_sensor(favorite=True, delivery=delivery)
                previous = {"1": False}
                alerted, state = alert_transitions(
                    [reading],
                    favorites={1},
                    previous=previous,
                    threshold=101,
                    now=NOW,
                    origin="live",
                )
                self.assertEqual([sensor.sensor_id for sensor in alerted], [1])
                self.assertEqual(state, {"1": True})
                self.assertEqual(previous, {"1": False})

                alerted_again, state_again = alert_transitions(
                    [reading],
                    favorites={1},
                    previous=state,
                    threshold=101,
                    now=NOW,
                    origin="live",
                )
                self.assertEqual(alerted_again, [])
                self.assertEqual(state_again, {"1": True})

    def test_low_crossing_clears_state_without_alerting(self):
        reading = make_sensor(aqi=100, favorite=True)
        alerted, state = alert_transitions(
            [reading],
            favorites={1},
            previous={"1": True},
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, {"1": False})

    def test_demo_origin_neither_alerts_nor_changes_prior_state(self):
        reading = make_sensor(favorite=True)
        previous = {"1": False, "removed": True}
        alerted, state = alert_transitions(
            [reading],
            favorites={1},
            previous=previous,
            threshold=101,
            now=NOW,
            origin="demo",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, previous)
        self.assertEqual(previous, {"1": False, "removed": True})

    def test_demo_sensor_provenance_neither_alerts_nor_changes_state(self):
        reading = make_sensor(favorite=True, origin="demo")
        previous = {"1": False}
        alerted, state = alert_transitions(
            [reading],
            favorites={1},
            previous=previous,
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, previous)

    def test_stale_observations_preserve_high_and_low_states(self):
        stale = NOW - MAX_ALERT_OBSERVATION_AGE - 1
        sensors = [
            make_sensor(1, aqi=120, favorite=True, last_seen=stale),
            make_sensor(2, aqi=20, favorite=True, last_seen=stale),
        ]
        previous = {"1": False, "2": True}
        alerted, state = alert_transitions(
            sensors,
            favorites={1, 2},
            previous=previous,
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, previous)
        self.assertEqual(previous, {"1": False, "2": True})

    def test_observation_exactly_two_hours_old_is_eligible(self):
        reading = make_sensor(
            favorite=True,
            last_seen=NOW - MAX_ALERT_OBSERVATION_AGE,
        )
        alerted, state = alert_transitions(
            [reading],
            favorites={1},
            previous={"1": False},
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual([sensor.sensor_id for sensor in alerted], [1])
        self.assertEqual(state, {"1": True})

    def test_future_observation_beyond_skew_never_alerts_or_changes_state(self):
        sensors = [
            make_sensor(1, aqi=150, favorite=True, last_seen=NOW + 601),
            make_sensor(2, aqi=150, favorite=True, last_seen=2**63 - 1),
            make_sensor(3, aqi=150, favorite=True, last_seen=int(NOW) * 1000),
        ]
        previous = {"1": False, "2": False, "3": False}
        alerted, state = alert_transitions(
            sensors,
            favorites={1, 2, 3},
            previous=previous,
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, previous)

    def test_observation_within_future_skew_is_eligible(self):
        reading = make_sensor(aqi=150, favorite=True, last_seen=NOW + 600)
        alerted, state = alert_transitions(
            [reading],
            favorites={1},
            previous={"1": False},
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual([sensor.sensor_id for sensor in alerted], [1])
        self.assertEqual(state, {"1": True})

    def test_missing_aqi_or_observation_time_does_not_alert_or_create_state(self):
        sensors = [
            make_sensor(1, aqi=None, favorite=True),
            make_sensor(2, favorite=True, last_seen=None),
        ]
        alerted, state = alert_transitions(
            sensors,
            favorites={1, 2},
            previous={},
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, {})

    def test_non_favorites_do_not_alert(self):
        reading = make_sensor(favorite=True)
        alerted, state = alert_transitions(
            [reading],
            favorites=set(),
            previous={},
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, {})

    def test_live_processing_prunes_state_for_removed_favorites(self):
        alerted, state = alert_transitions(
            [],
            favorites={1},
            previous={"1": True, "2": False},
            threshold=101,
            now=NOW,
            origin="live",
        )
        self.assertEqual(alerted, [])
        self.assertEqual(state, {"1": True})


if __name__ == "__main__":
    unittest.main()
