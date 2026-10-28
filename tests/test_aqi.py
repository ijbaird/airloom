import unittest

from airloom import aqi
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
        for concentration, expected_aqi in expected.items():
            with self.subTest(concentration=concentration):
                self.assertEqual(aqi_from_pm25(concentration), expected_aqi)

    def test_invalid_and_extreme_inputs(self):
        self.assertIsNone(aqi_from_pm25(None))
        self.assertEqual(aqi_from_pm25(-10), 0)
        self.assertEqual(aqi_from_pm25(800), 500)
        self.assertEqual(aqi_from_pm25(1e30), 500)
        self.assertEqual(aqi_from_pm25(1e308), 500)

    def test_non_finite_aqi_is_unavailable_instead_of_good(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(aqi_from_pm25(value))

    def test_band_metadata(self):
        self.assertEqual(band_for_aqi(42).label, "Good")
        self.assertEqual(band_for_aqi(122).label, "Unhealthy for sensitive groups")
        self.assertEqual(band_for_aqi(None).label, "Unavailable")

    def test_correction_model_version(self):
        self.assertEqual(aqi.CORRECTION_VERSION, 2)

    def test_cf1_correction_final_three_piece_equations(self):
        # Hand-derived from Barkjohn et al. (2022), §3.1.3, equations 1–3.
        fixtures = (
            # 0.524*20 - 0.0862*50 + 5.75
            ("linear below 570", 20, 50, 11.92),
            # At 590, blend weight = 0.496; linear = 311.462,
            # quadratic = 381.2701, so 0.504*311.462 + 0.496*381.2701.
            ("570 to 611 blend", 590, 40, 346.0868176),
            # 0.000421*700**2 + 0.392*700 + 3.44
            ("quadratic at and above 611", 700, 50, 484.13),
        )
        for name, value, humidity, expected in fixtures:
            with self.subTest(branch=name):
                self.assertAlmostEqual(epa_corrected_pm25(value, humidity), expected, places=9)

    def test_cf1_correction_quadratic_ignores_humidity(self):
        self.assertEqual(epa_corrected_pm25(700, 10), epa_corrected_pm25(700, 90))

    def test_atm_correction_all_five_branches(self):
        # Hand-derived from the CF=ATM equations in PurpleAir API discussion post 6.
        fixtures = (
            # 0.524*20 - 0.0862*50 + 5.75
            ("below 30", 20, 11.92),
            # At 40, t=0.5 and slope=(0.524+0.786)/2=0.655.
            ("30 to 50 blend", 40, 27.64),
            # 0.786*100 - 0.0862*50 + 5.75
            ("50 to 210", 100, 80.04),
            # At 235, t=0.5: 0.738*235 - 0.0862*50*0.5
            # + 2.966*0.5 + 5.75*0.5 + 0.000884*235**2*0.5.
            ("210 to 260 blend", 235, 200.04245),
            # 2.966 + 0.69*300 + 0.000884*300**2
            ("260 and above", 300, 289.526),
        )
        for name, value, expected in fixtures:
            with self.subTest(branch=name):
                self.assertAlmostEqual(epa_corrected_pm25(value, 50, basis="ATM"), expected, places=9)

    def test_epa_correction_rejects_unknown_basis(self):
        with self.assertRaises(ValueError):
            epa_corrected_pm25(20, 50, basis="unknown")

    def test_epa_correction_low_values_clamp_to_zero(self):
        self.assertEqual(epa_corrected_pm25(1, 100), 0.0)
        self.assertEqual(epa_corrected_pm25(1, 100, basis="ATM"), 0.0)

    def test_epa_correction_missing_value_is_unavailable(self):
        self.assertIsNone(epa_corrected_pm25(None, 50))
        self.assertIsNone(epa_corrected_pm25(None, 50, basis="ATM"))

    def test_epa_correction_missing_humidity_uses_neutral_default(self):
        self.assertEqual(epa_corrected_pm25(20, None), epa_corrected_pm25(20, 50))
        self.assertEqual(epa_corrected_pm25(20, float("nan")), epa_corrected_pm25(20, 50))

    def test_epa_correction_rejects_non_finite_concentration(self):
        for basis in ("CF1", "ATM"):
            for value in (float("inf"), float("-inf"), float("nan")):
                with self.subTest(basis=basis, value=value):
                    self.assertIsNone(epa_corrected_pm25(value, 50, basis=basis))

    def test_correction_overflow_is_unavailable(self):
        for basis in ("CF1", "ATM"):
            with self.subTest(basis=basis):
                self.assertIsNone(epa_corrected_pm25(1e308, 50, basis=basis))

    def test_large_finite_correction_clamps_to_aqi_scale(self):
        for basis in ("CF1", "ATM"):
            with self.subTest(basis=basis):
                corrected = epa_corrected_pm25(1e30, 50, basis=basis)
                self.assertIsNotNone(corrected)
                self.assertEqual(aqi_from_pm25(corrected), 500)

    def test_truncate_handles_non_finite(self):
        self.assertIsNone(truncate_pm25(float("inf")))
        self.assertIsNone(truncate_pm25(float("-inf")))
        self.assertIsNone(truncate_pm25(float("nan")))

    def test_truncate_avoids_decimal_domain_errors_for_huge_finite_values(self):
        self.assertEqual(truncate_pm25(1e30), 1e30)
        self.assertEqual(truncate_pm25(1e308), 1e308)


if __name__ == "__main__":
    unittest.main()
