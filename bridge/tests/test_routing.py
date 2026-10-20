"""iMessage -> XMPP: an SSE message event becomes an XMPP stanza."""
from __future__ import annotations

import logging

from conftest import message_event, run
from fakes import FakeTransport

from relay.core import Bridge
from relay.upstream import UpstreamClient


def _bridge(cfg, transport=None):
    transport = transport or FakeTransport()
    bridge = Bridge(cfg, transport, UpstreamClient(cfg.api_url))
    return bridge, transport


def test_one_to_one_event_becomes_pipe_stanza(make_config):
    cfg = make_config(targets=("pipe@example.org",))
    async def scenario():
        bridge, transport = _bridge(cfg)
        await bridge.on_event(message_event())
        assert transport.sent == [
            {
                "to": "pipe@example.org",
                "body": "Alice (+15551234567): hello from iMessage",
                "mtype": "chat",
                "muc_nick": None,
            }
        ]
        assert bridge.stats["delivered"] == 1

    run(scenario())


def test_sender_prefix_can_be_disabled(make_config):
    cfg = make_config(targets=("pipe@example.org",), prefix_sender=False)

    async def scenario():
        bridge, transport = _bridge(cfg)
        await bridge.on_event(message_event())
        assert transport.sent[0]["body"] == "hello from iMessage"

    run(scenario())


def test_known_contact_gets_its_own_jid(make_config):
    cfg = make_config(
        targets=("pipe@example.org",),
        contacts={"+15551234567": "alice@example.org"},
    )

    async def scenario():
        bridge, transport = _bridge(cfg)
        await bridge.on_event(message_event())
        assert transport.sent == [
            {"to": "alice@example.org", "body": "hello from iMessage", "mtype": "chat", "muc_nick": None}
        ]

    run(scenario())


def test_group_chat_is_delivered_to_the_muc(make_config):
    cfg = make_config(muc="imessage@conference.example.org", muc_nick="bridge")

    async def scenario():
        bridge, transport = _bridge(cfg)
        await bridge.on_event(message_event(is_group=True))
        assert transport.sent == [
            {
                "to": "imessage@conference.example.org",
                "body": "Alice (+15551234567): hello from iMessage",
                "mtype": "groupchat",
                "muc_nick": "Alice (+15551234567)",
            }
        ]

    run(scenario())


def test_group_chat_without_muc_falls_back_to_targets(make_config):
    cfg = make_config(targets=("pipe@example.org",))

    async def scenario():
        bridge, transport = _bridge(cfg)
        await bridge.on_event(message_event(is_group=True))
        assert transport.sent[0]["to"] == "pipe@example.org"
        assert transport.sent[0]["mtype"] == "chat"
        assert transport.sent[0]["body"].startswith("[chat-1] Alice (+15551234567): ")

    run(scenario())


def test_duplicate_guid_is_dropped(make_config):
    cfg = make_config(targets=("pipe@example.org",))

    async def scenario():
        bridge, transport = _bridge(cfg)
        event = message_event(guid="same-guid")
        await bridge.on_event(event)
        await bridge.on_event(dict(event))
        await bridge.on_event(dict(event))
        assert len(transport.sent) == 1
        assert bridge.stats["deduped"] == 2

    run(scenario())


def test_ready_event_is_not_a_message(make_config):
    cfg = make_config()

    async def scenario():
        bridge, transport = _bridge(cfg)
        await bridge.on_event({"type": "ready"})
        await bridge.on_event({"type": "typing", "guid": "x"})
        assert transport.sent == []

    run(scenario())


def test_unroutable_event_is_counted_not_crashed(make_config):
    cfg = make_config(targets=(), muc=None, contacts={})

    async def scenario():
        bridge, transport = _bridge(cfg)
        await bridge.on_event(message_event())
        assert transport.sent == []
        assert bridge.stats["unrouted"] == 1

    run(scenario())


def test_transport_failure_is_contained(make_config, caplog):
    cfg = make_config(targets=("pipe@example.org",))

    async def scenario():
        bridge, transport = _bridge(cfg, transport=FakeTransport(fail_sends=True))
        with caplog.at_level(logging.ERROR, logger="relay.core"):
            await bridge.on_event(message_event())
        assert bridge.stats["delivery_errors"] == 1
        assert transport.sent == []

    run(scenario())
    assert any("failed to deliver stanza" in record.message for record in caplog.records)
