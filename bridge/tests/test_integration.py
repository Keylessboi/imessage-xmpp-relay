"""Interactions between groups (MUC), msgfeatures and OMEMO."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from conftest import message_event, run
from fakes import FakeComponentTransport
from mock_upstream import MockUpstream  # noqa: F401  (import keeps parity with sibling tests)

from relay import xep
from relay.component import _is_reaction_payload
from relay.core import Bridge
from relay.muc import maybe_make_group_service
from relay.upstream import UpstreamClient

from test_component import PEER, component_config
from test_component_omemo import (
    OMEMO2_TAG,
    OWNER,
    PEER_A,
    close_all,
    make_transport,
)


class RecordingGroups:
    def __init__(self):
        self.messages = []

    async def on_message(self, message):
        self.messages.append(message)
        return True


def test_group_message_reaches_the_room_with_quote_fallback_and_is_indexed():
    async def scenario():
        cfg = component_config()
        transport = FakeComponentTransport()
        upstream = UpstreamClient(cfg.api_url)
        bridge = Bridge(cfg, transport, upstream)
        bridge.groups = RecordingGroups()
        await bridge.on_event(message_event(guid="P1", sender=PEER, text="parent", is_group=True,
                                            chat_guid="iMessage;+;grp"))
        await bridge.on_event({"type": "message_meta", "guid": "R1", "reply_to": "P1"})
        await bridge.on_event(message_event(guid="R1", sender=PEER, text="answer", is_group=True,
                                            chat_guid="iMessage;+;grp"))
        texts = [m.text for m in bridge.groups.messages]
        assert texts[0] == "parent"
        assert texts[1].startswith("> ") and texts[1].endswith("answer")
        assert transport.sent == []  # nothing leaked through the legacy per-peer path
        # the room stanza id is the guid, so an owner XEP-0461 reply/reaction still resolves
        assert bridge.rich.index.by_xmpp_id("R1").guid == "R1"
        await upstream.aclose()

    run(scenario())


def test_group_receipts_stay_labelled_notices_with_groups_attached():
    async def scenario():
        cfg = component_config()
        transport = FakeComponentTransport()
        upstream = UpstreamClient(cfg.api_url)
        bridge = Bridge(cfg, transport, upstream)
        bridge.groups = RecordingGroups()
        await bridge.on_event({"type": "reaction", "to_guid": "T9", "from": PEER, "chat_guid": "iMessage;+;g",
                               "reaction": "love", "enable": True})
        assert transport.sent and transport.sent[0]["body"].startswith("[iMessage;+;g] " + PEER)
        await upstream.aclose()

    run(scenario())


def test_omemo_disables_muc_rooms_fail_closed():
    cfg = component_config()
    object.__setattr__(cfg, "omemo", True) if cfg.__dataclass_params__.frozen else setattr(cfg, "omemo", True)

    class T:
        xmpp = type("X", (), {"register_handler": lambda *a, **k: None})()

    assert maybe_make_group_service(cfg, T(), None) is None


def test_reaction_payload_detection():
    els = xep.reactions("m1", ["👍"])
    assert [_is_reaction_payload(e) for e in els] == [True, True, False]
    assert not _is_reaction_payload(xep.markable())


def test_omemo_stanza_encrypts_body_keeps_reply_ids_and_drops_reaction_element(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            prefix, reply_els = xep.reply("m0", OWNER, "hi")
            els = reply_els + [xep.markable(), xep.receipt_request()]
            await transport.send_message(to=OWNER, body=prefix + "secret", frm=PEER_A,
                                         msg_id="m1", elements=els)
            (msg,) = cap.sent
            raw = ET.tostring(msg.xml, encoding="unicode")
            assert "secret" not in raw and msg.xml.find(OMEMO2_TAG) is not None
            assert msg["id"] == "m1"
            assert msg.xml.find("{" + xep.NS_REPLY + "}reply") is not None
            assert msg.xml.find("{" + xep.NS_MARKERS + "}markable") is not None

            cap.sent.clear()
            await transport.send_message(to=OWNER, body="Loved “hi”", frm=PEER_A, msg_id="m2",
                                         elements=xep.reactions("m0", ["❤️"]))
            (msg,) = cap.sent
            raw = ET.tostring(msg.xml, encoding="unicode")
            assert "reactions:0" not in raw and "❤" not in raw and "Loved" not in raw
            assert msg.xml.find(OMEMO2_TAG) is not None

            cap.sent.clear()  # body-less typing indicator: no content, goes as-is
            await transport.send_message(to=OWNER, body="", frm=PEER_A, elements=xep.chat_state("composing"))
            (msg,) = cap.sent
            assert msg.xml.find("{" + xep.NS_CHATSTATES + "}composing") is not None
            assert msg.xml.find(OMEMO2_TAG) is None
        finally:
            await close_all(co, owner)

    run(scenario())
