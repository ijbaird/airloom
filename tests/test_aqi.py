import unittest

from airloom import aqi
from airloom.aqi import BANDS, aqi_from_pm25, band_for_aqi, epa_corrected_pm25, truncate_pm25
from airloom.models import Sensor


def _contrast(a, b):
    """WCAG 2.x contrast ratio between two #rrggbb colors."""
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    lighter, darker = sorted((luminance(a), luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


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

    def test_cf1_correction_exact_and_adjacent_threshold_fixtures(self):
        # Independently evaluated with Decimal from Barkjohn equations 1–3.
        fixtures = (
            (569.99, 300.11476),
            (570.0, 300.6283432),
            (570.01, 300.6491163521206724),
            (610.99, 400.7514228748386476),
            (611.0, 400.120141),
            (611.01, 400.1292056621),
        )
        for value, expected in fixtures:
            with self.subTest(value=value):
                self.assertAlmostEqual(
                    epa_corrected_pm25(value, 50, basis="CF1"), expected, places=9)

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

    def test_atm_correction_exact_and_adjacent_threshold_fixtures(self):
        # Independently evaluated with Decimal from the published ATM equations.
        fixtures = (
            (29.99, 17.15476),
            (30.0, 17.16),
            (30.01, 17.16917131),
            (49.99, 40.72559131),
            (50.0, 40.74),
            (50.01, 40.74786),
            (209.99, 166.49214),
            (210.0, 166.5),
            (210.01, 166.51193063057768),
            (259.99, 242.10563913574232),
            (260.0, 242.1244),
            (260.01, 242.1358968884),
        )
        for value, expected in fixtures:
            with self.subTest(value=value):
                self.assertAlmostEqual(
                    epa_corrected_pm25(value, 50, basis="ATM"), expected, places=9)

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


class BandContrastTest(unittest.TestCase):
    def test_band_colors_are_unchanged(self):
        self.assertEqual(
            [band.color for band in BANDS],
            ["#35b779", "#f6c945", "#f39c3d", "#e65b65", "#9b6bc3", "#7e394d"],
        )
        self.assertEqual(band_for_aqi(None).color, "#7d8590")

    def test_band_foregrounds_meet_normal_text_contrast(self):
        bands = [*BANDS, band_for_aqi(None)]
        self.assertEqual(
            [band.foreground for band in bands],
            ["#08271b", "#332400", "#341900", "#2a0509", "#12061c", "#ffffff", "#0b0e11"],
        )
        for band in bands:
            with self.subTest(band=band.label):
                self.assertGreaterEqual(_contrast(band.color, band.foreground), 4.5)

    def test_sensor_payload_uses_band_foreground(self):
        self.assertEqual(Sensor(1, "x", 0, 0, 180, 70.0).to_dict()["foreground"], "#2a0509")


if __name__ == "__main__":
    unittest.main()
