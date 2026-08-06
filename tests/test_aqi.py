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

    def test_epa_correction_linear_branch(self):
        self.assertAlmostEqual(epa_corrected_pm25(20, 50), 11.85, places=2)
        self.assertEqual(epa_corrected_pm25(1, 100), 0.0)
        self.assertIsNone(epa_corrected_pm25(None, 50))

    def test_epa_correction_quadratic_branch(self):
        # 0.46 * 500 + 3.93e-4 * 500**2 + 2.97, independent of humidity
        self.assertAlmostEqual(epa_corrected_pm25(500, 50), 331.22, places=2)
        self.assertEqual(epa_corrected_pm25(500, 10), epa_corrected_pm25(500, 90))
        below = epa_corrected_pm25(342.9, 50)
        above = epa_corrected_pm25(343.0, 50)
        self.assertGreater(above, below)

    def test_epa_correction_missing_humidity_uses_neutral_default(self):
        self.assertEqual(epa_corrected_pm25(20, None), epa_corrected_pm25(20, 50))
        self.assertEqual(epa_corrected_pm25(20, float("nan")), epa_corrected_pm25(20, 50))

    def test_epa_correction_rejects_non_finite_concentration(self):
        self.assertIsNone(epa_corrected_pm25(float("inf"), 50))
        self.assertIsNone(epa_corrected_pm25(float("nan"), 50))

    def test_truncate_handles_non_finite(self):
        self.assertEqual(truncate_pm25(float("inf")), 0.0)
        self.assertEqual(truncate_pm25(float("-inf")), 0.0)
        self.assertEqual(truncate_pm25(float("nan")), 0.0)


if __name__ == "__main__":
    unittest.main()

