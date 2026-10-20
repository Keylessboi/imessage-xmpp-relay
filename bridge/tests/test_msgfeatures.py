"""Receipts, typing, reactions, replies, edits, retractions and placeholders.

Pure XEP helpers, the state helpers, and the Bridge in component mode against
the fake transport + mock upstream. Nothing here touches a real XMPP server or
real Apple; see the README for what that leaves untested.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from conftest import message_event, run
from fakes import FakeComponentTransport, FakeComponentXmpp, FakeStanza
from mock_upstream import MockUpstream, serve

from relay import xep
from relay.component import ComponentTransport
from relay.config import Config
from relay.core import Bridge
from relay.models import IncomingXmpp
from relay.rich import (
    MessageIndex,
    MsgRec,
    ReactionSets,
    attachment_placeholder,
    render_attachments,
    tapback_text,
)
from relay.upstream import UpstreamClient

from test_component import DOMAIN, OWNER, PEER, PEER_JID, component_config

NS = "{" + xep.NS_RECEIPTS + "}"


def tags(extra: dict) -> list[str]:
    return [el.tag for el in extra["elements"]]


def find(extra: dict, tag: str):
    return next((el for el in extra["elements"] if el.tag == tag), None)


def q(ns: str, tag: str) -> str:
    return "{" + ns + "}" + tag


def wire(cfg, url=None):
    transport = FakeComponentTransport()
    upstream = UpstreamClient(url or cfg.api_url)
    return Bridge(cfg, transport, upstream), transport, upstream


def msg_xml(inner: str, attrs: str = "") -> ET.Element:
    return ET.fromstring("<message xmlns='jabber:client' " + attrs + ">" + inner + "</message>")


# -- pure XEP helpers -----------------------------------------------------------


def test_parse_features_reads_receipts_markers_and_states():
    f = xep.parse_features(
        msg_xml(
            "<request xmlns='urn:xmpp:receipts'/><markable xmlns='urn:xmpp:chat-markers:0'/>"
            "<composing xmlns='http://jabber.org/protocol/chatstates'/>",
            "id='m1'",
        )
    )
    assert (f.msg_id, f.receipt_request, f.markable, f.chat_state) == ("m1", True, True, "composing")
    d = xep.parse_features(msg_xml("<displayed xmlns='urn:xmpp:chat-markers:0' id='g1'/>"))
    assert d.marker == ("displayed", "g1") and d.interesting
    assert not xep.parse_features(msg_xml("<received xmlns='urn:xmpp:receipts' id='g1'/>")).interesting


def test_parse_reactions_dedupes_and_allows_empty_set():
    f = xep.parse_features(
        msg_xml(
            "<reactions xmlns='urn:xmpp:reactions:0' id='g1'>"
            "<reaction>\U0001f44d</reaction><reaction>\U0001f44d</reaction><reaction>\U0001f389</reaction>"
            "</reactions>"
        )
    )
    assert f.reactions_id == "g1" and f.reactions == ("\U0001f44d", "\U0001f389")
    empty = xep.parse_features(msg_xml("<reactions xmlns='urn:xmpp:reactions:0' id='g1'/>"))
    assert empty.reactions == () and empty.interesting


def test_parse_reply_correction_retraction():
    f = xep.parse_features(
        msg_xml(
            "<reply xmlns='urn:xmpp:reply:0' to='a@b' id='orig'/>"
            "<replace xmlns='urn:xmpp:message-correct:0' id='r1'/>"
            "<retract xmlns='urn:xmpp:message-retract:1' id='r2'/>"
        )
    )
    assert (f.reply_id, f.reply_to, f.replace_id, f.retract_id) == ("orig", "a@b", "r1", "r2")


def test_strip_fallback_removes_the_quoted_range_only():
    f = xep.parse_features(
        msg_xml(
            "<reply xmlns='urn:xmpp:reply:0' id='o'/>"
            "<fallback xmlns='urn:xmpp:fallback:0' for='urn:xmpp:reply:0'>"
            "<body start='0' end='8'/></fallback>"
        )
    )
    assert xep.strip_fallback("> quote\nmy answer", f) == "my answer"


def test_whole_body_fallback_empties_the_body():
    f = xep.parse_features(
        msg_xml(
            "<retract xmlns='urn:xmpp:message-retract:1' id='x'/>"
            "<fallback xmlns='urn:xmpp:fallback:0' for='urn:xmpp:message-retract:1'/>"
        )
    )
    assert xep.strip_fallback("unsupported client text", f) == ""


def test_out_of_range_fallback_is_ignored():
    f = xep.parse_features(
        msg_xml("<fallback xmlns='urn:xmpp:fallback:0' for='x'><body start='0' end='99'/></fallback>")
    )
    assert xep.strip_fallback("short", f) == "short"


def test_builders_round_trip_through_the_parser():
    wrapper = ET.Element("message")
    for el in xep.reactions("g1", ["❤️"]) + xep.chat_state("composing"):
        wrapper.append(el)
    f = xep.parse_features(wrapper)
    assert f.reactions_id == "g1" and f.reactions == ("❤️",)
    assert f.chat_state == "composing"
    assert ("urn:xmpp:reactions:0", None, None) in f.fallbacks
    body, els = xep.reply("o1", "p@x", "hello there")
    assert body == "> hello there\n"
    w2 = ET.Element("message")
    for el in els:
        w2.append(el)
    f2 = xep.parse_features(w2)
    assert f2.reply_id == "o1" and xep.strip_fallback(body + "answer", f2) == "answer"


def test_chat_state_rejects_unknown_states():
    import pytest

    with pytest.raises(ValueError):
        xep.chat_state("typing")


def test_emoji_tapback_mapping():
    assert xep.emoji_to_reaction("❤️") == ("heart", None)
    assert xep.emoji_to_reaction("\U0001f44d") == ("like", None)
    assert xep.emoji_to_reaction("\U0001f389") == ("emoji", "\U0001f389")
    assert xep.reaction_to_emoji("laugh", None) == "\U0001f602"
    assert xep.reaction_to_emoji("emoji", "\U0001f389") == "\U0001f389"
    assert xep.reaction_to_emoji("sticker", None) is None


# -- state helpers ----------------------------------------------------------------


def test_attachment_placeholders():
    assert attachment_placeholder({"name": "wave.png", "sticker": True}) == "[sticker: wave.png]"
    assert attachment_placeholder({"sticker": True}) == "[sticker]"
    assert attachment_placeholder({"name": "a.jpg", "mime": "image/jpeg"}) == "[attachment: a.jpg (image/jpeg)]"
    assert attachment_placeholder({}) == "[attachment]"
    assert render_attachments("￼", [{"name": "s.png", "sticker": True}]) == "[sticker: s.png]"
    assert render_attachments("look ￼ and ￼", [{"name": "a"}]) == "look [attachment: a] and [attachment]"
    assert render_attachments("plain", []) == "plain"
    assert render_attachments("x", [{"name": "late"}]) == "x [attachment: late]"


def test_tapback_text():
    assert tapback_text("heart", None, True, "hi") == "Loved “hi”"
    assert tapback_text("heart", None, False, None) == "Removed ❤️ from a message"
    assert tapback_text("emoji", "\U0001f389", True, "x") == "Reacted \U0001f389 to “x”"


def test_message_index_is_bounded_and_cross_referenced():
    idx = MessageIndex(maxsize=2)
    for n in range(3):
        idx.remember(MsgRec(guid="g%d" % n, peer="+1", text="t", outbound=False, xmpp_id="x%d" % n))
    assert idx.by_guid("g0") is None and idx.by_xmpp_id("x0") is None
    assert idx.by_xmpp_id("x2").guid == "g2"
    assert idx.by_guid("missing") is None and idx.by_xmpp_id(None) is None


def test_reaction_sets_accumulate_and_clear():
    sets = ReactionSets()
    assert sets.apply("g", "+1", "A", True) == ["A"]
    assert sets.apply("g", "+1", "B", True) == ["A", "B"]
    assert sets.apply("g", "+1", "A", True) == ["A", "B"]
    assert sets.apply("g", "+1", "A", False) == ["B"]
    assert sets.apply("g", "+2", "A", True) == ["A"]
    assert sets.apply("g", "+1", "B", False) == []


def test_read_receipt_and_typing_config_default_on_and_can_be_disabled():
    base = {
        "XMPP_MODE": "component", "COMPONENT_JID": DOMAIN,
        "COMPONENT_SECRET": "s", "COMPONENT_OWNER": OWNER,
    }
    cfg = Config.from_env(dict(base))
    assert cfg.send_read_receipts and cfg.send_typing
    off = Config.from_env({**base, "RELAY_SEND_READ_RECEIPTS": "0", "RELAY_SEND_TYPING": "off"})
    assert not off.send_read_receipts and not off.send_typing


# -- iMessage -> XMPP ----------------------------------------------------------------


def test_inbound_message_carries_guid_id_and_requests_receipts():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event(message_event(guid="G1", sender=PEER, text="hello"))
        assert transport.sent[0]["body"] == "hello"  # body untouched
        extra = transport.extras[0]
        assert extra["msg_id"] == "G1"
        assert q(xep.NS_MARKERS, "markable") in tags(extra)
        assert q(xep.NS_RECEIPTS, "request") in tags(extra)
        assert q(xep.NS_HINTS, "store") in tags(extra)
        await upstream.aclose()

    run(scenario())


def test_client_mode_gets_no_stanza_extras(make_config):
    from fakes import FakeTransport

    async def scenario():
        cfg = make_config()
        transport = FakeTransport()
        bridge = Bridge(cfg, transport, UpstreamClient(cfg.api_url))
        await bridge.on_event(message_event())
        assert transport.extras[0] == {"msg_id": None, "elements": []}
        await bridge.upstream.aclose()

    run(scenario())


def test_sticker_and_attachment_become_readable_text():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event(
            {"type": "message_meta", "guid": "G2", "attachments": [{"name": "w.png", "mime": "image/png", "sticker": True}]}
        )
        await bridge.on_event(message_event(guid="G2", sender=PEER, text="￼"))
        assert transport.sent[0]["body"] == "[sticker: w.png]"
        await bridge.on_event(message_event(guid="G3", sender=PEER, text="pic ￼"))
        assert transport.sent[1]["body"] == "pic [attachment]"
        await upstream.aclose()

    run(scenario())


def test_inbound_reply_references_the_known_parent_with_a_quote_fallback():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event(message_event(guid="P1", sender=PEER, text="lunch?"))
        await bridge.on_event({"type": "message_meta", "guid": "R1", "reply_to": "P1", "attachments": []})
        await bridge.on_event(message_event(guid="R1", sender=PEER, text="yes please"))
        sent = transport.sent[1]
        assert sent["body"] == "> lunch?\nyes please"
        reply = find(transport.extras[1], q(xep.NS_REPLY, "reply"))
        assert reply.get("id") == "P1" and reply.get("to") == PEER_JID
        f = xep.parse_features(_wrap(transport.extras[1]["elements"]))
        assert xep.strip_fallback(sent["body"], f) == "yes please"
        await upstream.aclose()

    run(scenario())


def _wrap(elements):
    w = ET.Element("message")
    for el in elements:
        w.append(el)
    return w


def test_reply_to_an_owner_message_uses_the_owner_stanza_id():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, transport, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid=OWNER + "/phone", body="my text", to_jid=PEER_JID, msg_id="own-1")
            )
            await bridge.on_event({"type": "message_meta", "guid": "R2", "reply_to": "sent-guid-1"})
            await bridge.on_event(message_event(guid="R2", sender=PEER, text="re"))
            reply = find(transport.extras[0], q(xep.NS_REPLY, "reply"))
            assert reply.get("id") == "own-1" and reply.get("to") == OWNER
            await upstream.aclose()

    run(scenario())


def test_delivered_and_read_map_back_to_the_owner_stanza_id():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, transport, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(
                IncomingXmpp(
                    from_jid=OWNER + "/phone", body="hi", to_jid=PEER_JID,
                    msg_id="own-1", receipt_request=True, markable=True,
                )
            )
            await bridge.on_event({"type": "delivered", "guid": "sent-guid-1", "from": PEER, "chat_guid": "c"})
            await bridge.on_event({"type": "read", "guid": "sent-guid-1", "from": PEER, "chat_guid": "c"})
            received, displayed = transport.sent[0], transport.sent[1]
            assert received["to"] == OWNER and received["frm"] == PEER_JID and received["body"] == ""
            r = find(transport.extras[0], q(xep.NS_RECEIPTS, "received"))
            assert r.get("id") == "own-1"
            d = find(transport.extras[1], q(xep.NS_MARKERS, "displayed"))
            assert d.get("id") == "own-1"
            await upstream.aclose()

    run(scenario())


def test_receipts_are_not_sent_when_the_owner_did_not_ask_or_the_guid_is_unknown():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, transport, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(
                IncomingXmpp(from_jid=OWNER + "/phone", body="hi", to_jid=PEER_JID, msg_id="own-1")
            )
            await bridge.on_event({"type": "delivered", "guid": "sent-guid-1", "from": PEER})
            await bridge.on_event({"type": "read", "guid": "sent-guid-1", "from": PEER})
            await bridge.on_event({"type": "read", "guid": "never-seen", "from": PEER})
            assert transport.sent == []
            await upstream.aclose()

    run(scenario())


def test_typing_becomes_chat_states_from_the_peer_jid():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event({"type": "typing", "from": PEER, "chat_guid": "iMessage;-;" + PEER, "typing": True})
        await bridge.on_event({"type": "typing", "from": PEER, "chat_guid": "iMessage;-;" + PEER, "typing": False})
        assert [t["frm"] for t in transport.sent] == [PEER_JID, PEER_JID]
        assert q(xep.NS_CHATSTATES, "composing") in tags(transport.extras[0])
        assert q(xep.NS_CHATSTATES, "active") in tags(transport.extras[1])
        assert q(xep.NS_HINTS, "no-store") in tags(transport.extras[0])
        # group typing is dropped
        await bridge.on_event({"type": "typing", "from": PEER, "chat_guid": "iMessage;+;abc", "typing": True})
        assert len(transport.sent) == 2
        await upstream.aclose()

    run(scenario())


def test_tapbacks_send_the_full_reaction_set_with_a_fallback_body():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event(message_event(guid="T1", sender=PEER, text="dinner at 7"))

        def react(name, enable=True, emoji=None):
            return {"type": "reaction", "guid": "x", "to_guid": "T1", "from": PEER,
                    "chat_guid": "c", "reaction": name, "emoji": emoji, "enable": enable}

        await bridge.on_event(react("heart"))
        await bridge.on_event(react("like"))
        await bridge.on_event(react("heart", enable=False))
        sent = transport.sent[1:]
        assert sent[0]["body"] == "Loved “dinner at 7”" and sent[0]["frm"] == PEER_JID

        def emojis(extra):
            el = find(extra, q(xep.NS_REACTIONS, "reactions"))
            assert el.get("id") == "T1"
            return [r.text for r in el]

        assert emojis(transport.extras[1]) == ["❤️"]
        assert emojis(transport.extras[2]) == ["❤️", "\U0001f44d"]
        assert emojis(transport.extras[3]) == ["\U0001f44d"]
        assert find(transport.extras[1], q(xep.NS_FALLBACK, "fallback")).get("for") == xep.NS_REACTIONS
        await upstream.aclose()

    run(scenario())


def test_sticker_tapback_is_text_only_and_group_tapback_is_a_labelled_notice():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event({"type": "reaction", "to_guid": "T9", "from": PEER, "chat_guid": "c",
                               "reaction": "sticker", "enable": True})
        assert tags(transport.extras[0]) == [] and "sticker" in transport.sent[0]["body"]
        await bridge.on_event({"type": "reaction", "to_guid": "T9", "from": PEER, "chat_guid": "iMessage;+;g",
                               "reaction": "heart", "enable": True})
        assert transport.sent[1]["frm"] is None
        assert transport.sent[1]["body"].startswith("[iMessage;+;g] " + PEER + ": Loved")
        await upstream.aclose()

    run(scenario())


def test_edit_becomes_a_correction_of_the_original_id():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event(message_event(guid="E1", sender=PEER, text="teh"))
        await bridge.on_event({"type": "edit", "guid": "E1", "part": 0, "from": PEER, "chat_guid": "c", "text": "the"})
        assert transport.sent[1]["body"] == "the" and transport.sent[1]["frm"] == PEER_JID
        assert find(transport.extras[1], q(xep.NS_CORRECT, "replace")).get("id") == "E1"
        assert transport.extras[1]["msg_id"] and transport.extras[1]["msg_id"] != "E1"
        assert bridge.rich.index.by_guid("E1").text == "the"
        await upstream.aclose()

    run(scenario())


def test_unsend_becomes_a_retraction_with_a_fallback_body():
    async def scenario():
        bridge, transport, upstream = wire(component_config())
        await bridge.on_event(message_event(guid="U1", sender=PEER, text="oops"))
        await bridge.on_event({"type": "unsend", "guid": "U1", "part": 0, "from": PEER, "chat_guid": "c"})
        assert find(transport.extras[1], q(xep.NS_RETRACT, "retract")).get("id") == "U1"
        assert transport.sent[1]["body"] == xep.RETRACT_FALLBACK_TEXT
        await bridge.on_event({"type": "unsend", "guid": "U1", "part": 2, "from": PEER, "chat_guid": "c"})
        assert tags(transport.extras[2]) == [] and "unsent" in transport.sent[2]["body"]
        await upstream.aclose()

    run(scenario())


def test_feature_events_are_ignored_in_client_mode(make_config):
    from fakes import FakeTransport

    async def scenario():
        cfg = make_config()
        transport = FakeTransport()
        bridge = Bridge(cfg, transport, UpstreamClient(cfg.api_url))
        for etype in ("delivered", "read", "typing", "reaction", "edit", "unsend"):
            await bridge.on_event({"type": etype, "guid": "g", "from": "+1", "chat_guid": "c"})
        assert transport.sent == []
        await bridge.upstream.aclose()

    run(scenario())


# -- XMPP -> iMessage --------------------------------------------------------------------


def xmpp(**kw) -> IncomingXmpp:
    base = dict(from_jid=OWNER + "/phone", body="", to_jid=PEER_JID)
    base.update(kw)
    return IncomingXmpp(**base)


def test_displayed_marker_sends_one_read_receipt():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, transport, upstream = wire(component_config(api_url=url), url)
            await bridge.on_event(message_event(guid="D1", sender=PEER))
            await bridge.on_xmpp_message(xmpp(displayed_id="D1"))
            await bridge.on_xmpp_message(xmpp(displayed_id="D1"))  # repeat is dropped
            assert mock.actions == [("read", {"to": PEER, "guid": "D1"})]
            assert mock.sent == []  # never mistaken for a text message
            await upstream.aclose()

    run(scenario())


def test_read_receipts_can_be_turned_off():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url, send_read_receipts=False), url)
            await bridge.on_event(message_event(guid="D1", sender=PEER))
            await bridge.on_xmpp_message(xmpp(displayed_id="D1"))
            assert mock.actions == []
            await upstream.aclose()

    run(scenario())


def test_displayed_marker_for_an_unknown_id_still_reads_by_guid():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(xmpp(displayed_id="OLD-GUID"))
            assert mock.actions == [("read", {"to": PEER, "guid": "OLD-GUID"})]
            await upstream.aclose()

    run(scenario())


def test_composing_and_paused_map_to_typing_with_debounce():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url), url)
            for state in ("composing", "composing", "paused", "active"):
                await bridge.on_xmpp_message(xmpp(chat_state=state))
            assert mock.actions == [
                ("typing", {"to": PEER, "typing": True}),
                ("typing", {"to": PEER, "typing": False}),
            ]
            await upstream.aclose()

    run(scenario())


def test_chat_state_alongside_a_body_does_not_double_send():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(xmpp(body="hello", chat_state="active"))
            assert mock.actions == [] and mock.sent[0]["text"] == "hello"
            await upstream.aclose()

    run(scenario())


def test_typing_can_be_turned_off():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url, send_typing=False), url)
            await bridge.on_xmpp_message(xmpp(chat_state="composing"))
            assert mock.actions == []
            await upstream.aclose()

    run(scenario())


def test_reaction_set_changes_become_tapback_actions():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url), url)
            await bridge.on_event(message_event(guid="T1", sender=PEER, text="dinner"))
            heart, party = "❤️", "\U0001f389"
            await bridge.on_xmpp_message(xmpp(reactions_id="T1", reactions=(heart,)))
            await bridge.on_xmpp_message(xmpp(reactions_id="T1", reactions=(heart, party)))
            await bridge.on_xmpp_message(xmpp(reactions_id="T1", reactions=(party,)))
            await bridge.on_xmpp_message(xmpp(reactions_id="T1", reactions=()))
            kinds = [(k, p["reaction"], p.get("emoji"), p["enable"]) for k, p in mock.actions]
            assert kinds == [
                ("react", "heart", None, True),
                ("react", "emoji", party, True),
                ("react", "heart", None, False),
                ("react", "emoji", party, False),
            ]
            assert mock.actions[0][1]["to_text"] == "dinner" and mock.actions[0][1]["guid"] == "T1"
            assert mock.sent == []
            await upstream.aclose()

    run(scenario())


def test_failed_reaction_is_reported_to_the_owner():
    async def scenario():
        mock = MockUpstream()
        mock.action_status = 503
        async with serve(mock) as url:
            bridge, transport, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(xmpp(reactions_id="T1", reactions=("\U0001f44d",)))
            assert "iMessage react failed" in transport.sent[0]["body"]
            assert bridge.stats["action_errors"] == 1
            await upstream.aclose()

    run(scenario())


def test_xmpp_reply_threads_onto_the_referenced_guid():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url), url)
            await bridge.on_event(message_event(guid="A", sender=PEER))
            await bridge.on_event(message_event(guid="B", sender=PEER))
            await bridge.on_xmpp_message(xmpp(body="answer to A", reply_id="A"))
            await bridge.on_xmpp_message(xmpp(body="plain"))
            assert mock.sent[0]["reply_to"] == "A"
            assert mock.sent[1]["reply_to"] == "B"  # old behaviour: latest inbound
            await upstream.aclose()

    run(scenario())


def test_correction_and_retraction_of_an_owner_message():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, transport, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(xmpp(body="helo", msg_id="own-1"))
            await bridge.on_xmpp_message(xmpp(body="hello", msg_id="own-2", replace_id="own-1"))
            await bridge.on_xmpp_message(xmpp(retract_id="own-1"))
            assert len(mock.sent) == 1  # the correction is not a new message
            assert mock.actions == [
                ("edit", {"to": PEER, "guid": "sent-guid-1", "text": "hello", "part": 0}),
                ("unsend", {"to": PEER, "guid": "sent-guid-1", "part": 0}),
            ]
            assert transport.sent == []
            await upstream.aclose()

    run(scenario())


def test_correcting_an_unknown_or_foreign_message_is_refused_with_a_notice():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, transport, upstream = wire(component_config(api_url=url), url)
            await bridge.on_event(message_event(guid="IN", sender=PEER))
            await bridge.on_xmpp_message(xmpp(body="x", replace_id="IN"))     # peer's message
            await bridge.on_xmpp_message(xmpp(retract_id="nope"))             # unknown
            assert mock.actions == [] and mock.sent == []
            notices = transport.bodies()[1:]  # [0] is the inbound message itself
            assert len(notices) == 2
            assert "Cannot edit" in notices[0] and "Cannot unsend" in notices[1]
            await upstream.aclose()

    run(scenario())


def test_feature_stanzas_from_strangers_are_denied():
    async def scenario():
        mock = MockUpstream()
        async with serve(mock) as url:
            bridge, _, upstream = wire(component_config(api_url=url), url)
            await bridge.on_xmpp_message(xmpp(from_jid="stranger@evil.example/x", displayed_id="D1"))
            await bridge.on_xmpp_message(xmpp(from_jid="stranger@evil.example/x", reactions_id="a", reactions=("x",)))
            assert mock.actions == [] and bridge.stats["denied"] == 2
            await upstream.aclose()

    run(scenario())


# -- component transport wiring -------------------------------------------------------------


def test_component_transport_parses_feature_stanzas():
    async def scenario():
        fake = FakeComponentXmpp()
        transport = ComponentTransport(component_config(), xmpp=fake)
        seen: list[IncomingXmpp] = []

        async def handler(message):
            seen.append(message)

        transport.set_incoming_handler(handler)
        base = {"type": "chat", "to": PEER_JID, "from": OWNER + "/p", "body": ""}
        await fake.fire("message", FakeStanza(**base, xml=msg_xml(
            "<displayed xmlns='urn:xmpp:chat-markers:0' id='g1'/>")))
        await fake.fire("message", FakeStanza(**base, xml=msg_xml(
            "<received xmlns='urn:xmpp:receipts' id='g1'/>")))  # nothing to act on
        await fake.fire("message", FakeStanza(**{**base, "body": "> q\nreal"}, xml=msg_xml(
            "<reply xmlns='urn:xmpp:reply:0' id='o'/><request xmlns='urn:xmpp:receipts'/>"
            "<fallback xmlns='urn:xmpp:fallback:0' for='urn:xmpp:reply:0'><body start='0' end='4'/></fallback>",
            "id='m9'")))
        assert len(seen) == 2
        assert seen[0].displayed_id == "g1" and seen[0].body == ""
        assert (seen[1].body, seen[1].reply_id, seen[1].msg_id, seen[1].receipt_request) == ("real", "o", "m9", True)

    run(scenario())


def test_component_transport_sets_id_and_appends_elements():
    async def scenario():
        fake = FakeComponentXmpp()
        transport = ComponentTransport(component_config(), xmpp=fake)
        await transport.send_message(
            to=OWNER, body="", frm=PEER_JID, msg_id="abc", elements=xep.received_receipt("x1")
        )
        record = fake.messages()[0]
        assert record["id"] == "abc" and record["body"] is None
        assert [e.tag for e in record["elements"]] == [
            q(xep.NS_RECEIPTS, "received"), q(xep.NS_HINTS, "no-store")
        ]

    run(scenario())
