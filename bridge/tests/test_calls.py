"""Call-to-FaceTime: XEP-0353 / Jingle decline, disco, text command, link post.

Inbound stanzas are real slixmpp stanzas dispatched through a real
slixmpp.ComponentXMPP (no socket); the outgoing raw XML is captured from its
send_raw. The imessage-server is a small fake upstream.
"""
from __future__ import annotations

import asyncio
from xml.etree import ElementTree as ET

import pytest
import slixmpp

from conftest import run, wait_until
from relay.calls import (
    CallService,
    RateLimiter,
    build_reject,
    is_command,
    jingle_iq_info,
    jingle_message_kind,
)
from relay.component import ComponentTransport
from relay.config import Config
from relay.upstream import UpstreamClient, UpstreamError
from mock_upstream import MockUpstream, serve

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"
FULL = OWNER + "/phone"
PEER = "+15557654321"
PEER_JID = PEER + "@" + DOMAIN
URL = "https://facetime.apple.com/join#v=1&p=SECRET&k=KEY"
NS = "jabber:component:accept"


def cfg() -> Config:
    return Config(
        mode="component", component_jid=DOMAIN, component_domain=DOMAIN,
        component_host="127.0.0.1", component_port=5347, component_secret="s",
        component_owner=OWNER, targets=(OWNER,), allowed=frozenset({OWNER}),
    )


class FakeUpstream:
    def __init__(self, *, rang=True, note=None, error=None):
        self.calls: list[tuple] = []
        self.rang, self.note, self.error = rang, note, error

    async def facetime_call(self, to, video=True):
        self.calls.append((to, video))
        if self.error:
            raise UpstreamError(self.error)
        return URL, self.rang, self.note

    async def facetime_link(self):
        self.calls.append(("link",))
        if self.error:
            raise UpstreamError(self.error)
        return URL


class Rig:
    def __init__(self, upstream=None, limiter=None):
        self.xmpp = slixmpp.ComponentXMPP(DOMAIN, "s", "127.0.0.1", 5347)
        self.raw: list[ET.Element] = []
        self.xmpp.send_raw = lambda data: self.raw.append(
            ET.fromstring("<root xmlns='%s'>%s</root>" % (NS, data))[0]
        )
        self.posted: list[dict] = []
        self.transport = ComponentTransport(cfg(), xmpp=self.xmpp)
        self.upstream = upstream or FakeUpstream()

        async def send_message(**kw):
            self.posted.append(kw)

        self.transport.attach_calls(
            CallService(cfg(), self.upstream, send_message, self.transport.send_raw, limiter)
        )

    def feed(self, xml: str) -> None:
        self.xmpp._spawn_event(ET.fromstring(xml))


def propose(sid="abc", media="video", frm=FULL, to=PEER_JID, body="") -> str:
    return (
        "<message xmlns='%s' type='chat' from='%s' to='%s' id='jm-propose-%s'>"
        "<propose xmlns='urn:xmpp:jingle-message:0' id='%s'>"
        "<description xmlns='urn:xmpp:jingle:apps:rtp:1' media='%s'/></propose>"
        "<request xmlns='urn:xmpp:receipts'/><store xmlns='urn:xmpp:hints'/>%s</message>"
        % (NS, frm, to, sid, sid, media, body)
    )


def jingle_iq(action="session-initiate", sid="s1", frm=FULL, to=PEER_JID, iq_id="i1") -> str:
    return (
        "<iq xmlns='%s' type='set' from='%s' to='%s' id='%s'>"
        "<jingle xmlns='urn:xmpp:jingle:1' action='%s' sid='%s' initiator='%s'>"
        "<content creator='initiator' name='v'>"
        "<description xmlns='urn:xmpp:jingle:apps:rtp:1' media='video'/></content>"
        "</jingle></iq>" % (NS, frm, to, iq_id, action, sid, frm)
    )


def tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1]


def test_pure_parsers_and_command():
    el = ET.fromstring(propose())
    assert jingle_message_kind(el) == ("propose", "abc", ["video"])
    assert jingle_message_kind(ET.fromstring("<message xmlns='%s'/>" % NS)) is None
    assert jingle_iq_info(ET.fromstring(jingle_iq())) == ("session-initiate", "s1")
    assert is_command(" FaceTime ") and is_command("/facetime")
    assert not is_command("facetime me later")
    lim = RateLimiter(2, 10, clock=iter([0, 1, 2, 11]).__next__)
    assert [lim.allow() for _ in range(4)] == [True, True, False, True]


def test_propose_rejected_from_called_jid_to_full_jid_then_link_posted():
    async def scenario():
        rig = Rig()
        rig.feed(propose())
        await wait_until(lambda: rig.posted and rig.raw)
        reject = rig.raw[0]
        assert tag(reject) == "message"
        assert reject.get("from") == PEER_JID and reject.get("to") == FULL
        assert reject.get("type") == "chat"
        r = reject.find("{urn:xmpp:jingle-message:0}reject")
        assert r is not None and r.get("id") == "abc"
        assert reject.find("{urn:xmpp:jingle-message:0}proceed") is None
        assert reject.find("{urn:xmpp:hints}store") is not None
        assert rig.upstream.calls == [(PEER, True)]
        post = rig.posted[0]
        assert post["to"] == OWNER and post["frm"] == PEER_JID
        assert post["body"].startswith("FaceTime: " + URL)
        assert "Ringing the contact" in post["body"]

    run(scenario())


def test_audio_propose_asks_for_audio_call_and_retry_is_deduped():
    async def scenario():
        rig = Rig()
        rig.feed(propose(media="audio"))
        await wait_until(lambda: rig.posted)
        rig.feed(propose(media="audio"))  # client retry, same session id
        await wait_until(lambda: len(rig.raw) == 2)
        await asyncio.sleep(0.05)
        assert rig.upstream.calls == [(PEER, False)]
        assert len(rig.posted) == 1

    run(scenario())


def test_not_rung_fallback_is_stated():
    async def scenario():
        rig = Rig(FakeUpstream(rang=False, note="the contact was NOT rung: boom"))
        rig.feed(propose())
        await wait_until(lambda: rig.posted)
        body = rig.posted[0]["body"]
        assert body.startswith("FaceTime: " + URL)
        assert "NOT rung" in body and "Ringing the contact now" not in body

    run(scenario())


def test_server_error_is_readable_and_has_no_link():
    async def scenario():
        rig = Rig(FakeUpstream(error="POST /im/facetime-call -> 503: not registered"))
        rig.feed(propose())
        await wait_until(lambda: rig.posted)
        body = rig.posted[0]["body"]
        assert body.startswith("FaceTime unavailable:") and "not registered" in body
        assert "facetime.apple.com" not in body

    run(scenario())


def test_non_owner_propose_is_denied_and_nothing_happens():
    async def scenario():
        rig = Rig()
        rig.feed(propose(frm="mallory@evil.example/x"))
        await asyncio.sleep(0.1)
        assert rig.raw == [] and rig.posted == [] and rig.upstream.calls == []

    run(scenario())


def test_propose_to_unknown_jid_is_ignored():
    async def scenario():
        rig = Rig()
        rig.feed(propose(to="nobody@imessage.example.org"))
        await asyncio.sleep(0.1)
        assert rig.raw == [] and rig.upstream.calls == []

    run(scenario())


def test_other_jingle_message_kinds_are_ignored():
    async def scenario():
        rig = Rig()
        for kind in ("retract", "proceed", "ringing", "reject"):
            rig.feed(
                "<message xmlns='%s' type='chat' from='%s' to='%s'>"
                "<%s xmlns='urn:xmpp:jingle-message:0' id='z'/></message>"
                % (NS, FULL, PEER_JID, kind)
            )
        await asyncio.sleep(0.1)
        assert rig.raw == [] and rig.upstream.calls == []

    run(scenario())


def test_session_initiate_gets_result_then_terminate_decline_then_link():
    async def scenario():
        rig = Rig()
        rig.feed(jingle_iq())
        await wait_until(lambda: len(rig.raw) == 2 and rig.posted)
        result, term = rig.raw
        assert tag(result) == "iq" and result.get("type") == "result"
        assert result.get("id") == "i1" and result.get("to") == FULL
        assert term.get("type") == "set" and term.get("to") == FULL
        assert term.get("from") == PEER_JID
        j = term.find("{urn:xmpp:jingle:1}jingle")
        assert j.get("action") == "session-terminate" and j.get("sid") == "s1"
        assert j.find("{urn:xmpp:jingle:1}reason/{urn:xmpp:jingle:1}decline") is not None
        assert rig.posted[0]["body"].startswith("FaceTime: ")

    run(scenario())


def test_session_initiate_after_propose_with_same_sid_does_not_double_post():
    async def scenario():
        rig = Rig()
        rig.feed(propose(sid="same"))
        await wait_until(lambda: rig.posted)
        rig.feed(jingle_iq(sid="same"))
        await wait_until(lambda: len(rig.raw) == 3)
        await asyncio.sleep(0.05)
        assert len(rig.posted) == 1

    run(scenario())


def test_non_initiate_jingle_action_is_just_acknowledged():
    async def scenario():
        rig = Rig()
        rig.feed(jingle_iq(action="session-terminate"))
        await wait_until(lambda: rig.raw)
        await asyncio.sleep(0.05)
        assert len(rig.raw) == 1 and rig.raw[0].get("type") == "result"
        assert rig.posted == []

    run(scenario())


def test_text_command_to_contact_posts_link_and_is_not_forwarded():
    async def scenario():
        rig = Rig()
        forwarded = []

        async def handler(msg):
            forwarded.append(msg)

        rig.transport.set_incoming_handler(handler)
        rig.feed(
            "<message xmlns='%s' type='chat' from='%s' to='%s'><body>facetime</body></message>"
            % (NS, FULL, PEER_JID)
        )
        await wait_until(lambda: rig.posted)
        assert rig.upstream.calls == [(PEER, True)]
        assert rig.posted[0]["frm"] == PEER_JID
        assert forwarded == []

    run(scenario())


def test_text_command_to_the_bridge_jid_gives_plain_link_not_rung():
    async def scenario():
        rig = Rig()
        rig.feed(
            "<message xmlns='%s' type='chat' from='%s' to='%s'><body>/facetime</body></message>"
            % (NS, FULL, DOMAIN)
        )
        await wait_until(lambda: rig.posted)
        assert rig.upstream.calls == [("link",)]
        assert rig.posted[0]["frm"] == DOMAIN
        assert "NOT rung" in rig.posted[0]["body"]

    run(scenario())


def test_ordinary_text_still_forwarded():
    async def scenario():
        rig = Rig()
        got = []

        async def handler(msg):
            got.append(msg.body)

        rig.transport.set_incoming_handler(handler)
        rig.feed(
            "<message xmlns='%s' type='chat' from='%s' to='%s'><body>hello</body></message>"
            % (NS, FULL, PEER_JID)
        )
        await wait_until(lambda: got)
        assert got == ["hello"] and rig.upstream.calls == []

    run(scenario())


def test_rate_limit_stops_the_third_request_in_the_window():
    async def scenario():
        rig = Rig(limiter=RateLimiter(2, 3600))
        for i in range(3):
            rig.feed(propose(sid="p%d" % i))
        await wait_until(lambda: len(rig.posted) == 3)
        assert len(rig.upstream.calls) == 2
        assert "too many requests" in rig.posted[2]["body"]
        assert "facetime.apple.com" not in rig.posted[2]["body"]

    run(scenario())


def test_disco_info_advertises_jingle_for_contact_and_component():
    async def scenario():
        rig = Rig()
        for target, ctype in ((PEER_JID, "registered"), (DOMAIN, "imessage")):
            rig.raw.clear()
            rig.feed(
                "<iq xmlns='%s' type='get' from='%s' to='%s' id='d1'>"
                "<query xmlns='http://jabber.org/protocol/disco#info'/></iq>" % (NS, FULL, target)
            )
            await wait_until(lambda: rig.raw)
            iq = rig.raw[0]
            assert iq.get("type") == "result" and iq.get("from") == target and iq.get("id") == "d1"
            q = iq.find("{http://jabber.org/protocol/disco#info}query")
            feats = {f.get("var") for f in q.findall("{http://jabber.org/protocol/disco#info}feature")}
            assert {"urn:xmpp:jingle-message:0", "urn:xmpp:jingle:1",
                    "urn:xmpp:jingle:apps:rtp:1", "urn:xmpp:jingle:apps:rtp:video",
                    "urn:xmpp:jingle:apps:rtp:audio"} <= feats
            ident = q.find("{http://jabber.org/protocol/disco#info}identity")
            assert ident.get("type") == ctype

    run(scenario())


def test_disco_from_non_owner_is_not_answered():
    async def scenario():
        rig = Rig()
        rig.feed(
            "<iq xmlns='%s' type='get' from='mallory@evil.example' to='%s' id='d2'>"
            "<query xmlns='http://jabber.org/protocol/disco#info'/></iq>" % (NS, PEER_JID)
        )
        await asyncio.sleep(0.1)
        assert rig.raw == []

    run(scenario())


def test_build_reject_escapes_attributes():
    xml = build_reject(frm=PEER_JID, to=FULL, sid="a'b\"<c>")
    el = ET.fromstring("<root xmlns='%s'>%s</root>" % (NS, xml))[0]
    assert el.find("{urn:xmpp:jingle-message:0}reject").get("id") == "a'b\"<c>"


def test_link_never_appears_in_logs(caplog):
    import logging

    async def scenario():
        rig = Rig()
        rig.feed(propose())
        await wait_until(lambda: rig.posted)

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert "SECRET" not in caplog.text and "facetime.apple.com" not in caplog.text


# -- upstream client against the mock server ------------------------------

def test_upstream_facetime_call_and_fallbacks():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as base:
            client = UpstreamClient(base)
            try:
                url, rang, note = await client.facetime_call("+15557654321", True)
                assert (url, rang, note) == (URL, True, None)
                assert mock.facetime_calls == [{"to": "+15557654321", "video": True}]
                mock.call_status = 404  # old server without the endpoint
                url, rang, note = await client.facetime_call("+15557654321")
                assert url == URL and rang is False and note
                mock.call_status = 503
                with pytest.raises(UpstreamError) as err:
                    await client.facetime_call("+15557654321")
                assert "not registered" in str(err.value)
                assert URL not in str(err.value)
            finally:
                await client.aclose()

    run(scenario())
