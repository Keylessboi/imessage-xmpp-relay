"""HTTP client for the frozen imessage-server contract.

Talks to:

* GET  /health
* GET  /im/status
* POST /im/send
* GET  /im/events   (Server-Sent Events)

The event stream reconnects with exponential backoff plus jitter. A non-2xx
response, a dropped connection or a malformed event never terminates the
subscriber loop -- the stream is simply retried.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .logsetup import get_logger

log = get_logger("relay.upstream")

EventHandler = Callable[[dict], Awaitable[None]]


class UpstreamError(Exception):
    """A request to the imessage-server failed or returned a non-2xx status."""


class Backoff:
    """Exponential backoff with jitter, never returning zero or a negative delay."""

    def __init__(
        self,
        initial: float = 1.0,
        factor: float = 2.0,
        max_delay: float = 60.0,
        jitter: float = 0.5,
        rand: Callable[[], float] = random.random,
        minimum: float = 0.05,
    ) -> None:
        if initial <= 0:
            raise ValueError("initial backoff must be > 0")
        self.initial = initial
        self.factor = max(1.0, factor)
        self.max_delay = max(initial, max_delay)
        self.jitter = max(0.0, jitter)
        self._rand = rand
        self.minimum = minimum
        self._current = initial

    @property
    def current(self) -> float:
        return self._current

    def next_delay(self) -> float:
        base = min(self._current, self.max_delay)
        delay = base + self._rand() * self.jitter * base
        self._current = min(self._current * self.factor, self.max_delay)
        return max(self.minimum, delay)

    def reset(self) -> None:
        self._current = self.initial


class UpstreamClient:
    """Thin async wrapper around the imessage-server HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        backoff: Optional[Backoff] = None,
        healthy_stream_after: float = 30.0,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.backoff = backoff or Backoff()
        self.healthy_stream_after = healthy_stream_after
        self._session = session
        self._owns_session = session is None
        self.last_event_id: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    # -- plain requests ----------------------------------------------------

    async def _get_json(self, path: str) -> dict:
        try:
            async with self.session.get(
                self.base_url + path,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                body = await resp.text()
                if resp.status // 100 != 2:
                    raise UpstreamError(
                        "GET " + path + " -> " + str(resp.status) + ": " + body[:300]
                    )
                return json.loads(body) if body else {}
        except aiohttp.ClientError as exc:
            raise UpstreamError("GET " + path + " failed: " + str(exc)) from exc
        except ValueError as exc:
            raise UpstreamError("GET " + path + " returned invalid JSON: " + str(exc)) from exc

    async def health(self) -> dict:
        return await self._get_json("/health")

    async def status(self) -> dict:
        return await self._get_json("/im/status")

    async def send(self, to: str, text: str, reply_to: Optional[str] = None) -> dict:
        """POST /im/send. Raises UpstreamError on any non-2xx response."""
        payload = {"to": to, "text": text, "reply_to": reply_to}
        try:
            async with self.session.post(
                self.base_url + "/im/send",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                body = await resp.text()
                if resp.status // 100 != 2:
                    detail = body[:300]
                    try:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict) and parsed.get("error"):
                            detail = str(parsed["error"])
                    except ValueError:
                        pass
                    raise UpstreamError(
                        "POST /im/send -> " + str(resp.status) + ": " + detail
                    )
                try:
                    return json.loads(body) if body else {}
                except ValueError:
                    return {"ok": True, "raw": body[:200]}
        except aiohttp.ClientError as exc:
            raise UpstreamError("POST /im/send failed: " + str(exc)) from exc

    # -- event stream ------------------------------------------------------

    async def _pump(self, resp, on_event: EventHandler, stop: asyncio.Event) -> None:
        """Read SSE lines until the connection ends or the stream is stopped."""
        async for raw in resp.content:
            if stop.is_set():
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line or line.startswith(":"):
                continue
            if line.startswith("id:"):
                self.last_event_id = line[3:].strip()
                continue
            if line.startswith("event:"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                log.warning(
                    "ignoring unparseable SSE payload",
                    event="sse_bad_json",
                    payload=payload[:200],
                )
                continue
            if not isinstance(event, dict):
                log.warning("ignoring non-object SSE payload", event="sse_bad_payload")
                continue
            try:
                await on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A handler bug must not tear down the subscriber.
                log.exception("event handler failed", event="sse_handler_error")

    async def _stream_once(self, on_event: EventHandler, stop: asyncio.Event) -> None:
        """One connection attempt, abandoned as soon as stop is requested.

        The line reader runs as its own task so shutdown does not have to wait
        for the next byte from the upstream server.
        """
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if self.last_event_id:
            headers["Last-Event-ID"] = self.last_event_id
        timeout = aiohttp.ClientTimeout(
            total=None, connect=self.timeout, sock_connect=self.timeout, sock_read=None
        )
        async with self.session.get(
            self.base_url + "/im/events", headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                text = (await resp.text())[:300]
                raise UpstreamError("GET /im/events -> " + str(resp.status) + ": " + text)
            log.info("event stream connected", event="sse_connected", status=resp.status)
            pump = asyncio.create_task(self._pump(resp, on_event, stop))
            stopper = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {pump, stopper}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                pump.cancel()
                stopper.cancel()
                await asyncio.gather(pump, stopper, return_exceptions=True)
            if pump in done and not pump.cancelled():
                pump.result()

    async def run_event_stream(self, on_event: EventHandler, stop: asyncio.Event) -> None:
        """Consume /im/events forever, reconnecting with backoff until stop is set."""
        while not stop.is_set():
            started = time.monotonic()
            clean = True
            try:
                await self._stream_once(on_event, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                clean = False
                log.warning("event stream error; will reconnect", event="sse_error", error=str(exc))
            if stop.is_set():
                break
            if clean and (time.monotonic() - started) >= self.healthy_stream_after:
                self.backoff.reset()
                log.info("event stream closed after a healthy run", event="sse_closed")
            delay = self.backoff.next_delay()
            log.info("reconnecting to event stream", event="sse_reconnect", delay=round(delay, 3))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break
            except (asyncio.TimeoutError, TimeoutError):
                continue
        log.info("event stream stopped", event="sse_stopped")
