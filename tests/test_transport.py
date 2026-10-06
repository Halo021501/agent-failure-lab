"""Async transport mocks only. No network requests or credentials leave this process."""
import asyncio
import os
import threading
import time
import unittest
from unittest.mock import patch
from lab.common import Halt, LabError
from lab.model_config import ModelConfig
from lab.model_transport import OpenAITransport


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_transport(self, post, cancel=None, deadline=None):
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
            async def post(self, url, **kwargs): return await post(url, **kwargs)
        with patch.dict(os.environ, {"LAB_TEST_CREDENTIAL":"test-only"}), patch("httpx.AsyncClient", return_value=Client()):
            transport = OpenAITransport(ModelConfig(model="mock-model", api_key_env="LAB_TEST_CREDENTIAL", request_timeout_seconds=1))
            return await transport._post("responses", {"model":"mock-model"}, cancel or threading.Event(), deadline or time.perf_counter() + 3)

    async def test_official_endpoint_and_authorization_not_in_payload(self):
        observed = []
        async def post(url, **kwargs):
            observed.append((url, kwargs))
            class Response:
                status_code = 200
                def json(self): return {"output":[]}
            return Response()
        self.assertEqual(await self.run_transport(post), {"output":[]})
        self.assertEqual(observed[0][0], "https://api.openai.com/v1/responses")
        self.assertNotIn("test-only", str(observed[0][1]["json"]))

    async def test_in_flight_cancel_stops_task(self):
        cancel, stopped = threading.Event(), asyncio.Event()
        async def post(url, **kwargs):
            cancel.set()
            try:
                await asyncio.sleep(30)
            finally:
                stopped.set()
        with self.assertRaises(Halt) as error:
            await self.run_transport(post, cancel)
        self.assertEqual(error.exception.code, "cancelled")
        self.assertTrue(stopped.is_set())

    async def test_wall_deadline_stops_task(self):
        stopped = asyncio.Event()
        async def post(url, **kwargs):
            try:
                await asyncio.sleep(30)
            finally:
                stopped.set()
        with self.assertRaises(Halt) as error:
            await self.run_transport(post, deadline=time.perf_counter() + .05)
        self.assertEqual(error.exception.code, "budget_exceeded")
        self.assertTrue(stopped.is_set())

    async def test_http_error_has_safe_message_and_no_retry(self):
        calls = []
        async def post(url, **kwargs):
            calls.append(url)
            class Response:
                status_code = 429
            return Response()
        with self.assertRaises(LabError) as error:
            await self.run_transport(post)
        self.assertEqual(error.exception.code, "model_http_error")
        self.assertNotIn("test-only", str(error.exception))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
