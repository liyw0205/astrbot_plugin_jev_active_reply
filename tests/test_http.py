import unittest
from dataclasses import replace

from aiohttp import web

from ..client import DecisionError, JevClient
from ..policy import QUESTIONS
from .test_client import answer


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.behavior = "ok"

        async def handler(request):
            payload = await request.json()
            self.calls.append(
                (request.path, request.headers.get("Authorization"), payload)
            )
            if self.behavior == "rate":
                return web.json_response(
                    {"error": {"code": "rate_limited"}},
                    status=429,
                    headers={"Retry-After": "9"},
                )
            if self.behavior == "redirect":
                raise web.HTTPFound("/should-not-follow")
            if self.behavior == "invalid":
                return web.Response(text="<html>error</html>")
            return web.json_response(answer())

        app = web.Application()
        app.router.add_post("/{path:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.client = JevClient(
            {
                "primary_channel": "typesafe",
                "allow_channel_fallback": True,
                "typesafe": {"keys": ["official-a", "official-b"], "quota_rpm": 2},
                "nanbei": {"keys": ["hub-a", "hub-b"]},
            }
        )
        # Test-only injection; production configuration rejects plain HTTP.
        for name, pool in self.client.pools.items():
            suffix = "/v1/systemone"
            pool.adapter = replace(
                pool.adapter, endpoint=f"http://127.0.0.1:{self.port}{suffix}"
            )

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_both_wire_protocols_and_key_rotation(self):
        for _ in range(4):
            result = await self.client.evaluate({"text": "中文测试"}, QUESTIONS)
            self.assertEqual(result["model"], "jev-1.13.0")
        self.assertEqual([p for p, _, _ in self.calls], ["/v1/systemone"] * 4)
        self.assertEqual(
            [k for _, k, _ in self.calls],
            ["Bearer official-a", "Bearer official-b", "Bearer hub-a", "Bearer hub-b"],
        )
        self.assertEqual(self.calls[0][2]["state"]["text"], "中文测试")

    async def test_429_honors_delay_without_replay(self):
        self.behavior = "rate"
        with self.assertRaises(DecisionError):
            await self.client.evaluate({}, QUESTIONS)
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(
            self.client.pools["typesafe"].status()["cooldown_seconds"], 0
        )

    async def test_redirect_does_not_leak_bearer(self):
        self.behavior = "redirect"
        with self.assertRaises(DecisionError):
            await self.client.evaluate({}, QUESTIONS)
        self.assertEqual(len(self.calls), 1)

    async def test_non_json_success_rejected(self):
        self.behavior = "invalid"
        with self.assertRaises(DecisionError):
            await self.client.evaluate({}, QUESTIONS)
