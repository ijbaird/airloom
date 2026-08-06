import json
import unittest


class BridgeEncodingTest(unittest.TestCase):
    def test_webkit_string_message_has_a_second_json_layer(self):
        message = {"action": "favorite", "id": 42}
        posted_string = json.dumps(message)
        jsc_to_json = json.dumps(posted_string)
        decoded = json.loads(jsc_to_json)
        if isinstance(decoded, str):
            decoded = json.loads(decoded)
        self.assertEqual(decoded, message)


if __name__ == "__main__":
    unittest.main()
