import json
import unittest

from airloom.bridge import decode_message


class BridgeDecodingTest(unittest.TestCase):
    def test_webkit_string_message_has_a_second_json_layer(self):
        message = {"action": "favorite", "id": 42}
        jsc_to_json = json.dumps(json.dumps(message))
        self.assertEqual(decode_message(jsc_to_json), message)

    def test_single_layer_message_still_decodes(self):
        message = {"action": "refresh"}
        self.assertEqual(decode_message(json.dumps(message)), message)

    def test_non_object_messages_are_rejected(self):
        for raw in ("[1, 2]", "null", '"just a string"', "5"):
            with self.subTest(raw=raw):
                with self.assertRaises((TypeError, json.JSONDecodeError)):
                    decode_message(raw)

    def test_invalid_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            decode_message("not json")


if __name__ == "__main__":
    unittest.main()
