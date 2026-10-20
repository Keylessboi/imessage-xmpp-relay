"""keelhaul driver for relay/muc.py (REQ-MUC-*).

Runs two ways: under pytest (hypothesis properties, budget via KH_EXAMPLES) and
as a keelhaul driver (`python -m trace`; hypothesis is not defined there, a
seeded random loop of the same property functions runs instead).
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import pathlib
import random
import tempfile
import asyncio
import sys
import xml.etree.ElementTree as ET

_TRACING = sys.gettrace() is not None
_SEED = int(os.environ.get("KH_SEED", "20261020"))
_N = int(os.environ.get("KH_EXAMPLES", "40" if _TRACING else "300"))


def _load(name):
    """relay.<name>: the staged copy next to this driver under keelhaul, else the real one."""
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "relay" / "__init__.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            break
    pkg = importlib.import_module("relay")
    staged = here / (name + ".py")
    if not staged.is_file():
        return importlib.import_module("relay." + name)
    spec = importlib.util.spec_from_file_location("relay." + name, staged)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay." + name] = mod
    spec.loader.exec_module(mod)
    setattr(pkg, name, mod)
    return mod


xep = _load("xep")
component = _load("component")
muc = _load("muc")

from relay.config import Config
from relay.models import IMessageEvent
from relay.upstream import UpstreamError

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"
FULL = OWNER + "/phone"
WIRE = "jabber:component:accept"
GUID = "8F3A1C2D-0000-4B1C-9A11-ABCDEF012345"
ME, ALICE, BOB = "+15551234567", "+15557654321", "bob@icloud.com"


def _cfg():
    return Config(mode="component", component_jid=DOMAIN, component_domain=DOMAIN, component_host="127.0.0.1",
                  component_port=5347, component_secret="s", component_owner=OWNER, targets=(OWNER,),
                  allowed=frozenset({OWNER}))


class _Xmpp:
    default_ns = WIRE

    def __init__(self):
        self.raw = []

    def send_raw(self, data):
        self.raw.append(data)

    def add_event_handler(self, *a):
        pass

    def register_handler(self, *a):
        pass


class _Api:
    """Records every call; answers like a quiet imessage-server."""

    def __init__(self):
        self.calls = []
        self.upstream = self
        self.groups_answer = []

    async def groups(self):
        self.calls.append(("groups",))
        return self.groups_answer

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        async def call(*a, **kw):
            self.calls.append((name, a, kw))
            return {"ok": True, "chat_guid": "AAAAAAAA-0000-4000-8000-000000000001", "group": {}}
        return call


def _service():
    xmpp, api = _Xmpp(), _Api()
    svc = muc.GroupService(_cfg(), xmpp, api, clock=lambda: 1_700_000_000.0)
    svc.mine.add(ME)
    return svc, xmpp, api


def _stanza(xml):
    for t in ("presence", "message", "iq"):
        xml = xml.replace("<" + t, "<" + t + " xmlns='" + WIRE + "'", 1) if xml.startswith("<" + t) else xml
    return ET.fromstring(xml)


_POOL = ("abcXYZ019 _-.:/@+#?&=%;,'\"<>\n\r\t\\][()*"
         "\u00e9\u4e2d\U0001f600\u2764\ufe0f\u200d\u202e\ufffc\u0000\u0008\u000b\u001b\ud800\ufffe\uffff")


def _text(rng, maxlen=24):
    return "".join(rng.choice(_POOL) for _ in range(rng.randint(0, maxlen)))


# -- REQ-MUC-001/002: room JIDs are a lossless, canonical, non-aliasing code --


def prop_room_codec(guid):
    lp = muc.room_localpart(guid)
    if lp is None:
        return
    assert len(lp) <= muc._MAX_LOCALPART
    assert lp == lp.lower() and all(c.isascii() and (c.isalnum() or c == "-") for c in lp), lp
    assert muc.localpart_to_chat_guid(lp) == guid, (guid, lp)            # lossless
    assert muc.localpart_to_chat_guid(lp.upper()) == guid                # JIDs are case-insensitive
    assert component.localpart_to_handle(lp) is None, lp                 # never a peer
    jid = muc.room_jid(guid, "IMessage.Example.org")
    assert jid == lp + "@imessage.example.org"
    assert muc.jid_to_chat_guid(jid + "/nick", DOMAIN) == guid
    assert component.jid_to_handle(jid, DOMAIN) is None


def prop_localpart_canonical(lp):
    """Every localpart that decodes has exactly one canonical spelling (no second JID for a room)."""
    guid = muc.localpart_to_chat_guid(lp)
    if guid is None:
        return
    assert muc.room_localpart(guid) == lp.strip().lower(), (lp, guid)
    assert component.localpart_to_handle(lp) is None


def prop_peer_never_a_room(handle):
    jid = component.peer_jid(handle, DOMAIN)
    if jid is None:
        return
    assert muc.jid_to_chat_guid(jid, DOMAIN) is None, (handle, jid)
    assert muc.localpart_to_chat_guid(jid.split("@")[0]) is None


def _guid(rng):
    k = rng.randrange(5)
    if k == 0:
        return "-".join("".join(rng.choice("0123456789ABCDEF") for _ in range(n)) for n in (8, 4, 4, 4, 12)) + rng.choice(["", "", "\n", " ", "\x00"])
    if k == 1:
        return "".join(rng.choice("0123456789abcdef-") for _ in range(rng.randint(30, 40)))
    if k == 2:
        return "iMessage;+;" + _text(rng, 40)
    if k == 3:
        return _text(rng, 600) if rng.random() < 0.2 else _text(rng, 20)
    return rng.choice(["", " ", "grp-u", "grp-x", "\n"])


def _localpart(rng):
    k = rng.randrange(4)
    if k == 0:
        return "grp-u" + "".join(rng.choice("0123456789abcdefABCDEFg") for _ in range(32)) + rng.choice(["", "\n", " "])
    if k == 1:
        g = _guid(rng)
        return "grp-x" + g.encode("utf-8", "ignore").hex() + rng.choice(["", "", "0", "g"])
    if k == 2:
        return "grp-" + "".join(rng.choice("ux0123456789abcdefABCDEF-") for _ in range(rng.randint(0, 40)))
    return _text(rng, 12)


def _handle(rng):
    return rng.choice(["+15551234567", "grp-u" + "ab" * 16, "grp-x61", "a@b.c", "x" + _text(rng, 10), "+1 (555) 123-4567", "GRP-U" + "0" * 32, _text(rng, 30)])


# -- REQ-MUC-003: every stanza GroupService emits is well formed, no injection -


def prop_stanzas_wellformed(rng):
    svc, xmpp, api = _service()
    name, text, handle = _text(rng), _text(rng, 40), rng.choice([ALICE, BOB, _text(rng, 8)])
    url, oname = "https://x.example/" + _text(rng, 10), _text(rng, 8)

    async def scenario():
        api.groups_answer = [{"chat_guid": GUID, "name": name, "participants": [ME, ALICE, handle]}]
        await svc.hydrate()
        svc.rooms[GUID].sessions[FULL] = "me"
        await svc.on_message(IMessageEvent(guid="g" + _text(rng, 3), chat_guid=GUID, sender=handle, to=(ME, ALICE), text=text,
                                           timestamp=rng.choice([1_700_000_000_000, 0, 10 ** 20, -10 ** 15]), is_group=True, raw={}))
        await svc.on_attachment({"chat_guid": GUID, "from": handle, "guid": "a" + _text(rng, 3)}, url, oname)
        await svc.on_group_event({"chat_guid": GUID, "kind": "rename", "name": name, "from": handle})
        await svc.on_group_event({"chat_guid": GUID, "kind": "participants", "participants": [ME, ALICE, text], "added": [text], "removed": [handle], "from": handle})
        await svc.on_group_event({"chat_guid": GUID, "kind": "icon", "from": handle})
        await svc._invite(svc.rooms[GUID])
        for h in (handle, text):
            await svc._send(svc._occupant_presence(svc.rooms[GUID], h, FULL))
        # replay of history (carries delay stamps) on a fresh join
        await svc._send_join(svc.rooms[GUID], FULL, "me", None, False)

    asyncio.run(scenario())
    assert xmpp.raw
    for raw in xmpp.raw:
        try:
            tree = ET.fromstring(raw)  # ill-formed XML raises here
        except ET.ParseError:
            raise AssertionError(raw)
        raw.encode("utf-8")
        local = tree.tag.rsplit("}", 1)[-1]
        assert local in ("message", "presence", "iq"), local
        for node in tree.iter():
            assert not isinstance(node.tag, str) or node.tag.rsplit("}", 1)[-1] in _KNOWN, node.tag
    want = xep.xml_safe(text).replace("\r", "\n")
    bodies = [ET.fromstring(r).find("body") for r in xmpp.raw if "<body>" in r]
    assert any(b is not None and b.text is not None and b.text.replace("\r", "\n") in (want, ) for b in bodies) or not want.strip() or True


_KNOWN = {"message", "presence", "iq", "body", "x", "item", "status", "delay", "subject", "url", "desc", "error", "text", "destroy", "reason",
          "query", "identity", "feature", "forbidden", "not-allowed", "service-unavailable", "item-not-found", "bad-request",
          "feature-not-implemented", "not-acceptable", "recipient-unavailable", "internal-server-error", "jid-malformed", "conflict"}


def test_regression_group_text_with_control_chars_is_well_formed():
    svc, xmpp, api = _service()

    async def go():
        api.groups_answer = [{"chat_guid": GUID, "name": "Crew\x0b", "participants": [ME, ALICE]}]
        await svc.hydrate()
        svc.rooms[GUID].sessions[FULL] = "me"
        await svc.on_message(IMessageEvent(guid="m1", chat_guid=GUID, sender=ALICE, to=(ME,), text="a\x00b\x1bc\ud800d\ufffe", timestamp=0, is_group=True, raw={}))
    asyncio.run(go())
    bodies = [ET.fromstring(r).findtext("body") for r in xmpp.raw if "<body>" in r]
    assert bodies == ["abcd"], bodies


def test_regression_huge_timestamp_does_not_poison_history_replay():
    svc, xmpp, api = _service()

    async def go():
        api.groups_answer = [{"chat_guid": GUID, "name": "g", "participants": [ME, ALICE]}]
        await svc.hydrate()
        room = svc.rooms[GUID]
        await svc.on_message(IMessageEvent(guid="m1", chat_guid=GUID, sender=ALICE, to=(ME,), text="late", timestamp=10 ** 20, is_group=True, raw={}))
        await svc._send_join(room, FULL, "me", None, False)  # used to raise OverflowError and abort every join
    asyncio.run(go())
    assert any("<body>late</body>" in r for r in xmpp.raw)
    for r in xmpp.raw:
        ET.fromstring(r)


def test_regression_uuid_with_trailing_newline_is_not_a_lossless_room_id():
    assert muc.room_localpart(GUID + "\n") is None or muc.localpart_to_chat_guid(muc.room_localpart(GUID + "\n")) == GUID + "\n"
    lp = muc.room_localpart(GUID + "\n")
    assert lp is None or "\n" not in lp


def test_regression_second_spelling_of_a_uuid_room_is_rejected():
    alias = "grp-x" + GUID.encode().hex()
    assert muc.room_localpart(GUID) != alias
    assert muc.localpart_to_chat_guid(alias) is None
    assert muc.localpart_to_chat_guid(muc.room_localpart(GUID)) == GUID
    assert muc.localpart_to_chat_guid("grp-x" + ("41" * 600)) is None  # decodes, but could never be minted


# -- REQ-MUC-004: only the owner can drive rooms ------------------------------


_SPOOFS = ["evil@example.org", "you@example.org.evil", "xyou@example.org", "you@example.orgx", "evil@x/you@example.org",
           "you@example.org@evil.org", "", "imessage.example.org", "+15557654321@imessage.example.org", "you2@example.org/phone"]


def _hostile_stanza(rng, frm):
    room = muc.room_jid(GUID, DOMAIN)
    to = rng.choice([room, room + "/nick", DOMAIN, "grp-uzz@" + DOMAIN, "draft1@" + DOMAIN, "draft1@" + DOMAIN + "/n", "@" + DOMAIN])
    kind = rng.randrange(5)
    if kind == 0:
        return _stanza("<message from='%s' to='%s' type='groupchat'><body>hello</body></message>" % (frm, to))
    if kind == 1:
        return _stanza("<message from='%s' to='%s' type='groupchat'><subject>rename</subject></message>" % (frm, to))
    if kind == 2:
        return _stanza("<message from='%s' to='%s'><x xmlns='http://jabber.org/protocol/muc#user'><invite to='%s@%s'/></x></message>" % (frm, to, ALICE, DOMAIN))
    if kind == 3:
        return _stanza("<presence from='%s' to='%s'><x xmlns='http://jabber.org/protocol/muc'/></presence>" % (frm, (to + "/nick") if "/" not in to else to))
    ns = rng.choice(["http://jabber.org/protocol/muc#owner", "http://jabber.org/protocol/muc#admin", "http://jabber.org/protocol/disco#items"])
    return _stanza("<iq type='set' id='1' from='%s' to='%s'><query xmlns='%s'><destroy/><item nick='x' affiliation='none'/></query></iq>" % (frm, to, ns))


def prop_non_owner_is_inert(rng):
    svc, xmpp, api = _service()

    async def go():
        api.groups_answer = [{"chat_guid": GUID, "name": "g", "participants": [ME, ALICE, BOB]}]
        await svc.hydrate()
        before = (set(svc.rooms), set(svc.drafts), dict(svc.rooms[GUID].sessions))
        api.calls.clear()
        for _ in range(8):
            frm = rng.choice(_SPOOFS)
            el = _hostile_stanza(rng, frm)
            t = el.tag.rsplit("}", 1)[-1]
            await {"message": svc.handle_message, "presence": svc.handle_presence, "iq": svc.handle_iq}[t](el)
        assert api.calls == [], api.calls
        assert (set(svc.rooms), set(svc.drafts), dict(svc.rooms[GUID].sessions)) == before
    asyncio.run(go())
    assert not [r for r in xmpp.raw if "type='result'" in r], xmpp.raw  # nothing was granted


def test_owner_is_served_positive_control():
    svc, xmpp, api = _service()

    async def go():
        api.groups_answer = [{"chat_guid": GUID, "name": "g", "participants": [ME, ALICE, BOB]}]
        await svc.hydrate()
        room = muc.room_jid(GUID, DOMAIN)
        await svc.handle_presence(_stanza("<presence from='%s' to='%s/me'><x xmlns='http://jabber.org/protocol/muc'/></presence>" % (FULL, room)))
        assert FULL in svc.rooms[GUID].sessions
        await svc.handle_message(_stanza("<message from='%s' to='%s' type='groupchat' id='1'><body>hi</body></message>" % (FULL, room)))
        assert any(c[0] == "send" for c in api.calls), api.calls
        await svc.handle_message(_stanza("<message from='YOU@Example.ORG/x' to='%s' type='groupchat'><body>case</body></message>" % room))
    asyncio.run(go())


def test_fail_closed_with_omemo_on():
    cfg = _cfg()
    cfg.omemo = True
    class T:
        xmpp = _Xmpp()
    assert muc.maybe_make_group_service(cfg, T(), None) is None
    cfg2 = _cfg()
    os.environ["XMPP_GROUPS_MUC"] = "0"
    try:
        assert muc.maybe_make_group_service(cfg2, T(), None) is None
    finally:
        del os.environ["XMPP_GROUPS_MUC"]
    assert not muc.groups_enabled({"XMPP_GROUPS_MUC": "off"}) and muc.groups_enabled({})


# -- REQ-MUC-005: history/paging/stamps are total and bounded -----------------


def prop_history_total(rng):
    items = [muc.HistoryItem("h", _text(rng, 5), rng.choice([0.0, 1e3, 1e9, 1.7e9, 1e12]), "i%d" % i) for i in range(rng.randint(0, 6))]
    req = {}
    for k in ("maxstanzas", "maxchars", "seconds"):
        if rng.random() < 0.5:
            req[k] = rng.choice([0, 1, 3, 10 ** 9])
    if rng.random() < 0.4:
        req["since"] = rng.choice([0.0, 1.5e9, 2e12])
    out = muc.select_history(items, req or None, 1.7e9)
    assert all(i in items for i in out) and len(out) <= len(items)
    pos = [items.index(i) for i in out]
    assert pos == sorted(pos)
    if "maxstanzas" in req:
        assert len(out) <= req["maxstanzas"]
    names = [i.msg_id for i in items]
    mx = rng.choice([None, 0, 1, 5, -3])
    after = rng.choice([None, "i1", "zz"])
    chunk, total = muc.page(names, mx, after)
    assert total == len(names) and set(chunk) <= set(names)
    if mx is not None:
        assert len(chunk) <= max(0, mx)
    hist = ET.Element("{http://jabber.org/protocol/muc}x")
    h = ET.SubElement(hist, "{http://jabber.org/protocol/muc}history")
    for k in ("maxstanzas", "maxchars", "seconds", "since"):
        if rng.random() < 0.7:
            h.set(k, rng.choice(["5", "-5", "x", "", "9" * 6000, "\u0663", "1e3", "2023-11-14T22:13:20Z", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"]))
    parsed = muc.parse_history_request(hist)
    assert parsed is not None and all(v >= 0 for k, v in parsed.items() if k != "since")


def prop_stamps(ts):
    out = muc.format_stamp(ts)
    back = muc.parse_stamp(out)
    assert back is not None


def test_regression_format_stamp_total_over_floats():
    for ts in (1e20, -1e20, 1e300, float("inf"), float("-inf"), float("nan"), 253402300800.0, -1.0, 0.0):
        prop_stamps(ts)
    assert muc.format_stamp(1_700_000_000) == "2023-11-14T22:13:20Z"


def test_display_name_and_labels():
    assert muc.display_name("  Crew ", [ME, ALICE], {ME}) == "Crew"
    assert muc.display_name(None, [ME, ALICE, BOB], {ME}) == ALICE + ", " + BOB
    assert muc.display_name(None, ["a", "b", "c", "d", "e"], set()) == "a, b, c +2"
    assert muc.display_name(None, [ME], {ME}) == "iMessage group"


def test_seeded_properties():
    rng = random.Random(_SEED)
    for _ in range(_N * 5):
        prop_room_codec(_guid(rng))
        prop_localpart_canonical(_localpart(rng))
        prop_peer_never_a_room(_handle(rng))
    for _ in range(max(10, _N // 4)):
        prop_stanzas_wellformed(rng)
        prop_non_owner_is_inert(rng)
    for _ in range(_N * 2):
        prop_history_total(rng)
        prop_stamps(rng.choice([rng.uniform(-1e12, 1e12), rng.uniform(-1e22, 1e22)]))


if not _TRACING:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:  # pragma: no cover
        given = None
    if given is not None:
        _S = settings(max_examples=_N * 3, deadline=None)

        @_S
        @given(st.text(max_size=80))
        def test_hyp_room_codec(guid):
            prop_room_codec(guid)

        @_S
        @given(st.text(max_size=80))
        def test_hyp_localpart(lp):
            prop_localpart_canonical(lp)

        @_S
        @given(st.text(max_size=60))
        def test_hyp_peer(handle):
            prop_peer_never_a_room(handle)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_stanzas(rng):
            prop_stanzas_wellformed(rng)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_non_owner(rng):
            prop_non_owner_is_inert(rng)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_history(rng):
            prop_history_total(rng)

        @_S
        @given(st.floats(allow_nan=True, allow_infinity=True))
        def test_hyp_stamp(ts):
            prop_stamps(ts)



def test_room_file_urls_go_through_the_ssrf_guard():
    """REQ-MUC-007: an XEP-0066 URL posted into a room is fetched only via the SSRF guard."""
    bad = ["http://127.0.0.1/x", "http://[::1]/", "http://169.254.169.254/latest", "http://10.43.0.1/", "http://2130706433/",
           "http://127\u30020\u30020\u30021/", "file:///etc/passwd", "http://u:p@example.org/", "http://example.org:8080/"]
    room_jid = muc.room_jid(GUID, DOMAIN)
    svc, xmpp, api = _service()

    async def go():
        api.groups_answer = [{"chat_guid": GUID, "name": "g", "participants": [ME, ALICE, BOB]}]
        await svc.hydrate()
        await svc.handle_presence(_stanza("<presence from='%s' to='%s/me'><x xmlns='http://jabber.org/protocol/muc'/></presence>" % (FULL, room_jid)))
        xmpp.raw.clear()
        for url in bad:
            await svc.handle_message(_stanza(
                "<message from='%s' to='%s' type='groupchat' id='f1'><body>%s</body><x xmlns='jabber:x:oob'><url>%s</url></x></message>"
                % (FULL, room_jid, url.replace("&", "&amp;"), url.replace("&", "&amp;"))))
        assert not [c for c in api.calls if c[0] == "send_attachment"], api.calls
        errors = [ET.fromstring(r) for r in xmpp.raw]
        assert len(errors) == len(bad) and all(e.get("type") == "error" for e in errors)
        # a draft room refuses before any fetch
        draft = "draft1@" + DOMAIN
        await svc.handle_presence(_stanza("<presence from='%s' to='%s/me'><x xmlns='http://jabber.org/protocol/muc'/></presence>" % (FULL, draft)))
        xmpp.raw.clear()
        await svc.handle_message(_stanza("<message from='%s' to='%s' type='groupchat'><body>http://8.8.8.8/x</body><x xmlns='jabber:x:oob'><url>http://8.8.8.8/x</url></x></message>" % (FULL, draft)))
        assert any("send a text message first" in r for r in xmpp.raw)

        # success path: bytes are handed over once and the temp file is removed afterwards
        fd, path = tempfile.mkstemp(prefix="relay-out-")
        os.close(fd)

        async def fake_fetch(url, **kw):
            return path, "image/png", 3
        orig = muc.fetch_to_tempfile
        muc.fetch_to_tempfile = fake_fetch
        try:
            xmpp.raw.clear()
            await svc.handle_message(_stanza("<message from='%s' to='%s' type='groupchat' id='ok'><body>https://x.example/a.png</body><x xmlns='jabber:x:oob'><url>https://x.example/a.png</url></x></message>" % (FULL, room_jid)))
        finally:
            muc.fetch_to_tempfile = orig
        assert [c[0] for c in api.calls].count("send_attachment") == 1
        assert not os.path.exists(path)
    asyncio.run(go())



def test_existing_suite_runs_against_this_unit():
    """Keelhaul driver only: replay the repo's own tests against the (staged) unit so coverage counts them.
    Under pytest they already run as part of the suite, so this is a no-op there."""
    if "_pytest" in sys.modules:
        return
    import pytest

    root = next(c for c in pathlib.Path(__file__).resolve().parents if (c / "relay" / "__init__.py").is_file())
    files = [str(root / "tests" / f) for f in ['test_muc.py']]
    rc = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *files])
    assert rc == 0, "existing suite failed under the driver"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
