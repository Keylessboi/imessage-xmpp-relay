"""Startup, wiring and graceful shutdown (SIGTERM) of the whole service."""
from __future__ import annotations

import asyncio
import os
import signal

from conftest import message_event, run, wait_until
from fakes import FakeTransport
from mock_upstream import MockUpstream, serve

from relay.main import install_signal_handlers, run_service
from relay.upstream import UpstreamClient


def test_service_delivers_events_and_shuts_down_cleanly(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url, targets=("pipe@example.org",))
            transport = FakeTransport()
            stop = asyncio.Event()
            service = asyncio.create_task(
                run_service(cfg, transport=transport, stop=stop)
            )
            try:
                await wait_until(lambda: transport.started)
                await wait_until(lambda: mock.requests["/health"] >= 1)
                await wait_until(lambda: mock.stream_connections >= 1)
                await mock.push(message_event(guid="service-1"))
                await wait_until(lambda: len(transport.sent) == 1)
                assert transport.sent[0]["to"] == "pipe@example.org"
                stop.set()
                await asyncio.wait_for(service, timeout=5)
                assert transport.stopped, "transport.stop() was not called on shutdown"
            finally:
                if not service.done():
                    service.cancel()
                    await asyncio.gather(service, return_exceptions=True)

    run(scenario())


def test_sigterm_is_translated_into_a_shutdown_request():
    async def scenario():
        stop = asyncio.Event()
        install_signal_handlers(stop)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=2)

    run(scenario())


def test_service_survives_an_unreachable_upstream(make_config):
    async def scenario():
        # Nothing is listening on this port; health/status/stream all fail.
        cfg = make_config(api_url="http://127.0.0.1:65500")
        transport = FakeTransport()
        stop = asyncio.Event()
        service = asyncio.create_task(run_service(cfg, transport=transport, stop=stop))
        try:
            await wait_until(lambda: transport.started, timeout=5)
            await asyncio.sleep(0.1)
            assert not service.done(), "service died when the upstream was down"
            stop.set()
            await asyncio.wait_for(service, timeout=5)
        finally:
            if not service.done():
                service.cancel()
                await asyncio.gather(service, return_exceptions=True)

    run(scenario())


def test_upstream_client_closes_its_own_session():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            client = UpstreamClient(url)
            await client.health()
            session = client.session
            assert not session.closed
            await client.aclose()
            assert session.closed

    run(scenario())
