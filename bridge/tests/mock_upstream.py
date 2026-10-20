"""A small aiohttp server implementing the frozen imessage-server contract."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from aiohttp import web


class MockUpstream:
    def __init__(
        self,
        *,
        fail_events_times: int = 0,
        send_status: int = 200,
        send_error: str = "boom",
    ) -> None:
        self.fail_events_times = fail_events_times
        self.send_status = send_status
        self.send_error = send_error
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.requests: Counter = Counter()
        self.health_payload: dict = {"ok": True, "registered": True}
        self.status_payload: dict = {
            "registered": True,
            "handles": ["+15551234567"],
            "apple_id": "me@example.org",
        }
        self._connections = 0
        self.facetime_calls: list[dict] = []
        self.call_status = 200

    @property
    def stream_connections(self) -> int:
        """How many times a stream was actually established (2xx)."""
        return self._connections

    async def push(self, item: Any) -> None:
        """Queue an event dict, or a raw string to be written as one data: line."""
        await self.queue.put(item)

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/im/status", self._status)
        app.router.add_post("/im/send", self._send)
        app.router.add_get("/im/events", self._events)
        app.router.add_post("/im/facetime-link", self._facetime_link)
        app.router.add_post("/im/facetime-call", self._facetime_call)
        return app

    async def _health(self, request: web.Request) -> web.Response:
        self.requests["/health"] += 1
        return web.json_response(self.health_payload)

    async def _status(self, request: web.Request) -> web.Response:
        self.requests["/im/status"] += 1
        return web.json_response(self.status_payload)

    async def _send(self, request: web.Request) -> web.Response:
        self.requests["/im/send"] += 1
        payload = await request.json()
        self.sent.append(payload)
        if self.send_status != 200:
            return web.json_response({"error": self.send_error}, status=self.send_status)
        return web.json_response({"ok": True, "guid": "sent-guid-" + str(len(self.sent))})

    FACETIME_URL = "https://facetime.apple.com/join#v=1&p=SECRET&k=KEY"

    async def _facetime_link(self, request: web.Request) -> web.Response:
        self.requests["/im/facetime-link"] += 1
        return web.json_response({"ok": True, "url": self.FACETIME_URL})

    async def _facetime_call(self, request: web.Request) -> web.Response:
        self.requests["/im/facetime-call"] += 1
        if self.call_status != 200:
            if self.call_status == 404:
                return web.Response(status=404, text="not found")
            return web.json_response({"error": "not registered"}, status=self.call_status)
        self.facetime_calls.append(await request.json())
        return web.json_response(
            {"ok": True, "url": self.FACETIME_URL, "rang": True, "note": None}
        )

    async def _events(self, request: web.Request) -> web.StreamResponse:
        self.requests["/im/events"] += 1
        if self.fail_events_times > 0:
            self.fail_events_times -= 1
            return web.Response(status=500, text="upstream exploded")
        self._connections += 1
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"type":"ready"}\n\n')
        try:
            while True:
                # Poll instead of blocking forever on the queue: a client that
                # goes away must let this handler finish, or AppRunner.cleanup()
                # waits out its whole shutdown timeout.
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=0.05)
                except (asyncio.TimeoutError, TimeoutError):
                    if request.transport is None or request.transport.is_closing():
                        break
                    continue
                if item is None:
                    break
                if isinstance(item, str):
                    payload = item
                else:
                    payload = json.dumps(item)
                await resp.write(("data: " + payload + "\n\n").encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:
            pass
        try:
            await resp.write_eof()
        except Exception:
            pass
        return resp


@asynccontextmanager
async def serve(mock: MockUpstream) -> AsyncIterator[str]:
    """Run the mock on a random localhost port and yield its base URL."""
    runner = web.AppRunner(mock.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield "http://127.0.0.1:" + str(port)
    finally:
        await site.stop()
        await runner.cleanup()
