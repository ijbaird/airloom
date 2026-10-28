import unittest

from airloom.models import CONFIDENCE_THRESHOLD, Sensor, passes_confidence


def sensor(confidence=None, **overrides):
    return Sensor(sensor_id=1, name="S1", latitude=45.5, longitude=-122.6,
                  aqi=40, pm25=9.6, confidence=confidence, **overrides)


class PassesConfidenceTest(unittest.TestCase):
    def test_threshold_is_ninety(self):
        self.assertEqual(CONFIDENCE_THRESHOLD, 90)

    def test_disabled_filter_passes_everything(self):
        for value in (None, 0, 30, 89, 90, 100):
            self.assertTrue(passes_confidence(sensor(value), False))

    def test_enabled_filter_blocks_below_threshold(self):
        self.assertFalse(passes_confidence(sensor(0), True))
        self.assertFalse(passes_confidence(sensor(89), True))
        self.assertTrue(passes_confidence(sensor(90), True))
        self.assertTrue(passes_confidence(sensor(100), True))

    def test_missing_confidence_fails_open(self):
        # Demo sensors and rows cached before the field existed have None.
        self.assertTrue(passes_confidence(sensor(None), True))

    def test_confidence_reaches_the_web_payload(self):
        self.assertEqual(sensor(97).to_dict()["confidence"], 97)
        self.assertIsNone(sensor().to_dict()["confidence"])

    def test_default_provenance_reaches_the_web_payload(self):
        reading = sensor()
        self.assertEqual(reading.origin, "live")
        self.assertEqual(reading.delivery, "network")
        self.assertEqual(reading.to_dict()["origin"], "live")
        self.assertEqual(reading.to_dict()["delivery"], "network")

    def test_explicit_provenance_reaches_the_web_payload(self):
        reading = sensor(origin="demo", delivery="cache")
        self.assertEqual(reading.origin, "demo")
        self.assertEqual(reading.delivery, "cache")
        self.assertEqual(reading.to_dict()["origin"], "demo")
        self.assertEqual(reading.to_dict()["delivery"], "cache")


if __name__ == "__main__":
    unittest.main()
