"""Outbound attachments (owner posts a URL, XEP-0066): SSRF rules, caps, and the bridge flow.

Everything is local: an aiohttp test server stands in for both the file host
and the imessage-server. Nothing reaches the real network or Apple.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import aiohttp
import pytest
from aiohttp import web
from conftest import run
from fakes import FakeComponentTransport, FakeStanza, FakeComponentXmpp
from mock_upstream import MockUpstream, serve

from relay import outbound, xep
from relay.component import ComponentTransport
from relay.core import Bridge
from relay.models import IncomingXmpp
from relay.outbound import FetchError, PublicOnlyResolver, check_url, fetch_to_tempfile, guess_name, is_public_ip
from relay.upstream import UpstreamClient

from test_component import OWNER, PEER, PEER_JID, component_config, component_transport, stanza


def test_public_ip_classification():
    for ok in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]:
        assert is_public_ip(ok), ok
    for bad in ["127.0.0.1", "10.43.0.1", "172.16.5.4", "192.168.1.67", "169.254.169.254", "100.64.0.1",
                "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "nope"]:
        assert not is_public_ip(bad), bad


def test_check_url_rules():
    assert check_url("https://example.org/a.png")[1] == "example.org"
    assert check_url("http://example.org:80/a")[1] == "example.org"
    for bad in ["file:///etc/passwd", "ftp://example.org/x", "gopher://x/", "http://", "https://user:pw@example.org/x",
                "http://example.org:8080/x", "https://example.org:22/", "http://127.0.0.1/x", "http://[::1]/x",
                "http://192.168.1.1/x", "http://169.254.169.254/latest", "http://10.0.0.5:80/x", "not a url", "http://[bad/"]:
        with pytest.raises(FetchError):
            check_url(bad)


def test_guess_name():
    assert guess_name("https://x.org/a/b/cat%20pic.jpg?x=1") == "cat pic.jpg"
    assert guess_name("https://x.org/") == "file"


def test_resolver_refuses_non_public_answers():
    async def scenario():
        r = PublicOnlyResolver()
        with pytest.raises(OSError):
            await r.resolve("localhost", 80)
        with pytest.raises(OSError):
            await r.resolve("127.0.0.1", 80)

    run(scenario())


class FileHost:
    """Local file server; the SSRF checks are bypassed for it in tests."""

    def __init__(self):
        app = web.Application()
        app.router.add_get("/ok.png", self.ok)
        app.router.add_get("/big", self.big)
        app.router.add_get("/stream-big", self.stream_big)
        app.router.add_get("/redir", self.redir)
        app.router.add_get("/empty", self.empty)
        self.app = app

    async def ok(self, request):
        return web.Response(body=b"\x89PNG\r\n\x1a\nDATA", content_type="image/png")

    async def big(self, request):
        return web.Response(body=b"x" * 5000)

    async def stream_big(self, request):
        resp = web.StreamResponse()  # no Content-Length: the cap must still hold
        await resp.prepare(request)
        for _ in range(10):
            await resp.write(b"y" * 1000)
        return resp

    async def redir(self, request):
        raise web.HTTPFound("http://169.254.169.254/latest/meta-data")

    async def empty(self, request):
        return web.Response(body=b"")


async def with_host(fn):
    runner = web.AppRunner(FileHost().app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = "http://127.0.0.1:%d" % runner.addresses[0][1]
    try:
        await fn(base)
    finally:
        await runner.cleanup()


@pytest.fixture
def allow_local(monkeypatch):
    # The file host is on loopback and a random port; production rules would refuse it.
    monkeypatch.setattr(outbound, "check_url", lambda url: (url, "127.0.0.1"))


def test_fetch_streams_to_a_tempfile(allow_local):
    async def scenario(base):
        async with aiohttp.ClientSession() as session:
            path, ctype, size = await fetch_to_tempfile(base + "/ok.png", max_bytes=1000, timeout=5, session=session)
        try:
            assert (ctype, size) == ("image/png", 12)
            assert open(path, "rb").read().startswith(b"\x89PNG")
        finally:
            os.unlink(path)

    run(with_host(scenario))


def test_fetch_enforces_the_cap_with_and_without_content_length(allow_local):
    async def scenario(base):
        before = set(os.listdir(os.environ.get("TMPDIR", "/tmp")))
        async with aiohttp.ClientSession() as session:
            for route in ("/big", "/stream-big"):
                with pytest.raises(FetchError, match="larger"):
                    await fetch_to_tempfile(base + route, max_bytes=2000, timeout=5, session=session)
            with pytest.raises(FetchError, match="empty"):
                await fetch_to_tempfile(base + "/empty", max_bytes=2000, timeout=5, session=session)
            with pytest.raises(FetchError, match="404"):
                await fetch_to_tempfile(base + "/missing", max_bytes=2000, timeout=5, session=session)
        leaked = [f for f in set(os.listdir(os.environ.get("TMPDIR", "/tmp"))) - before if f.startswith("relay-out-")]
        assert leaked == []

    run(with_host(scenario))


def test_local_host_is_refused_with_the_real_checks():
    async def scenario(base):
        with pytest.raises(FetchError):
            await fetch_to_tempfile(base + "/ok.png", max_bytes=1000, timeout=5)

    run(with_host(scenario))


def test_a_redirect_to_a_private_literal_is_refused_before_it_is_requested(monkeypatch):
    # Only the first hop (the test host) is waved through; the redirect target
    # goes through the real check and must be refused without any request.
    real = outbound.check_url
    monkeypatch.setattr(outbound, "check_url", lambda u: (u, "h") if u.startswith("http://127.0.0.1:") else real(u))

    async def scenario(base):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(FetchError, match="publicly routable"):
                await fetch_to_tempfile(base + "/redir", max_bytes=1000, timeout=5, session=session)

    run(with_host(scenario))


# -- the bridge flow ------------------------------------------------------------


def test_component_parses_an_oob_url_and_forwards_it():
    async def scenario():
        transport, fake, cfg = component_transport()
        seen = []

        async def handler(msg):
            seen.append(msg)

        transport.set_incoming_handler(handler)
        s = FakeStanza(type="chat", body="https://files.example.org/a.png", to=PEER_JID, **{"from": OWNER},
                       xml=ET.fromstring(
                           "<message xmlns='jabber:client'><body>https://files.example.org/a.png</body>"
                           "<x xmlns='jabber:x:oob'><url>https://files.example.org/a.png</url></x></message>"))
        await transport._on_message(s)
        assert seen and seen[0].oob_url == "https://files.example.org/a.png"
        assert seen[0].has_features

    run(scenario())


def test_owner_oob_url_is_fetched_and_uploaded(monkeypatch, tmp_path):
    async def scenario():
        f = tmp_path / "f.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\nDATA")

        async def fake_fetch(url, **kw):
            assert kw["max_bytes"] == 100 * 1024 * 1024
            p = tmp_path / "tmp-out"
            p.write_bytes(f.read_bytes())
            return str(p), "image/png", 12

        monkeypatch.setattr("relay.core.fetch_to_tempfile", fake_fetch)
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = component_config(api_url=url)
            bridge = Bridge(cfg, FakeComponentTransport(), UpstreamClient(url))
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid=OWNER + "/phone", body="https://files.example.org/cat.png",
                             to_jid=PEER_JID, oob_url="https://files.example.org/cat.png")
            )
            assert mock.attachments[0]["query"] == {"to": PEER, "name": "cat.png", "mime": "image/png"}
            assert mock.attachments[0]["body"].startswith(b"\x89PNG")
            assert mock.sent == []  # body == url: no caption text
            assert not (tmp_path / "tmp-out").exists(), "temp file removed"
            assert bridge.stats["attachments_sent"] == 1
            await bridge.upstream.aclose()

    run(scenario())


def test_caption_is_sent_as_text_and_refusals_are_reported(monkeypatch, tmp_path):
    async def scenario():
        async def fake_fetch(url, **kw):
            if "bad" in url:
                raise FetchError("that address is not publicly routable")
            p = tmp_path / "t2"
            p.write_bytes(b"abc")
            return str(p), "", 3

        monkeypatch.setattr("relay.core.fetch_to_tempfile", fake_fetch)
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = component_config(api_url=url)
            transport = FakeComponentTransport()
            bridge = Bridge(cfg, transport, UpstreamClient(url))
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid=OWNER, body="look at this", to_jid=PEER_JID, oob_url="https://f.example.org/x.bin"))
            assert mock.sent[0]["text"] == "look at this"
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid=OWNER, body="http://bad/x", to_jid=PEER_JID, oob_url="http://bad/x"))
            assert len(mock.attachments) == 1
            assert "Not sent: that address is not publicly routable" in transport.bodies()[-1]
            mock.attachment_status = 502
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid=OWNER, body="https://f.example.org/y.bin", to_jid=PEER_JID, oob_url="https://f.example.org/y.bin"))
            assert "iMessage attachment failed" in transport.bodies()[-1]
            await bridge.upstream.aclose()

    run(scenario())


def test_non_owner_oob_is_denied(monkeypatch):
    async def scenario():
        called = []

        async def fake_fetch(url, **kw):
            called.append(url)
            raise FetchError("x")

        monkeypatch.setattr("relay.core.fetch_to_tempfile", fake_fetch)
        cfg = component_config()
        bridge = Bridge(cfg, FakeComponentTransport(), UpstreamClient(cfg.api_url))
        await bridge.on_xmpp_message(
            IncomingXmpp(from_jid="mallory@evil.org", body="http://x", to_jid=PEER_JID, oob_url="http://x"))
        assert called == []
        await bridge.upstream.aclose()

    run(scenario())
