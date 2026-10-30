from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

from airloom.requests import RequestRegistry, RequestToken


class RequestTokenTest(unittest.TestCase):
    def test_token_is_frozen(self):
        token = RequestToken(1, "refresh", 0)

        with self.assertRaises(FrozenInstanceError):
            token.request_id = 2


class RequestRegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = RequestRegistry()

    def test_request_ids_increase_monotonically_across_contexts(self):
        first = self.registry.begin("refresh")
        second = self.registry.begin("trend:7", generation=3)
        third = self.registry.begin("refresh")

        self.assertEqual(first, RequestToken(1, "refresh", 0))
        self.assertEqual(second, RequestToken(2, "trend:7", 3))
        self.assertEqual(third.request_id, 3)

    def test_new_request_supersedes_same_context(self):
        stale = self.registry.begin("place-search")
        current = self.registry.begin("place-search")

        self.assertFalse(self.registry.is_current(stale))
        self.assertTrue(self.registry.is_current(current))

    def test_independent_contexts_remain_current(self):
        refresh = self.registry.begin("refresh")
        search = self.registry.begin("place-search")

        self.assertTrue(self.registry.is_current(refresh))
        self.assertTrue(self.registry.is_current(search))

    def test_stale_finish_does_not_finish_newer_request(self):
        stale = self.registry.begin("refresh")
        current = self.registry.begin("refresh")

        self.registry.finish(stale)

        self.assertTrue(self.registry.is_current(current))

    def test_finish_makes_current_request_not_current(self):
        token = self.registry.begin("refresh")

        self.registry.finish(token)

        self.assertFalse(self.registry.is_current(token))

    def test_generation_mismatch_is_not_current_and_cannot_finish_token(self):
        token = self.registry.begin("home", generation=4)
        mismatched = RequestToken(token.request_id, token.context, generation=5)

        self.assertFalse(self.registry.is_current(mismatched))
        self.registry.finish(mismatched)
        self.assertTrue(self.registry.is_current(token))

    def test_targeted_invalidation_only_invalidates_that_context(self):
        refresh = self.registry.begin("refresh")
        search = self.registry.begin("place-search")

        self.registry.invalidate("refresh")

        self.assertFalse(self.registry.is_current(refresh))
        self.assertTrue(self.registry.is_current(search))

    def test_global_invalidation_invalidates_every_context(self):
        refresh = self.registry.begin("refresh")
        search = self.registry.begin("place-search")

        self.registry.invalidate()

        self.assertFalse(self.registry.is_current(refresh))
        self.assertFalse(self.registry.is_current(search))

    def test_replacement_after_invalidation_is_newer_and_isolated_from_old_token(self):
        for invalidate in (
            lambda registry: registry.invalidate("trend:7"),
            lambda registry: registry.invalidate(),
        ):
            with self.subTest(invalidate=invalidate):
                registry = RequestRegistry()
                old = registry.begin("trend:7", generation=3)
                invalidate(registry)

                replacement = registry.begin("trend:7", generation=3)

                self.assertGreater(replacement.request_id, old.request_id)
                self.assertNotEqual(replacement, old)
                self.assertFalse(registry.is_current(old))
                self.assertTrue(registry.is_current(replacement))
                # A late completion of the invalidated request settles nothing.
                registry.finish(old)
                self.assertFalse(registry.is_current(old))
                self.assertTrue(registry.is_current(replacement))

    def test_concurrent_begins_have_unique_ids_and_one_current_token_per_context(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(
                lambda index: self.registry.begin(f"context:{index % 8}"),
                range(200),
            ))

        self.assertEqual({token.request_id for token in tokens}, set(range(1, 201)))
        newest = {}
        for token in tokens:
            if token.context not in newest or token.request_id > newest[token.context].request_id:
                newest[token.context] = token
        for token in tokens:
            with self.subTest(context=token.context, request_id=token.request_id):
                self.assertEqual(
                    self.registry.is_current(token),
                    token == newest[token.context],
                )


if __name__ == "__main__":
    unittest.main()
