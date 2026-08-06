import json
import tempfile
import unittest
from pathlib import Path

from airloom.store import Store


class StoreTest(unittest.TestCase):
    def test_round_trip_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.data["api_key"] = "secret-1234"
            store.toggle_favorite(42)
            loaded = Store(path)
            self.assertEqual(loaded.data["api_key"], "secret-1234")
            self.assertEqual(loaded.data["favorites"], [42])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(loaded.public_config()["api_key_hint"], "••••1234")
            self.assertNotIn("api_key", loaded.public_config())

    def test_malformed_config_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("not json", encoding="utf-8")
            store = Store(path)
            self.assertEqual(store.data["location_name"], "Portland, Oregon")


if __name__ == "__main__":
    unittest.main()

