import unittest

from airloom.location import COARSE_FIX_METERS, is_coarse_fix


class CoarseFixTest(unittest.TestCase):
    def test_precise_fixes_are_not_coarse(self):
        self.assertFalse(is_coarse_fix(250.0))
        self.assertFalse(is_coarse_fix(COARSE_FIX_METERS))

    def test_ip_level_fixes_are_coarse(self):
        self.assertTrue(is_coarse_fix(25000.0))

    def test_missing_accuracy_is_treated_as_coarse(self):
        self.assertTrue(is_coarse_fix(None))


if __name__ == "__main__":
    unittest.main()
