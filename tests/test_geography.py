import unittest

from airloom import purpleair
from airloom.geography import (
    Bounds,
    bounds_around,
    bounds_contains,
    nearest_longitude,
    split_bounds,
)


class BoundsTest(unittest.TestCase):
    def test_positional_field_order_remains_compatible(self):
        bounds = Bounds(10.0, -20.0, -10.0, 20.0)
        self.assertEqual(
            (bounds.north, bounds.west, bounds.south, bounds.east),
            (10.0, -20.0, -10.0, 20.0),
        )

    def test_rejects_non_finite_coordinates(self):
        valid = {"north": 10.0, "west": -20.0, "south": -10.0, "east": 20.0}
        for field in valid:
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        Bounds(**{**valid, field: value})

    def test_rejects_out_of_range_coordinates(self):
        valid = {"north": 10.0, "west": -20.0, "south": -10.0, "east": 20.0}
        cases = (
            ("north", 90.1),
            ("north", -90.1),
            ("south", 90.1),
            ("south", -90.1),
            ("west", 180.1),
            ("west", -180.1),
            ("east", 180.1),
            ("east", -180.1),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    Bounds(**{**valid, field: value})

    def test_rejects_inverted_latitude(self):
        with self.assertRaises(ValueError):
            Bounds(-10.0, -20.0, 10.0, 20.0)


class SplitBoundsTest(unittest.TestCase):
    def test_crossing_bounds_split_at_both_antimeridian_edges(self):
        crossing = Bounds(10.0, 179.0, -10.0, -179.0)
        self.assertEqual(
            split_bounds(crossing),
            (
                Bounds(10.0, 179.0, -10.0, 180.0),
                Bounds(10.0, -180.0, -10.0, -179.0),
            ),
        )

    def test_noncrossing_bounds_return_a_one_tuple(self):
        bounds = Bounds(10.0, -20.0, -10.0, 20.0)
        self.assertEqual(split_bounds(bounds), (bounds,))


class BoundsContainsTest(unittest.TestCase):
    def test_crossing_outer_contains_inner_bounds_on_both_sides(self):
        outer = Bounds(10.0, 170.0, -10.0, -170.0)
        for inner in (
            Bounds(5.0, 175.0, -5.0, 179.0),
            Bounds(5.0, -179.0, -5.0, -175.0),
            Bounds(5.0, 175.0, -5.0, -175.0),
        ):
            with self.subTest(inner=inner):
                self.assertTrue(bounds_contains(outer, inner))

    def test_crossing_outer_rejects_bounds_outside_either_arm(self):
        outer = Bounds(10.0, 170.0, -10.0, -170.0)
        for inner in (
            Bounds(5.0, -175.0, -5.0, 175.0),
            Bounds(5.0, 165.0, -5.0, -165.0),
            Bounds(11.0, 175.0, -5.0, 179.0),
        ):
            with self.subTest(inner=inner):
                self.assertFalse(bounds_contains(outer, inner))

    def test_full_world_contains_crossing_bounds(self):
        world = Bounds(90.0, -180.0, -90.0, 180.0)
        self.assertTrue(bounds_contains(world, Bounds(10.0, 179.0, -10.0, -179.0)))


class BoundsAroundTest(unittest.TestCase):
    def test_wraps_east_and_west_across_the_antimeridian(self):
        east = bounds_around(0.0, 179.5, 100.0)
        self.assertAlmostEqual(east.west, 178.5990990991)
        self.assertAlmostEqual(east.east, -179.5990990991)
        self.assertGreater(east.west, east.east)

        west = bounds_around(0.0, -179.5, 100.0)
        self.assertAlmostEqual(west.west, 179.5990990991)
        self.assertAlmostEqual(west.east, -178.5990990991)
        self.assertGreater(west.west, west.east)

    def test_rejects_invalid_center_and_radius(self):
        cases = (
            (float("nan"), 0.0, 20.0),
            (float("inf"), 0.0, 20.0),
            (90.1, 0.0, 20.0),
            (-90.1, 0.0, 20.0),
            (0.0, float("nan"), 20.0),
            (0.0, float("-inf"), 20.0),
            (0.0, 180.1, 20.0),
            (0.0, -180.1, 20.0),
            (0.0, 0.0, float("nan")),
            (0.0, 0.0, float("inf")),
            (0.0, 0.0, float("-inf")),
        )
        for latitude, longitude, radius in cases:
            with self.subTest(latitude=latitude, longitude=longitude, radius=radius):
                with self.assertRaises(ValueError):
                    bounds_around(latitude, longitude, radius)


class NearestLongitudeTest(unittest.TestCase):
    def test_projects_to_the_nearest_world_on_both_sides(self):
        self.assertEqual(nearest_longitude(-179.0, 179.0), 181.0)
        self.assertEqual(nearest_longitude(179.0, -179.0), -181.0)
        self.assertEqual(nearest_longitude(170.0, -170.0), -190.0)

    def test_supports_an_unwrapped_reference_and_deterministic_ties(self):
        self.assertEqual(nearest_longitude(-179.0, 540.0), 541.0)
        self.assertEqual(nearest_longitude(179.0, -540.0), -541.0)
        self.assertEqual(nearest_longitude(0.0, 180.0), 360.0)
        self.assertEqual(nearest_longitude(0.0, -180.0), 0.0)

    def test_rejects_non_finite_or_noncanonical_longitudes(self):
        cases = (
            (float("nan"), 0.0),
            (float("inf"), 0.0),
            (180.1, 0.0),
            (-180.1, 0.0),
            (0.0, float("nan")),
            (0.0, float("inf")),
        )
        for longitude, reference in cases:
            with self.subTest(longitude=longitude, reference=reference):
                with self.assertRaises(ValueError):
                    nearest_longitude(longitude, reference)


class PurpleAirCompatibilityTest(unittest.TestCase):
    def test_existing_imports_reexport_canonical_primitives(self):
        self.assertIs(purpleair.Bounds, Bounds)
        self.assertIs(purpleair.bounds_around, bounds_around)
        self.assertIs(purpleair.bounds_contains, bounds_contains)


if __name__ == "__main__":
    unittest.main()
