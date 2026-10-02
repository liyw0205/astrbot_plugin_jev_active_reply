import asyncio
import math
import unittest
import tempfile
from pathlib import Path

from ..client import Adapter, DecisionError, JevClient, KeyPool, retry_delay
from ..policy import QUESTIONS
from ..state import Ledger


def answer(questions=QUESTIONS):
    return {
        "model": "jev-1.13.0",
        "answers": {key: {"type": "noul", "noul": 0.9} for key in questions},
        "usage": {"input_tokens": 30, "output_tokens": 10},
    }


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_pool_budget_is_durable_and_shared(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Ledger(Path(directory) / "daily.db")
            for _ in range(2):
                pool = KeyPool(
                    "mindshub", {"keys": ["a", "b"], "daily_request_limit": 2}
                )
                pool.ledger = store
                key = await pool.acquire()
                self.assertIsNotNone(key)
                await pool.release(key)
            pool = KeyPool("mindshub", {"keys": ["c"], "daily_request_limit": 2})
            pool.ledger = store
            self.assertIsNone(await pool.acquire())
            official = KeyPool("typesafe", {"keys": ["official"]})
            official.ledger = store
            self.assertIsNotNone(await official.acquire())
            store.close()

    async def test_each_channel_rotates(self):
        for kind in ("typesafe", "mindshub"):
            pool = KeyPool(kind, {"keys": ["a", "b", "c"]})
            got = []
            for _ in range(6):
                key = await pool.acquire()
                got.append(key.secret)
                await pool.release(key)
            self.assertEqual(got, ["a", "b", "c"] * 2)

    async def test_shared_quota_and_expiration(self):
        clock = Clock()
        pool = KeyPool("mindshub", {"keys": ["a", "b"], "quota_rpm": 2}, clock)
        for _ in range(2):
            key = await pool.acquire()
            await pool.release(key)
        self.assertIsNone(await pool.acquire())
        clock.value += 60
        self.assertIsNotNone(await pool.acquire())

    async def test_concurrency_reservation_is_atomic(self):
        pool = KeyPool("typesafe", {"keys": ["a", "b", "c"], "concurrency": 2})
        result = await asyncio.gather(*(pool.acquire() for _ in range(30)))
        self.assertEqual(sum(k is not None for k in result), 2)
        self.assertEqual(len({k.secret for k in result if k}), 2)

    async def test_auth_key_disabled_not_whole_pool(self):
        pool = KeyPool("typesafe", {"keys": ["a", "b"]})
        key = await pool.acquire()
        await pool.release(key, math.inf)
        self.assertEqual((await pool.acquire()).secret, "b")
        self.assertNotIn("secret", repr(key))

    async def test_shared_backoff(self):
        pool = KeyPool("mindshub", {"keys": ["a", "b"]})
        key = await pool.acquire()
        await pool.release(key, 60, True)
        self.assertIsNone(await pool.acquire())

    async def test_duplicate_keys_do_not_multiply_budget(self):
        pool = KeyPool("mindshub", {"keys": ["a", " a ", ""]})
        self.assertEqual(len(pool.keys), 1)


class ContractTests(unittest.TestCase):
    def test_endpoints_and_aliases(self):
        a = Adapter.create("typesafe", {})
        b = Adapter.create("mindshub", {})
        self.assertTrue(a.endpoint.endswith("/v1/systemone"))
        self.assertTrue(b.endpoint.endswith("/v1/decisions"))
        self.assertEqual(a.model, "jev-latest")
        self.assertEqual(b.model, "jev")
        self.assertEqual(set(a.encode({}, QUESTIONS)), {"model", "state", "questions"})
        self.assertEqual(a.decode(answer(), QUESTIONS), b.decode(answer(), QUESTIONS))

    def test_no_chat_parameters(self):
        body = Adapter.create("mindshub", {}).encode({}, QUESTIONS)
        self.assertFalse(
            {"messages", "temperature", "max_tokens", "stream"} & body.keys()
        )

    def test_bad_probabilities_rejected(self):
        for value in (True, None, "0.9", math.nan, math.inf, -1, 1.5):
            body = answer()
            body["answers"]["addressed"]["noul"] = value
            with self.assertRaises(DecisionError):
                Adapter.create("typesafe", {}).decode(body, QUESTIONS)

    def test_missing_answer_rejected(self):
        body = answer()
        del body["answers"]["intrusive"]
        with self.assertRaises(DecisionError):
            Adapter.create("mindshub", {}).decode(body, QUESTIONS)

    def test_gateway_503_is_not_user_auth_error(self):
        adapter = Adapter.create("mindshub", {})
        code, delay, shared = adapter.classify(
            503, {"error": {"upstream_status": 502}}, {}
        )
        self.assertEqual(code, "upstream_error")
        self.assertLess(delay, math.inf)
        self.assertTrue(shared)

    def test_billing_is_distinct(self):
        adapter = Adapter.create("mindshub", {})
        self.assertEqual(
            adapter.classify(
                429, {"error": {"code": "included_allowance_exhausted"}}, {}
            )[0],
            "funding_cooldown",
        )
        self.assertEqual(adapter.classify(402, {}, {})[0], "funding_cooldown")
        self.assertEqual(adapter.classify(429, {}, {"retry-after": "12"})[1], 12)

    def test_invalid_endpoint_rejected(self):
        for endpoint in (
            "http://api.typesafe.ai/v1/systemone",
            "https://user:pass@api.typesafe.ai/v1/systemone",
            "https://api.typesafe.ai/v1/chat/completions",
        ):
            with self.assertRaises(ValueError):
                Adapter.create("typesafe", {"endpoint": endpoint})

    def test_retry_header_dates(self):
        self.assertEqual(retry_delay("Fri, 02 Oct 2026 00:00:00 GMT", 1790899190), 10)
        self.assertEqual(retry_delay("nan"), 60)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_channels_never_mix_keys(self):
        calls = []

        async def transport(url, key, payload):
            calls.append((url, key, payload["model"]))
            return 200, {}, answer()

        client = JevClient(
            {
                "primary_channel": "typesafe",
                "allow_channel_fallback": True,
                "typesafe": {"keys": ["official1", "official2"], "quota_rpm": 2},
                "mindshub": {"keys": ["hub1", "hub2"]},
            },
            transport,
        )
        for _ in range(4):
            await client.evaluate({}, QUESTIONS)
        self.assertEqual(
            [c[1] for c in calls], ["official1", "official2", "hub1", "hub2"]
        )
        self.assertEqual(
            [c[2] for c in calls], ["jev-latest", "jev-latest", "jev", "jev"]
        )

    async def test_no_retry_on_any_post_failure(self):
        for status in (401, 403, 402, 429, 503, 529):
            calls = []

            async def transport(url, key, payload):
                calls.append(key)
                return status, {}, {}

            client = JevClient(
                {
                    "allow_channel_fallback": True,
                    "mindshub": {"keys": ["hub1", "hub2"]},
                    "typesafe": {"keys": ["official"]},
                },
                transport,
            )
            with self.assertRaises(DecisionError):
                await client.evaluate({}, QUESTIONS)
            self.assertEqual(calls, ["hub1"])
            self.assertEqual(client.pools["mindshub"].active, 0)

    async def test_cancel_releases_reservation(self):
        entered = asyncio.Event()

        async def transport(*args):
            entered.set()
            await asyncio.sleep(100)

        client = JevClient({"mindshub": {"keys": ["hub"]}}, transport)
        task = asyncio.create_task(client.evaluate({}, QUESTIONS))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client.pools["mindshub"].active, 0)
        self.assertEqual(len(client.pools["mindshub"].requests), 1)

    async def test_budget_precedes_request(self):
        async def never_send(*args):
            self.fail("Over-budget input must never reach the transport")

        client = JevClient(
            {"request_byte_budget": 8000, "mindshub": {"keys": ["hub"]}},
            transport=never_send,
        )
        try:
            with self.assertRaisesRegex(
                DecisionError, "protected_context_exceeds_safety_budget"
            ):
                await client.evaluate({"text": "汉" * 10000}, QUESTIONS)
            self.assertEqual(client.pools["mindshub"].active, 0)
            self.assertEqual(len(client.pools["mindshub"].requests), 0)
        finally:
            await client.close()
