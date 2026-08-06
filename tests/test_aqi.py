import unittest

from airloom.aqi import aqi_from_pm25, band_for_aqi, epa_corrected_pm25, truncate_pm25


class AQITest(unittest.TestCase):
    def test_truncates_instead_of_rounding(self):
        self.assertEqual(truncate_pm25(9.09), 9.0)
        self.assertEqual(truncate_pm25(35.49), 35.4)

    def test_2024_breakpoint_edges(self):
        expected = {
            0.0: 0,
            9.0: 50,
            9.1: 51,
            35.4: 100,
            35.5: 101,
            55.4: 150,
            55.5: 151,
            125.4: 200,
            125.5: 201,
            225.4: 300,
            225.5: 301,
            325.4: 500,
        }
        for concentration, aqi in expected.items():
            with self.subTest(concentration=concentration):
                self.assertEqual(aqi_from_pm25(concentration), aqi)

    def test_invalid_and_extreme_inputs(self):
        self.assertIsNone(aqi_from_pm25(None))
        self.assertEqual(aqi_from_pm25(-10), 0)
        self.assertEqual(aqi_from_pm25(800), 500)

    def test_band_metadata(self):
        self.assertEqual(band_for_aqi(42).label, "Good")
        self.assertEqual(band_for_aqi(122).label, "Unhealthy for sensitive groups")
        self.assertEqual(band_for_aqi(None).label, "Unavailable")

    def test_epa_correction(self):
        self.assertAlmostEqual(epa_corrected_pm25(20, 50), 11.93, places=2)
        self.assertEqual(epa_corrected_pm25(1, 100), 0.0)
        self.assertIsNone(epa_corrected_pm25(None, 50))


if __name__ == "__main__":
    unittest.main()

