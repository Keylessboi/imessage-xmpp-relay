"""XMPP -> iMessage: an inbound stanza becomes POST /im/send."""
from __future__ import annotations

import logging

from conftest import message_event, run
from fakes import FakeTransport
from mock_upstream import MockUpstream, serve

from relay.core import Bridge
from relay.models import IncomingXmpp
from relay.upstream import UpstreamClient


def _wire(cfg, url, transport=None):
    transport = transport or FakeTransport()
    upstream = UpstreamClient(url)
    return Bridge(cfg, transport, upstream), transport, upstream


def test_inbound_message_posts_to_im_send(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url)
            bridge, transport, upstream = _wire(cfg, url)
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid="alice@example.org/phone", body="+15551234567: hello there")
            )
            assert mock.sent == [{"to": "+15551234567", "text": "hello there", "reply_to": None}]
            assert bridge.stats["sent"] == 1
            assert transport.sent == []  # no error notice
            await upstream.aclose()

    run(scenario())


def test_contact_lookup_and_reply_threading(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(
                api_url=url,
                contacts={"+15551234567": "alice@example.org"},
            )
            bridge, transport, upstream = _wire(cfg, url)
            # First an inbound iMessage from that contact ...
            await bridge.on_event(message_event(guid="guid-reply", text="are you there?"))
            assert transport.sent[0]["to"] == "alice@example.org"
            # ... then the XMPP reply, which must reuse the guid as reply_to.
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid="alice@example.org/laptop", body="yes I am")
            )
            assert mock.sent == [
                {"to": "+15551234567", "text": "yes I am", "reply_to": "guid-reply"}
            ]
            await upstream.aclose()

    run(scenario())


def test_group_nick_is_used_as_the_destination(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(
                api_url=url,
                allowed=frozenset({"imessage@conference.example.org"}),
            )
            bridge, transport, upstream = _wire(cfg, url)
            await bridge.on_xmpp_message(
                IncomingXmpp(
                    from_jid="imessage@conference.example.org/+15551234567",
                    body="hi from the room",
                    mtype="groupchat",
                    muc_room="imessage@conference.example.org",
                )
            )
            assert mock.sent[0]["to"] == "+15551234567"
            assert mock.sent[0]["text"] == "hi from the room"
            await upstream.aclose()

    run(scenario())


def test_default_to_is_the_last_resort(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url, default_to="+15550001111")
            bridge, transport, upstream = _wire(cfg, url)
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid="alice@example.org/phone", body="plain body")
            )
            assert mock.sent[0]["to"] == "+15550001111"
            assert mock.sent[0]["text"] == "plain body"
            await upstream.aclose()

    run(scenario())


def test_unknown_sender_is_denied(make_config, caplog):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url)
            bridge, transport, upstream = _wire(cfg, url)
            with caplog.at_level(logging.WARNING, logger="relay.core"):
                await bridge.on_xmpp_message(
                    IncomingXmpp(from_jid="stranger@example.org/x", body="+15551234567: hi")
                )
            assert mock.sent == []
            assert bridge.stats["denied"] == 1
            await upstream.aclose()

    run(scenario())
    assert any("DENYING" in record.message for record in caplog.records)


def test_empty_allowlist_denies_everyone(make_config, caplog):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url, allowed=frozenset())
            bridge, transport, upstream = _wire(cfg, url)
            with caplog.at_level(logging.WARNING, logger="relay.core"):
                await bridge.on_xmpp_message(
                    IncomingXmpp(from_jid="alice@example.org/phone", body="+15551234567: hi")
                )
            assert mock.sent == []
            await upstream.aclose()

    run(scenario())
    assert any("empty XMPP_ALLOWED denies everyone" in record.getMessage() for record in caplog.records)


def test_upstream_error_notifies_sender(make_config):
    async def scenario():
        mock = MockUpstream(send_status=500, send_error="not registered")
        async with serve(mock) as url:
            cfg = make_config(api_url=url)
            bridge, transport, upstream = _wire(cfg, url)
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid="alice@example.org/phone", body="+15551234567: hi")
            )
            assert bridge.stats["send_errors"] == 1
            assert len(transport.sent) == 1
            assert "iMessage send failed" in transport.sent[0]["body"]
            await upstream.aclose()

    run(scenario())


def test_unresolvable_target_replies_with_a_hint(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url)
            bridge, transport, upstream = _wire(cfg, url)
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid="alice@example.org/phone", body="no handle here")
            )
            assert mock.sent == []
            assert bridge.stats["no_target"] == 1
            assert "No iMessage destination known" in transport.sent[0]["body"]
            await upstream.aclose()

    run(scenario())


def test_empty_and_error_messages_are_ignored(make_config):
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            cfg = make_config(api_url=url)
            bridge, transport, upstream = _wire(cfg, url)
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid="alice@example.org/phone", body="   ")
            )
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid="alice@example.org/phone", body="+15551234567: hi", mtype="error")
            )
            assert mock.sent == []
            await upstream.aclose()

    run(scenario())
