"""The SSE subscriber must survive upstream failures, bad data and handler bugs."""
from __future__ import annotations

import asyncio

from conftest import message_event, run, wait_until
from fakes import FakeTransport
from mock_upstream import MockUpstream, serve

from relay.core import Bridge
from relay.dedup import DedupCache
from relay.upstream import Backoff, UpstreamClient, UpstreamError


def _client(url: str) -> UpstreamClient:
    return UpstreamClient(
        url,
        backoff=Backoff(initial=0.01, factor=2.0, max_delay=0.05, jitter=0.0),
        healthy_stream_after=1000.0,
    )


def test_upstream_500_does_not_kill_the_subscriber():
    async def scenario():
        mock = MockUpstream(fail_events_times=2)
        async with serve(mock) as url:
            client = _client(url)
            received: list[dict] = []

            async def handler(event):
                received.append(event)

            stop = asyncio.Event()
            task = asyncio.create_task(client.run_event_stream(handler, stop))
            try:
                await wait_until(lambda: mock.requests["/im/events"] >= 3)
                await wait_until(lambda: mock.stream_connections >= 1)
                await mock.push(message_event(guid="after-500"))
                await wait_until(lambda: any(e.get("guid") == "after-500" for e in received))
                assert not task.done(), "subscriber died after upstream 500s"
                assert mock.requests["/im/events"] >= 3
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=5)
            await client.aclose()

    run(scenario())


def test_bad_json_and_handler_errors_are_survivable():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            client = _client(url)
            received: list[dict] = []

            async def handler(event):
                if event.get("guid") == "explode":
                    raise RuntimeError("handler bug")
                received.append(event)

            stop = asyncio.Event()
            task = asyncio.create_task(client.run_event_stream(handler, stop))
            try:
                await wait_until(lambda: mock.stream_connections >= 1)
                await mock.push("this is not json")
                await mock.push(message_event(guid="explode"))
                await mock.push(message_event(guid="survivor"))
                await wait_until(lambda: any(e.get("guid") == "survivor" for e in received))
                assert not task.done()
                assert any(e.get("type") == "ready" for e in received)
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=5)
            await client.aclose()

    run(scenario())


def test_replayed_event_is_delivered_once_over_the_wire(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url, targets=("pipe@example.org",))
            transport = FakeTransport()
            upstream = _client(url)
            bridge = Bridge(cfg, transport, upstream, dedup=DedupCache(16))
            stop = asyncio.Event()
            task = asyncio.create_task(upstream.run_event_stream(bridge.on_event, stop))
            try:
                await wait_until(lambda: mock.stream_connections >= 1)
                event = message_event(guid="replayed")
                await mock.push(event)
                await mock.push(dict(event))
                await wait_until(lambda: len(transport.sent) == 1)
                await asyncio.sleep(0.05)
                assert len(transport.sent) == 1
                assert bridge.stats["deduped"] == 1
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=5)
            await upstream.aclose()

    run(scenario())


def test_send_raises_on_non_2xx():
    async def scenario():
        mock = MockUpstream(send_status=503, send_error="not registered")
        async with serve(mock) as url:
            client = UpstreamClient(url)
            try:
                await client.send("+15551234567", "hi")
            except UpstreamError as exc:
                assert "503" in str(exc)
                assert "not registered" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("send() should raise for a 503")
            await client.aclose()

    run(scenario())


def test_health_and_status_use_the_frozen_contract():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            client = UpstreamClient(url)
            assert (await client.health()) == {"ok": True, "registered": True}
            status = await client.status()
            assert status["registered"] is True
            assert status["handles"] == ["+15551234567"]
            await client.aclose()

    run(scenario())


def test_backoff_grows_caps_and_never_hits_zero():
    backoff = Backoff(initial=1.0, factor=2.0, max_delay=5.0, jitter=0.0)
    delays = [backoff.next_delay() for _ in range(6)]
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[3] == 5.0
    assert all(delay >= 0.05 for delay in delays)
    assert max(delays) == 5.0
    backoff.reset()
    assert backoff.next_delay() == 1.0


def test_backoff_jitter_stays_within_bounds():
    backoff = Backoff(initial=2.0, factor=1.0, max_delay=2.0, jitter=0.5, rand=lambda: 1.0)
    assert backoff.next_delay() == 3.0
