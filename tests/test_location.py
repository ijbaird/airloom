import unittest

from airloom.location import COARSE_FIX_METERS, GeoClueLocator, is_coarse_fix


class CoarseFixTest(unittest.TestCase):
    def test_precise_fixes_are_not_coarse(self):
        self.assertFalse(is_coarse_fix(250.0))
        self.assertFalse(is_coarse_fix(COARSE_FIX_METERS))

    def test_ip_level_fixes_are_coarse(self):
        self.assertTrue(is_coarse_fix(25000.0))

    def test_missing_accuracy_is_treated_as_coarse(self):
        self.assertTrue(is_coarse_fix(None))


class DeliveryBookkeepingTest(unittest.TestCase):
    """The locator's delivery rules, exercised without GLib or GeoClue.

    A "fallback" is a failed attempt (timeout, denial, missing GeoClue)
    reported to the callback as (None, None, None); a "fix" is a genuine
    coordinate delivery. The permission dialog can outlive the timeout, so
    a fix after a fallback must still be delivered.
    """

    def test_fix_is_delivered_once(self):
        locator = GeoClueLocator()
        self.assertTrue(locator._note_fix())
        self.assertFalse(locator._note_fix())

    def test_fallback_is_reported_once(self):
        locator = GeoClueLocator()
        self.assertTrue(locator._note_fallback())
        self.assertFalse(locator._note_fallback())

    def test_fix_after_fallback_is_still_delivered(self):
        # Timeout fired while the user was reading the permission dialog;
        # the fix that follows their "Allow" click must not be dropped.
        locator = GeoClueLocator()
        self.assertTrue(locator._note_fallback())
        self.assertTrue(locator._note_fix())

    def test_fallback_after_fix_is_suppressed(self):
        # A late timeout or error must not tell the user detection failed
        # when a fix was already delivered.
        locator = GeoClueLocator()
        self.assertTrue(locator._note_fix())
        self.assertFalse(locator._note_fallback())


if __name__ == "__main__":
    unittest.main()
