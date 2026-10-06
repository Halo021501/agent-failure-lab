"""Official endpoint only; no implicit retries, redirects, or proxy inheritance."""
import asyncio
import os
import time
from .common import Halt, LabError

VERSION = "openai-responses-http-v1"


class OpenAITransport:
    def __init__(self, config):
        config.require_ready()
        self.config = config
        self._credential = os.environ[config.api_key_env].strip()

    async def _post(self, endpoint, payload, cancel, deadline):
        import httpx
        if cancel.is_set():
            raise Halt("cancelled", "模型请求已取消")
        request_deadline = min(deadline, time.perf_counter() + self.config.request_timeout_seconds)
        if request_deadline <= time.perf_counter():
            raise Halt("budget_exceeded", "实际时间预算已耗尽")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False), trust_env=False,
                                    follow_redirects=False, timeout=self.config.request_timeout_seconds) as client:
            task = asyncio.create_task(client.post("https://api.openai.com/v1/" + endpoint, json=payload,
                                                  headers={"Authorization": "Bearer " + self._credential}))
            try:
                while not task.done():
                    if cancel.is_set():
                        raise Halt("cancelled", "已取消本地在途模型请求")
                    if time.perf_counter() >= request_deadline:
                        if time.perf_counter() >= deadline:
                            raise Halt("budget_exceeded", "实际时间预算已耗尽")
                        raise LabError("model_timeout", "模型请求超过单次时限")
                    await asyncio.wait({task}, timeout=min(.05, max(.001, request_deadline - time.perf_counter())))
                response = task.result()
                if not 200 <= response.status_code < 300:
                    raise LabError("model_http_error", f"OpenAI HTTP {response.status_code}；未自动重试")
                try:
                    return response.json()
                except ValueError as error:
                    raise LabError("model_protocol_error", "OpenAI 返回非 JSON 响应") from error
            except httpx.TimeoutException as error:
                raise LabError("model_timeout", "模型请求超过单次时限") from error
            except httpx.HTTPError as error:
                raise LabError("model_network_error", "模型网络请求失败；未自动重试") from error
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def count(self, payload, cancel, deadline):
        body = {key: payload[key] for key in ("model", "input", "instructions", "tools")}
        return self._run("responses/input_tokens", body, cancel, deadline)

    def respond(self, payload, cancel, deadline):
        return self._run("responses", payload, cancel, deadline)

    def _run(self, endpoint, payload, cancel, deadline):
        try:
            return asyncio.run(self._post(endpoint, payload, cancel, deadline))
        except LabError:
            raise
        except Exception as error:
            raise LabError("model_transport_error", "模型传输异常；未自动重试") from error
