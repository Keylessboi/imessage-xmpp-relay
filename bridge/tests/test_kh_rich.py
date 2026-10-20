"""keelhaul driver for relay/rich.py (REQ-RICH-*).

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
rich = _load("rich")

from collections import Counter

from relay.config import Config
from relay.models import IMessageEvent, IncomingXmpp

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"
FFFC = "￼"
_POOL = "abXY01 _-.[]():;\n\té中\U0001f600￼￼\u0000"


def _text(rng, n=14):
    return "".join(rng.choice(_POOL) for _ in range(rng.randint(0, n)))


def _cfg():
    return Config(mode="component", component_jid=DOMAIN, component_domain=DOMAIN, component_host="127.0.0.1",
                  component_port=5347, component_secret="s", component_owner=OWNER, targets=(OWNER,),
                  allowed=frozenset({OWNER}))


# -- REQ-RICH-001: the guid <-> xmpp-id index is bounded and never lies -------


def prop_index(rng):
    size = rng.randint(1, 6)
    idx = rich.MessageIndex(maxsize=size)
    live = {}                                   # guid -> rec (model)
    order = []                                  # recency of remember
    for _ in range(rng.randint(1, 40)):
        guid = rng.choice(["g%d" % i for i in range(9)] + [""])
        xid = rng.choice([None, "", "x1", "x2", "x3", "g1", "g2"])
        rec = rich.MsgRec(guid=guid, peer="p", text="t", outbound=rng.random() < 0.5, xmpp_id=xid)
        idx.remember(rec)
        if guid:
            live.pop(guid, None)
            live[guid] = rec
            if guid in order:
                order.remove(guid)
            order.append(guid)
            while len(order) > size:
                live.pop(order.pop(0), None)
        assert len(idx._by_guid) <= size
        assert len(idx._by_xid) <= len(idx._by_guid) + 0 or len(idx._by_xid) <= size
        for g, r in live.items():
            assert idx.by_guid(g) is r
        for x, g in idx._by_xid.items():           # no dangling or lying xid entry
            assert g in idx._by_guid and idx._by_guid[g].xmpp_id == x, (x, g)
        for x in {r.xmpp_id for r in live.values() if r.xmpp_id}:
            holders = [r for r in live.values() if r.xmpp_id == x]
            got = idx.by_xmpp_id(x)
            assert got is not None and got.xmpp_id == x, (x, [r.guid for r in holders])
    assert idx.by_guid(None) is None and idx.by_xmpp_id(None) is None and idx.by_xmpp_id("") is None


def prop_reactions(rng):
    size = rng.randint(1, 5)
    rs = rich.ReactionSets(maxsize=size)
    for _ in range(rng.randint(1, 30)):
        g, w, e = rng.choice("abc"), rng.choice("xy"), rng.choice(["a", "b", "c"])
        out = rs.apply(g, w, e, rng.random() < 0.6)
        assert len(out) == len(set(out))
        out.append("mutation")
        assert "mutation" not in rs.get(g, w)         # callers get copies
        assert len(rs._sets) <= size
        for k, v in rs._sets.items():
            assert v and len(v) == len(set(v))        # empty sets are not stored
    rs.set("a", "x", [])
    assert rs.get("a", "x") == []


def prop_render(rng):
    text = _text(rng)
    atts = []
    for _ in range(rng.randint(0, 3)):
        atts.append({"name": rng.choice(["", "a.png", FFFC, "x" + FFFC, None, 3]), "mime": rng.choice(["", "image/png", FFFC]),
                     "sticker": rng.random() < 0.3})
    out = rich.render_attachments(text, atts)
    assert FFFC not in out, (text, atts, out)           # the glyph never reaches the user
    n_f = text.count(FFFC)
    n_ph = out.count("[attachment") + out.count("[sticker")
    if n_f or atts:
        assert n_ph >= max(n_f, len(atts)), (text, atts, out)
    if not n_f and not atts:
        assert out == text
    short = rich.short(text, 10)
    assert len(short) <= 10 and "\n" not in short


def prop_tapback(rng):
    for reaction in ["heart", "like", "emoji", "sticker", "weird", ""]:
        for enable in (True, False):
            t = rich.tapback_text(reaction, rng.choice([None, "\U0001f389", ""]), enable, rng.choice([None, "hi", ""]))
            assert isinstance(t, str) and t


# -- REQ-RICH-002: receipts, edits and reactions follow the index, never spoof -


class _Transport:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)


class _Upstream:
    def __init__(self):
        self.actions = []

    async def action(self, kind, **fields):
        self.actions.append((kind, fields))
        return {"ok": True}


def _rf():
    tr, up, notes = _Transport(), _Upstream(), []

    async def notify(msg, text):
        notes.append(text)
    return rich.RichFeatures(_cfg(), tr, up, notify, Counter()), tr, up, notes


def prop_events_total(rng):
    rf, tr, up, notes = _rf()
    rf.index.remember(rich.MsgRec(guid="G1", peer="+15557654321", text="hello", outbound=True, xmpp_id="x1", owner=OWNER,
                                  want_received=True, want_displayed=True))
    rf.index.remember(rich.MsgRec(guid="G2", peer="+15557654321", text="in", outbound=False, xmpp_id="G2"))
    events = []
    for _ in range(rng.randint(1, 8)):
        etype = rng.choice(["delivered", "read", "typing", "reaction", "edit", "unsend", "message_meta", "bogus"])
        ev = {"from": rng.choice(["+15557654321", "bob@icloud.com", "", "\ud800" if False else "x"]),
              "chat_guid": rng.choice(["iMessage;-;+1555", "iMessage;+;chat1", ""]),
              "guid": rng.choice(["G1", "G2", "zz", "", None]), "to_guid": rng.choice(["G1", "G2", "", None]),
              "reaction": rng.choice(["heart", "emoji", "sticker", "?", None]), "emoji": rng.choice([None, "\U0001f389", 5]),
              "enable": rng.choice([True, False, None, 0]), "typing": rng.choice([True, False, None]),
              "text": rng.choice(["new", "", None, FFFC]), "part": rng.choice([0, 1, None, "0"])}
        events.append((etype, ev))
    for etype, ev in events:
        asyncio.run(rf.on_event(etype, ev))
    for kw in tr.sent:
        assert kw["to"] == OWNER and kw["mtype"] == "chat"
        frm = kw["frm"]
        assert frm is None or (frm.endswith("@" + DOMAIN) and "/" not in frm), frm   # never a foreign From
        assert FFFC not in kw["body"]


def test_receipts_only_for_owner_outbound_messages():
    rf, tr, up, notes = _rf()
    rf.index.remember(rich.MsgRec(guid="G1", peer="+15557654321", text="hello", outbound=True, xmpp_id="x1", owner=OWNER, want_received=True))
    rf.index.remember(rich.MsgRec(guid="G2", peer="+15557654321", text="in", outbound=False, xmpp_id="G2", want_received=True))
    asyncio.run(rf.on_event("delivered", {"guid": "G2", "from": "+15557654321"}))
    assert tr.sent == []
    asyncio.run(rf.on_event("delivered", {"guid": "G1", "from": "+15557654321"}))
    assert len(tr.sent) == 1 and tr.sent[0]["to"] == OWNER and tr.sent[0]["frm"] == "+15557654321@" + DOMAIN
    asyncio.run(rf.on_event("read", {"guid": "G1", "from": "+15557654321"}))   # owner not markable: no marker
    assert len(tr.sent) == 1


def test_xmpp_side_cannot_edit_or_unsend_inbound_messages():
    rf, tr, up, notes = _rf()
    rf.index.remember(rich.MsgRec(guid="G2", peer="+15557654321", text="in", outbound=False, xmpp_id="G2"))
    msg = IncomingXmpp(from_jid=OWNER + "/p", to_jid="+15557654321@" + DOMAIN, body="evil", msg_id="m", replace_id="G2")
    assert asyncio.run(rf.on_xmpp(msg, "+15557654321"))
    msg2 = IncomingXmpp(from_jid=OWNER + "/p", to_jid="+15557654321@" + DOMAIN, body="", msg_id="m2", retract_id="G2")
    assert asyncio.run(rf.on_xmpp(msg2, "+15557654321"))
    assert up.actions == [] and len(notes) == 2


def test_regression_object_replacement_glyph_in_attachment_name_is_scrubbed():
    out = rich.render_attachments("see " + FFFC, [{"name": "a" + FFFC + "b", "mime": "image/png"}])
    assert FFFC not in out and "[attachment: ab (image/png)]" in out
    assert FFFC not in rich.attachment_placeholder({"name": FFFC})


def test_regression_shared_xmpp_id_does_not_orphan_the_other_record():
    idx = rich.MessageIndex(maxsize=8)
    idx.remember(rich.MsgRec(guid="G1", peer="p", text="", outbound=False, xmpp_id="X"))
    idx.remember(rich.MsgRec(guid="G2", peer="p", text="", outbound=True, xmpp_id="X"))
    idx.remember(rich.MsgRec(guid="G1", peer="p", text="", outbound=False, xmpp_id="Y"))   # G1 moves on
    assert idx.by_xmpp_id("X").guid == "G2"            # used to be dropped: G1's cleanup deleted G2's mapping
    assert idx.by_xmpp_id("Y").guid == "G1"


def test_regression_non_numeric_part_does_not_raise():
    rf, tr, up, notes = _rf()
    asyncio.run(rf.on_event("edit", {"guid": "G1", "from": "+15557654321", "text": "t", "part": "x"}))
    asyncio.run(rf.on_event("unsend", {"guid": "G1", "from": "+15557654321", "part": "x"}))
    assert len(tr.sent) == 2


def test_decorate_and_remember():
    rf, tr, up, notes = _rf()
    rf.note_meta({"guid": "M1", "attachments": [{"name": "a.png", "mime": "image/png"}], "reply_to": None})
    ev = IMessageEvent(guid="M1", chat_guid="c", sender="+15557654321", to=(), text=FFFC, is_group=False)
    msg, extras = rf.decorate(ev)
    assert msg.text == "[attachment: a.png (image/png)]" and extras.msg_id == "M1"
    rf.remember_inbound(msg)
    assert rf.index.by_guid("M1").text.startswith("[attachment")
    for i in range(300):
        rf.note_meta({"guid": "g%d" % i})
    assert len(rf._meta) <= 256
    rf.note_meta({"guid": ""})
    out = IncomingXmpp(from_jid=OWNER + "/p", to_jid="x", body="b", msg_id="o1", receipt_request=True, markable=True)
    rf.record_outbound(out, "+1555", "b", {"guid": "OG"})
    rf.record_outbound(out, "+1555", "b", {"nope": 1})
    rf.record_outbound(out, "+1555", "b", None)
    assert rf.index.by_xmpp_id("o1").guid == "OG" and rf.reply_guid(IncomingXmpp(from_jid="a", to_jid="b", body="", msg_id="z", reply_id="o1")) == "OG"


def test_seeded_properties():
    rng = random.Random(_SEED)
    for _ in range(_N * 3):
        prop_index(rng)
        prop_reactions(rng)
        prop_render(rng)
        prop_tapback(rng)
    for _ in range(max(10, _N // 2)):
        prop_events_total(rng)


if not _TRACING:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:  # pragma: no cover
        given = None
    if given is not None:
        _S = settings(max_examples=_N * 3, deadline=None)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_index(a):
            prop_index(a)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_reactions(a):
            prop_reactions(a)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_render(a):
            prop_render(a)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_events_total(a):
            prop_events_total(a)



def test_existing_suite_runs_against_this_unit():
    """Keelhaul driver only: replay the repo's own tests against the (staged) unit so coverage counts them.
    Under pytest they already run as part of the suite, so this is a no-op there."""
    if "_pytest" in sys.modules:
        return
    import pytest

    root = next(c for c in pathlib.Path(__file__).resolve().parents if (c / "relay" / "__init__.py").is_file())
    files = [str(root / "tests" / f) for f in ['test_msgfeatures.py', 'test_inbound.py']]
    rc = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *files])
    assert rc == 0, "existing suite failed under the driver"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
