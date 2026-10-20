"""iMessage groups as XEP-0045 rooms (relay/muc.py).

No network, no slixmpp socket: the component is a fake that records every raw
stanza string, and inbound stanzas are fed as namespaced XML exactly like the
`.xml` of a slixmpp stanza.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from conftest import run
from relay.component import localpart_to_handle
from relay.config import Config
from relay.models import IMessageEvent
from relay.muc import (
    NS_CONFERENCE,
    NS_DELAY,
    NS_DISCO_INFO,
    NS_DISCO_ITEMS,
    NS_RSM,
    NS_USER,
    REINVITE_SECS,
    GroupService,
    HistoryItem,
    display_name,
    format_stamp,
    groups_enabled,
    jid_to_chat_guid,
    localpart_to_chat_guid,
    maybe_make_group_service,
    page,
    parse_history_request,
    parse_stamp,
    room_jid,
    room_localpart,
    select_history,
)
from relay.upstream import UpstreamError

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"
FULL = OWNER + "/phone"
GUID = "8F3A1C2D-0000-4B1C-9A11-ABCDEF012345"
ROOM = room_jid(GUID, DOMAIN)
ALICE = "+15557654321"
BOB = "bob@icloud.com"
ME = "+15551234567"
WIRE = "jabber:component:accept"


def cfg() -> Config:
    return Config(
        mode="component",
        component_jid=DOMAIN,
        component_domain=DOMAIN,
        component_host="127.0.0.1",
        component_port=5347,
        component_secret="s",
        component_owner=OWNER,
        targets=(OWNER,),
        allowed=frozenset({OWNER}),
    )


class FakeXmpp:
    default_ns = WIRE

    def __init__(self) -> None:
        self.raw: list[str] = []
        self.event_handlers: dict = {}
        self.handlers: list = []

    def send_raw(self, data: str) -> None:
        self.raw.append(data)

    def add_event_handler(self, name, fn) -> None:
        self.event_handlers.setdefault(name, []).append(fn)

    def register_handler(self, handler) -> None:
        self.handlers.append(handler)

    def stanzas(self) -> list[ET.Element]:
        return [ET.fromstring(x) for x in self.raw]

    def take(self) -> list[ET.Element]:
        out = self.stanzas()
        self.raw.clear()
        return out


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail: str | None = None
        self.group_list: list[dict] = [
            {"chat_guid": GUID, "name": "Weekend", "participants": [ME, ALICE, BOB], "group_version": 0}
        ]
        self.created: dict = {
            "ok": True,
            "chat_guid": "AAAAAAAA-0000-4000-8000-000000000001",
            "guid": "G1",
            "group": {
                "chat_guid": "AAAAAAAA-0000-4000-8000-000000000001",
                "name": "trip",
                "participants": [ME, ALICE, BOB],
                "group_version": 0,
            },
        }

    def _maybe_fail(self):
        if self.fail:
            raise UpstreamError(self.fail)

    async def groups(self):
        self.calls.append(("groups",))
        return self.group_list

    async def create(self, participants, name, text):
        self.calls.append(("create", sorted(participants), name, text))
        self._maybe_fail()
        return self.created

    async def rename(self, chat_guid, name):
        self.calls.append(("rename", chat_guid, name))
        self._maybe_fail()
        return {"ok": True}

    async def members(self, chat_guid, add, remove):
        self.calls.append(("members", chat_guid, add, remove))
        self._maybe_fail()
        cur = [ME, ALICE, BOB]
        new = sorted((set(cur) | set(add)) - set(remove))
        return {"ok": True, "group": {"chat_guid": chat_guid, "participants": new}}

    async def leave(self, chat_guid):
        self.calls.append(("leave", chat_guid))
        self._maybe_fail()
        return {"ok": True}

    async def send(self, chat_guid, text, reply_to=None):
        self.calls.append(("send", chat_guid, text))
        self._maybe_fail()
        return {"ok": True}


def make(clock=None):
    xmpp, api = FakeXmpp(), FakeApi()
    now = [1_000_000.0]
    svc = GroupService(
        cfg(), xmpp, api,
        clock=clock or (lambda: now[0]),
    )
    svc.mine.add(ME)
    svc._now = now  # type: ignore[attr-defined]
    return svc, xmpp, api


def stanza(xml: str) -> ET.Element:
    """Inbound stanza as slixmpp hands it over: tags in the stream namespace."""
    el = ET.fromstring(xml.replace("<presence", "<presence xmlns='" + WIRE + "'", 1)
                       .replace("<message", "<message xmlns='" + WIRE + "'", 1)
                       .replace("<iq", "<iq xmlns='" + WIRE + "'", 1))
    return el


def join_xml(nick="me", room=ROOM, frm=FULL, extra=""):
    return ("<presence from='%s' to='%s/%s'><x xmlns='http://jabber.org/protocol/muc'>%s</x></presence>"
            % (frm, room, nick, extra))


def tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def find(el, local, ns=None):
    for ch in el.iter():
        if tag(ch) == local and (ns is None or ch.tag == "{%s}%s" % (ns, local)):
            return ch
    return None


def event(text="hi", sender=ALICE, to=(ME, BOB), ts=1_700_000_000_000, guid="m1", **kw):
    return IMessageEvent(guid=guid, chat_guid=GUID, sender=sender, to=tuple(to), text=text,
                         timestamp=ts, is_group=True, raw={}, **kw)


# -- pure helpers -------------------------------------------------------------


def test_room_localpart_round_trips_and_never_looks_like_a_peer():
    for guid in (GUID, "iMessage;+;00ff", "weird guid/with:stuff", "8f3a1c2d-0000-4b1c-9a11-abcdef012345"):
        local = room_localpart(guid)
        assert local and localpart_to_chat_guid(local) == guid
        assert localpart_to_handle(local) is None
    assert room_localpart(GUID) == "grp-u8f3a1c2d00004b1c9a11abcdef012345"
    assert localpart_to_chat_guid("+15557654321") is None
    assert localpart_to_chat_guid("grp-uzz") is None
    assert localpart_to_chat_guid("grp-x4") is None
    assert room_localpart("") is None


def test_room_jid_helpers():
    assert ROOM == "grp-u8f3a1c2d00004b1c9a11abcdef012345@" + DOMAIN
    assert jid_to_chat_guid(ROOM + "/nick", DOMAIN) == GUID
    assert jid_to_chat_guid(ROOM, "other.example") is None


def test_display_name_falls_back_to_members():
    assert display_name("  Crew ", [ME, ALICE], {ME}) == "Crew"
    assert display_name(None, [ME, ALICE, BOB], {ME}) == ALICE + ", " + BOB
    assert display_name(None, ["a", "b", "c", "d", "e"], set()) == "a, b, c +2"
    assert display_name(None, [ME], {ME}) == "iMessage group"


def test_stamp_round_trip():
    assert format_stamp(1_700_000_000) == "2023-11-14T22:13:20Z"
    assert parse_stamp("2023-11-14T22:13:20Z") == 1_700_000_000
    assert parse_stamp("nonsense") is None


def test_select_history_rules():
    items = [HistoryItem("a", "x" * 10, t) for t in (100.0, 200.0, 300.0, 400.0)]
    assert select_history(items, None, 500) == items
    assert select_history(items, {"maxstanzas": 2}, 500) == items[2:]
    assert select_history(items, {"maxstanzas": 0}, 500) == []
    assert select_history(items, {"seconds": 250}, 500) == items[2:]
    assert select_history(items, {"since": 200.0}, 500) == items[2:]
    assert select_history(items, {"maxchars": 25}, 500) == items[2:]


def test_parse_history_request():
    x = ET.fromstring("<x xmlns='http://jabber.org/protocol/muc'><history maxstanzas='5' seconds='x' "
                      "since='2023-11-14T22:13:20Z'/></x>")
    assert parse_history_request(x) == {"maxstanzas": 5, "since": 1_700_000_000}
    assert parse_history_request(ET.fromstring("<x xmlns='a'/>")) is None
    assert parse_history_request(None) is None


def test_page_after_and_max():
    items = ["a", "b", "c", "d"]
    assert page(items, None, None) == (items, 4)
    assert page(items, 2, None) == (["a", "b"], 4)
    assert page(items, 2, "b") == (["c", "d"], 4)
    assert page(items, 2, "zzz") == ([], 4)


def test_groups_enabled_flag():
    assert groups_enabled({})
    assert groups_enabled({"XMPP_GROUPS_MUC": "1"})
    assert not groups_enabled({"XMPP_GROUPS_MUC": "0"})
    assert not groups_enabled({"XMPP_GROUPS_MUC": "off"})


# -- iMessage -> XMPP ---------------------------------------------------------


def test_message_while_not_joined_invites_once_and_keeps_history():
    async def go():
        svc, xmpp, api = make()
        assert await svc.on_message(event("one")) is True
        out = xmpp.take()
        assert len(out) == 1 and tag(out[0]) == "message"
        invite = find(out[0], "x", NS_CONFERENCE)
        assert invite.get("jid") == ROOM
        assert out[0].get("to") == OWNER and out[0].get("from") == ROOM
        # group name came from imessage-server
        assert invite.get("reason") == "Weekend"
        # a second message inside the re-invite window does not invite again
        await svc.on_message(event("two", guid="m2"))
        assert xmpp.take() == []
        svc._now[0] += REINVITE_SECS + 1
        await svc.on_message(event("three", guid="m3"))
        assert len(xmpp.take()) == 1
        assert [h.text for h in svc.rooms[GUID].history] == ["one", "two", "three"]
        assert api.calls == [("groups",)]  # refreshed once, not per message

    run(go())


def test_message_while_joined_is_a_groupchat_from_the_sender_occupant():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event("warm up"))
        await svc.handle_presence(stanza(join_xml()))
        xmpp.take()
        await svc.on_message(event("hello room", sender=BOB, to=(ME, ALICE), guid="m9"))
        (msg,) = xmpp.take()
        assert msg.get("type") == "groupchat"
        assert msg.get("from") == ROOM + "/" + BOB
        assert msg.get("to") == FULL and msg.get("id") == "m9"
        assert find(msg, "body").text == "hello room"
        assert find(msg, "delay") is None

    run(go())


def test_join_sends_occupants_self_presence_history_then_subject():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event("older", ts=1_700_000_000_000))
        xmpp.take()
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        out = xmpp.take()
        kinds = [tag(s) for s in out]
        assert kinds == ["presence", "presence", "presence", "message", "message"]
        occupants = {s.get("from") for s in out[:2]}
        assert occupants == {ROOM + "/" + ALICE, ROOM + "/" + BOB}
        item = find(out[0], "item")
        assert item.get("affiliation") == "member" and item.get("jid", "").endswith("@" + DOMAIN)
        selfp = out[2]
        assert selfp.get("from") == ROOM + "/travis"
        codes = {st.get("code") for st in selfp.iter() if tag(st) == "status"}
        assert codes == {"100", "110"}
        hist = out[3]
        assert hist.get("from") == ROOM + "/" + ALICE
        assert find(hist, "delay", NS_DELAY).get("stamp") == "2023-11-14T22:13:20Z"
        subject = out[4]
        assert find(subject, "subject").text == "Weekend" and subject.get("from") == ROOM

    run(go())


def test_join_honours_history_maxstanzas_zero():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event("older"))
        xmpp.take()
        await svc.handle_presence(stanza(join_xml(extra="<history maxstanzas='0'/>")))
        assert [tag(s) for s in xmpp.take()] == ["presence", "presence", "presence", "message"]

    run(go())


def test_join_rejects_bad_requests():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event())
        xmpp.take()
        # no nick
        await svc.handle_presence(stanza("<presence from='%s' to='%s'><x xmlns='http://jabber.org/protocol/muc'/></presence>" % (FULL, ROOM)))
        assert find(xmpp.take()[0], "jid-malformed") is not None
        # nick equal to an occupant
        await svc.handle_presence(stanza(join_xml(nick=ALICE)))
        assert find(xmpp.take()[0], "conflict") is not None
        # unknown group
        other = room_jid("11111111-2222-4333-8444-555555555555", DOMAIN)
        svc.api.group_list = []
        await svc.handle_presence(stanza(join_xml(room=other)))
        assert find(xmpp.take()[-1], "item-not-found") is not None
        # a peer JID is not a room
        await svc.handle_presence(stanza(join_xml(room=ALICE + "@" + DOMAIN)))
        assert xmpp.take() == []
        # a stranger is dropped silently
        await svc.handle_presence(stanza(join_xml(frm="eve@evil.example/x")))
        assert xmpp.take() == []

    run(go())


def test_second_session_must_use_the_same_nick():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event())
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        await svc.handle_presence(stanza(join_xml(nick="other", frm=OWNER + "/laptop")))
        assert find(xmpp.take()[0], "conflict") is not None
        await svc.handle_presence(stanza(join_xml(nick="travis", frm=OWNER + "/laptop")))
        assert len(svc.rooms[GUID].sessions) == 2

    run(go())


def test_leaving_the_room_only_leaves_xmpp():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event())
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        await svc.handle_presence(stanza(
            "<presence type='unavailable' from='%s' to='%s/travis'/>" % (FULL, ROOM)))
        (p,) = xmpp.take()
        assert p.get("type") == "unavailable" and find(p, "status").get("code") == "110"
        assert svc.rooms[GUID].sessions == {}
        assert ("leave", GUID) not in api.calls

    run(go())


def test_rename_event_updates_subject_for_joined_owner():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event())
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        await svc.on_group_event({"type": "group", "kind": "rename", "chat_guid": GUID,
                                  "from": ALICE, "name": "Renamed"})
        (m,) = xmpp.take()
        assert find(m, "subject").text == "Renamed" and m.get("from") == ROOM + "/" + ALICE

    run(go())


def test_member_change_event_sends_presence_and_status_line():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event())
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        carol = "+15550001111"
        await svc.on_group_event({"type": "group", "kind": "participants", "chat_guid": GUID,
                                  "from": ALICE, "participants": [ME, ALICE, carol],
                                  "added": [carol], "removed": [BOB]})
        out = xmpp.take()
        joined = [s for s in out if tag(s) == "presence" and s.get("type") is None]
        left = [s for s in out if tag(s) == "presence" and s.get("type") == "unavailable"]
        assert [s.get("from") for s in joined] == [ROOM + "/" + carol]
        assert [s.get("from") for s in left] == [ROOM + "/" + BOB]
        lines = [find(s, "body").text for s in out if tag(s) == "message"]
        assert lines == [ALICE + " added " + carol, ALICE + " removed " + BOB]
        assert svc.occupants(svc.rooms[GUID]) == sorted([ALICE, carol])

    run(go())


def test_leave_line_and_icon_line():
    svc, _, _ = make()
    assert svc._change_lines(ALICE, [], [ALICE]) == [ALICE + " left"]

    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event())
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        await svc.on_group_event({"type": "group", "kind": "icon", "chat_guid": GUID, "from": ALICE})
        (m,) = xmpp.take()
        assert "icon" in find(m, "body").text

    run(go())


# -- XMPP -> iMessage ---------------------------------------------------------


def joined(svc, xmpp):
    async def go():
        await svc.on_message(event("seed"))
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()

    run(go())


def test_owner_message_is_sent_and_reflected():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        await svc.handle_message(stanza(
            "<message type='groupchat' id='c1' from='%s' to='%s'><body>yo</body></message>" % (FULL, ROOM)))
        assert ("send", GUID, "yo") in api.calls
        (m,) = xmpp.take()
        assert m.get("from") == ROOM + "/travis" and m.get("id") == "c1" and m.get("to") == FULL
        assert [h.text for h in svc.rooms[GUID].history][-1] == "yo"

    run(go())


def test_owner_message_without_joining_or_when_send_fails_gets_an_error():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        xmpp.take()
        msg = "<message type='groupchat' from='%s' to='%s'><body>yo</body></message>" % (FULL, ROOM)
        await svc.handle_message(stanza(msg))
        (e,) = xmpp.take()
        assert e.get("type") == "error" and find(e, "not-acceptable") is not None
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        api.fail = "boom"
        await svc.handle_message(stanza(msg))
        (e,) = xmpp.take()
        assert find(e, "recipient-unavailable") is not None
        assert "boom" in find(e, "text").text

    run(go())


def test_non_owner_room_traffic_is_dropped():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        xmpp.take()
        await svc.handle_message(stanza(
            "<message type='groupchat' from='eve@evil.example/x' to='%s'><body>x</body></message>" % ROOM))
        await svc.handle_iq(stanza(
            "<iq type='get' id='1' from='eve@evil.example/x' to='%s'><query xmlns='%s'/></iq>" % (DOMAIN, NS_DISCO_ITEMS)))
        assert xmpp.take() == [] and not [c for c in api.calls if c[0] == "send"]

    run(go())


def test_private_message_to_occupant_is_refused():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event("seed"))
        xmpp.take()
        await svc.handle_message(stanza(
            "<message type='chat' from='%s' to='%s/%s'><body>psst</body></message>" % (FULL, ROOM, ALICE)))
        (e,) = xmpp.take()
        assert find(e, "feature-not-implemented") is not None

    run(go())


def test_subject_change_renames_the_group():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        await svc.handle_message(stanza(
            "<message type='groupchat' from='%s' to='%s'><subject>New name</subject></message>" % (FULL, ROOM)))
        assert ("rename", GUID, "New name") in api.calls
        (m,) = xmpp.take()
        assert find(m, "subject").text == "New name" and svc.rooms[GUID].name == "New name"
        api.fail = "nope"
        await svc.handle_message(stanza(
            "<message type='groupchat' from='%s' to='%s'><subject>X</subject></message>" % (FULL, ROOM)))
        assert find(xmpp.take()[0], "recipient-unavailable") is not None
        assert svc.rooms[GUID].name == "New name"

    run(go())


def test_mediated_invite_adds_a_member():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        carol = "+15550001111"
        await svc.handle_message(stanza(
            "<message from='%s' to='%s'><x xmlns='%s'><invite to='%s@%s'/></x></message>"
            % (FULL, ROOM, NS_USER, carol, DOMAIN)))
        assert ("members", GUID, [carol], []) in api.calls
        out = xmpp.take()
        assert [s.get("from") for s in out if tag(s) == "presence"] == [ROOM + "/" + carol]
        # an invite for a stranger JID is refused
        await svc.handle_message(stanza(
            "<message from='%s' to='%s'><x xmlns='%s'><invite to='x@other.example'/></x></message>"
            % (FULL, ROOM, NS_USER)))
        assert find(xmpp.take()[0], "item-not-found") is not None

    run(go())


def test_admin_iq_add_and_remove_members():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        carol = "+15550001111"
        await svc.handle_iq(stanza(
            "<iq type='set' id='a1' from='%s' to='%s'><query xmlns='http://jabber.org/protocol/muc#admin'>"
            "<item affiliation='member' jid='%s@%s'/></query></iq>" % (FULL, ROOM, carol, DOMAIN)))
        assert ("members", GUID, [carol], []) in api.calls
        assert [s for s in xmpp.take() if tag(s) == "iq"][0].get("type") == "result"
        # kick by nick
        await svc.handle_iq(stanza(
            "<iq type='set' id='a2' from='%s' to='%s'><query xmlns='http://jabber.org/protocol/muc#admin'>"
            "<item nick='%s' role='none'/></query></iq>" % (FULL, ROOM, BOB)))
        assert ("members", GUID, [], [BOB]) in api.calls
        assert [s for s in xmpp.take() if tag(s) == "iq"][0].get("type") == "result"
        # failure surfaces as an iq error
        api.fail = "cannot"
        await svc.handle_iq(stanza(
            "<iq type='set' id='a3' from='%s' to='%s'><query xmlns='http://jabber.org/protocol/muc#admin'>"
            "<item affiliation='none' jid='%s@%s'/></query></iq>" % (FULL, ROOM, ALICE, DOMAIN)))
        (e,) = [s for s in xmpp.take() if tag(s) == "iq"]
        assert e.get("type") == "error" and e.get("id") == "a3"
        # an affiliation iMessage cannot express
        await svc.handle_iq(stanza(
            "<iq type='set' id='a4' from='%s' to='%s'><query xmlns='http://jabber.org/protocol/muc#admin'>"
            "<item affiliation='admin' jid='%s@%s'/></query></iq>" % (FULL, ROOM, ALICE, DOMAIN)))
        assert find(xmpp.take()[0], "bad-request") is not None

    run(go())


def test_admin_get_lists_members():
    async def go():
        svc, xmpp, _ = make()
        await svc.on_message(event("seed"))
        xmpp.take()
        await svc.handle_iq(stanza(
            "<iq type='get' id='g' from='%s' to='%s'><query xmlns='http://jabber.org/protocol/muc#admin'>"
            "<item affiliation='member'/></query></iq>" % (FULL, ROOM)))
        (r,) = xmpp.take()
        assert sorted(i.get("nick") for i in r.iter() if tag(i) == "item") == sorted([ALICE, BOB])

    run(go())


def test_owner_config_form_and_rename_via_form():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        q = "http://jabber.org/protocol/muc#owner"
        await svc.handle_iq(stanza("<iq type='get' id='f' from='%s' to='%s'><query xmlns='%s'/></iq>" % (FULL, ROOM, q)))
        (form,) = xmpp.take()
        assert find(form, "value").text == "http://jabber.org/protocol/muc#roomconfig"
        await svc.handle_iq(stanza(
            "<iq type='set' id='f2' from='%s' to='%s'><query xmlns='%s'><x xmlns='jabber:x:data' type='submit'>"
            "<field var='muc#roomconfig_roomname'><value>Via form</value></field></x></query></iq>" % (FULL, ROOM, q)))
        assert ("rename", GUID, "Via form") in api.calls
        out = xmpp.take()
        assert [s.get("type") for s in out if tag(s) == "iq"] == ["result"]

    run(go())


def test_destroying_the_room_leaves_the_imessage_group():
    async def go():
        svc, xmpp, api = make()
        await svc.on_message(event("seed"))
        await svc.handle_presence(stanza(join_xml(nick="travis")))
        xmpp.take()
        q = "http://jabber.org/protocol/muc#owner"
        api.fail = "denied"
        await svc.handle_iq(stanza(
            "<iq type='set' id='d' from='%s' to='%s'><query xmlns='%s'><destroy><reason>bye</reason></destroy></query></iq>"
            % (FULL, ROOM, q)))
        assert find(xmpp.take()[0], "not-allowed") is not None and GUID in svc.rooms
        api.fail = None
        await svc.handle_iq(stanza(
            "<iq type='set' id='d2' from='%s' to='%s'><query xmlns='%s'><destroy><reason>bye</reason></destroy></query></iq>"
            % (FULL, ROOM, q)))
        assert ("leave", GUID) in api.calls and GUID not in svc.rooms
        out = xmpp.take()
        assert find(out[0], "destroy") is not None and out[-1].get("type") == "result"

    run(go())


# -- discovery ---------------------------------------------------------------


def test_service_disco_info_and_items_with_rsm():
    async def go():
        svc, xmpp, api = make()
        await svc.hydrate()
        other = "22222222-2222-4222-8222-222222222222"
        api.group_list.append({"chat_guid": other, "name": None, "participants": [ME, ALICE, BOB]})
        await svc.hydrate()
        xmpp.take()
        await svc.handle_iq(stanza(
            "<iq type='get' id='i' from='%s' to='%s'><query xmlns='%s'/></iq>" % (FULL, DOMAIN, NS_DISCO_INFO)))
        (info,) = xmpp.take()
        feats = {f.get("var") for f in info.iter() if tag(f) == "feature"}
        assert {"http://jabber.org/protocol/muc", NS_RSM} <= feats
        assert find(info, "identity").get("category") == "conference"
        await svc.handle_iq(stanza(
            "<iq type='get' id='j' from='%s' to='%s'><query xmlns='%s'/></iq>" % (FULL, DOMAIN, NS_DISCO_ITEMS)))
        (items,) = xmpp.take()
        jids = [i.get("jid") for i in items.iter() if tag(i) == "item"]
        assert jids == sorted([ROOM, room_jid(other, DOMAIN)])
        names = {i.get("jid"): i.get("name") for i in items.iter() if tag(i) == "item"}
        assert names[ROOM] == "Weekend" and names[room_jid(other, DOMAIN)] == ALICE + ", " + BOB
        # paging
        await svc.handle_iq(stanza(
            "<iq type='get' id='k' from='%s' to='%s'><query xmlns='%s'><set xmlns='%s'><max>1</max></set></query></iq>"
            % (FULL, DOMAIN, NS_DISCO_ITEMS, NS_RSM)))
        (p1,) = xmpp.take()
        first_page = [i.get("jid") for i in p1.iter() if tag(i) == "item"]
        assert len(first_page) == 1 and find(p1, "count").text == "2"
        await svc.handle_iq(stanza(
            "<iq type='get' id='l' from='%s' to='%s'><query xmlns='%s'><set xmlns='%s'><max>1</max><after>%s</after></set></query></iq>"
            % (FULL, DOMAIN, NS_DISCO_ITEMS, NS_RSM, first_page[0])))
        (p2,) = xmpp.take()
        assert [i.get("jid") for i in p2.iter() if tag(i) == "item"] == [j for j in jids if j != first_page[0]]

    run(go())


def test_room_disco_info_and_unknown_iq():
    async def go():
        svc, xmpp, _ = make()
        await svc.hydrate()
        xmpp.take()
        await svc.handle_iq(stanza(
            "<iq type='get' id='i' from='%s' to='%s'><query xmlns='%s'/></iq>" % (FULL, ROOM, NS_DISCO_INFO)))
        (info,) = xmpp.take()
        assert find(info, "identity").get("name") == "Weekend"
        assert "muc_nonanonymous" in {f.get("var") for f in info.iter() if tag(f) == "feature"}
        await svc.handle_iq(stanza(
            "<iq type='get' id='u' from='%s' to='%s'><ping xmlns='urn:xmpp:ping'/></iq>" % (FULL, DOMAIN)))
        assert find(xmpp.take()[0], "service-unavailable") is not None
        await svc.handle_iq(stanza(
            "<iq type='get' id='n' from='%s' to='grp-u%s@%s'><query xmlns='%s'/></iq>" % (FULL, "0" * 32, DOMAIN, NS_DISCO_INFO)))
        assert find(xmpp.take()[0], "item-not-found") is not None

    run(go())


# -- creating a group from XMPP ----------------------------------------------


def test_draft_room_becomes_an_imessage_group_on_the_first_message():
    async def go():
        svc, xmpp, api = make()
        draft = "trip@" + DOMAIN
        await svc.handle_presence(stanza(join_xml(nick="travis", room=draft)))
        out = xmpp.take()
        selfp = [s for s in out if tag(s) == "presence"][-1]
        codes = {st.get("code") for st in selfp.iter() if tag(st) == "status"}
        assert "201" in codes and "trip" in svc.drafts
        # owner is joined; invite two contacts
        for handle in (ALICE, BOB):
            local = "bob-40icloud.com" if handle == BOB else handle
            await svc.handle_message(stanza(
                "<message from='%s' to='%s'><x xmlns='%s'><invite to='%s@%s'/></x></message>"
                % (FULL, draft, NS_USER, local, DOMAIN)))
        out = xmpp.take()
        assert len([s for s in out if tag(s) == "presence"]) == 2
        # discoverable while a draft
        await svc.handle_iq(stanza(
            "<iq type='get' id='x' from='%s' to='%s'><query xmlns='%s'/></iq>" % (FULL, DOMAIN, NS_DISCO_ITEMS)))
        assert draft in [i.get("jid") for i in xmpp.take()[0].iter() if tag(i) == "item"]
        # first message creates the group
        await svc.handle_message(stanza(
            "<message type='groupchat' id='c1' from='%s' to='%s'><body>hello all</body></message>" % (FULL, draft)))
        assert ("create", sorted([ALICE, BOB]), "trip", "hello all") in api.calls
        out = xmpp.take()
        destroy = find(out[0], "destroy")
        new_room = room_jid("AAAAAAAA-0000-4000-8000-000000000001", DOMAIN)
        assert destroy.get("jid") == new_room
        assert find(out[1], "x", NS_CONFERENCE).get("jid") == new_room
        assert "trip" not in svc.drafts and svc.rooms["AAAAAAAA-0000-4000-8000-000000000001"].name == "trip"
        assert [h.text for h in svc.rooms["AAAAAAAA-0000-4000-8000-000000000001"].history] == ["hello all"]

    run(go())


def test_draft_needs_two_contacts_and_reports_create_failures():
    async def go():
        svc, xmpp, api = make()
        draft = "tiny@" + DOMAIN
        await svc.handle_presence(stanza(join_xml(nick="travis", room=draft)))
        await svc.handle_message(stanza(
            "<message from='%s' to='%s'><x xmlns='%s'><invite to='%s@%s'/></x></message>"
            % (FULL, draft, NS_USER, ALICE, DOMAIN)))
        xmpp.take()
        msg = "<message type='groupchat' from='%s' to='%s'><body>hi</body></message>" % (FULL, draft)
        await svc.handle_message(stanza(msg))
        assert find(xmpp.take()[0], "not-acceptable") is not None
        assert not [c for c in api.calls if c[0] == "create"]
        await svc.handle_message(stanza(
            "<message from='%s' to='%s'><x xmlns='%s'><invite to='bob-40icloud.com@%s'/></x></message>"
            % (FULL, draft, NS_USER, DOMAIN)))
        xmpp.take()
        api.fail = "apple said no"
        await svc.handle_message(stanza(msg))
        (e,) = xmpp.take()
        assert "apple said no" in find(e, "text").text and "tiny" in svc.drafts

    run(go())


def test_leaving_an_empty_draft_discards_it():
    async def go():
        svc, xmpp, _ = make()
        draft = "gone@" + DOMAIN
        await svc.handle_presence(stanza(join_xml(nick="travis", room=draft)))
        await svc.handle_presence(stanza(
            "<presence type='unavailable' from='%s' to='%s/travis'/>" % (FULL, draft)))
        assert "gone" not in svc.drafts

    run(go())


# -- wiring --------------------------------------------------------------------


def test_bridge_routes_group_messages_into_rooms_and_keeps_1to1_untouched(monkeypatch):
    from fakes import FakeComponentTransport
    from relay.core import Bridge
    from relay.upstream import UpstreamClient

    class Transport(FakeComponentTransport):
        def __init__(self):
            super().__init__()
            self.xmpp = FakeXmpp()

    async def go():
        transport = Transport()
        upstream = UpstreamClient("http://127.0.0.1:1")
        monkeypatch.delenv("XMPP_GROUPS_MUC", raising=False)
        bridge = Bridge(cfg(), transport, upstream)
        assert bridge.groups is not None
        assert transport.xmpp.handlers and "message" in transport.xmpp.event_handlers
        assert "presence" in transport.xmpp.event_handlers

        async def groups():
            return []

        bridge.groups.api.groups = groups  # type: ignore[method-assign]
        base = {"type": "message", "guid": "g1", "chat_guid": GUID, "from": ALICE,
                "to": [ME, BOB], "text": "hi group", "timestamp": 1, "is_group": True}
        await bridge.on_event(base)
        assert transport.sent == []  # no legacy chat line
        (invite,) = transport.xmpp.take()
        assert find(invite, "x", NS_CONFERENCE) is not None
        # 1:1 still takes the old path
        await bridge.on_event({**base, "guid": "p1", "chat_guid": "iMessage;-;" + ALICE,
                               "to": [ME], "is_group": False})
        assert transport.sent and transport.sent[0]["mtype"] == "chat"
        # the group event goes to the service
        await bridge.on_event({"type": "group", "kind": "rename", "chat_guid": GUID, "name": "N", "from": ALICE})
        assert bridge.groups.rooms[GUID].name == "N"

    run(go())


def test_disabled_flag_and_client_mode_keep_the_old_behaviour(monkeypatch):
    from fakes import FakeComponentTransport
    from relay.upstream import UpstreamClient

    class Transport(FakeComponentTransport):
        xmpp = FakeXmpp()

    monkeypatch.setenv("XMPP_GROUPS_MUC", "0")
    assert maybe_make_group_service(cfg(), Transport(), UpstreamClient("http://x")) is None
    monkeypatch.delenv("XMPP_GROUPS_MUC")
    assert maybe_make_group_service(cfg(), FakeComponentTransport(), UpstreamClient("http://x")) is None


def test_real_slixmpp_component_feeds_the_service():
    """Wire-level: real slixmpp parsing and handler dispatch, no socket."""
    import asyncio

    import slixmpp

    async def go():
        x = slixmpp.ComponentXMPP(DOMAIN, "s", "127.0.0.1", 5347)
        sent: list[str] = []
        x.send_raw = lambda data: sent.append(data)  # no connection in this test
        api = FakeApi()
        svc = GroupService(cfg(), x, api)
        svc.mine.add(ME)
        svc.attach()
        await svc.hydrate()
        x.init_parser()
        x.data_received(("<stream:stream xmlns='jabber:component:accept' "
                         "xmlns:stream='http://etherx.jabber.org/streams' from='%s' id='1'>" % DOMAIN).encode())
        x.data_received(join_xml(nick="nick").encode())
        x.data_received(("<iq type='get' id='9' from='%s' to='%s'><query xmlns='%s'/></iq>"
                         % (FULL, DOMAIN, NS_DISCO_ITEMS)).encode())
        x.data_received(("<message type='groupchat' from='%s' to='%s'><body>yo</body></message>"
                         % (FULL, ROOM)).encode())
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(sent) >= 6 and ("send", GUID, "yo") in api.calls:
                break
        parsed = [ET.fromstring(s) for s in sent]
        assert any(tag(p) == "iq" and p.get("id") == "9" and p.get("type") == "result" for p in parsed)
        assert any(tag(p) == "message" and p.get("from") == ROOM + "/nick" for p in parsed)
        assert ("send", GUID, "yo") in api.calls

    run(go())
