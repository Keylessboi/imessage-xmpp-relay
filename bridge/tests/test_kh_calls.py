"""keelhaul driver for relay/calls.py (REQ-CALL-*).

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
calls = _load("calls")

from relay.config import Config
from relay.upstream import UpstreamError

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"
FULL = OWNER + "/phone"
PEER = "+15557654321"
PEER_JID = PEER + "@" + DOMAIN
URL = "https://facetime.apple.com/join#v=1&p=SECRET&k=KEY"
_POOL = "abXY01 _-.:/@+#?&=%;,'\"<>\n\r\t\\é中\U0001f600\u0000\u001b\ud800￾"


def _text(rng, n=16):
    return "".join(rng.choice(_POOL) for _ in range(rng.randint(0, n)))


def _cfg():
    return Config(mode="component", component_jid=DOMAIN, component_domain=DOMAIN, component_host="127.0.0.1",
                  component_port=5347, component_secret="s", component_owner=OWNER, targets=(OWNER,),
                  allowed=frozenset({OWNER}))


class _Up:
    def __init__(self, error=None, rang=True):
        self.calls, self.error, self.rang = [], error, rang

    async def facetime_call(self, to, video=True):
        self.calls.append((to, video))
        if self.error:
            raise UpstreamError(self.error)
        return URL, self.rang, None

    async def facetime_link(self):
        self.calls.append(("link",))
        if self.error:
            raise UpstreamError(self.error)
        return URL


class _Msg:
    """The slice of a slixmpp stanza CallService reads."""

    def __init__(self, xml=None, **fields):
        self.xml = xml
        self.f = fields

    def __getitem__(self, k):
        return self.f.get(k, "")


def _svc(clock=None, error=None):
    up = _Up(error)
    sent, raw = [], []

    async def send_message(**kw):
        sent.append(kw)

    async def send_raw(xml):
        raw.append(xml)
    limiter = calls.RateLimiter(clock=clock) if clock else None
    return calls.CallService(_cfg(), up, send_message, send_raw, limiter), up, sent, raw


def _propose(sid, to=PEER_JID, frm=FULL, media="audio"):
    xml = ET.fromstring("<message xmlns='jabber:component:accept'><propose xmlns='urn:xmpp:jingle-message:0' id='x'>"
                        "<description xmlns='urn:xmpp:jingle:apps:rtp:1' media='%s'/></propose></message>" % media)
    xml.find("{urn:xmpp:jingle-message:0}propose").set("id", sid)
    return _Msg(xml, to=to, **{"from": frm}, body="")


# -- REQ-CALL-001: the rate limit holds, ids are bounded, a retry rings once ----


def prop_rate_limit(burst, window, gaps):
    t = [0.0]
    lim = calls.RateLimiter(burst=burst, window=window, clock=lambda: t[0])
    allowed = []
    for gap in gaps:
        t[0] += gap
        if lim.allow():
            allowed.append(t[0])
        # reference: no more than `burst` allowed inside any half-open window of length `window`
        recent = [a for a in allowed if t[0] - a < window]
        assert len(recent) <= max(burst, 0), (burst, window, gaps, allowed)
    # liveness: with burst>0 an event a full window after the previous allowed one is allowed again
    if burst > 0:
        t[0] += window * 2
        assert lim.allow()


def prop_post_link_cannot_hammer(rng):
    t = [100.0]
    svc, up, sent, raw = _svc(clock=lambda: t[0])
    n = rng.randint(1, 12)
    for i in range(n):
        asyncio.run(svc.post_link(PEER_JID, "command"))
        t[0] += rng.choice([0.0, 0.1, 5.0])
    rung = [c for c in up.calls if c and c[0] != "link"]
    span = t[0] - 100.0
    assert len(rung) <= calls.RATE_BURST * (int(span // calls.RATE_WINDOW) + 1), (len(rung), span)
    assert len(sent) == n                                   # the owner always hears back
    assert all(m["to"] == OWNER for m in sent)              # the link goes to the owner only
    limited = [m for m in sent if "too many requests" in m["body"]]
    assert all(URL not in m["body"] for m in limited)
    assert len(limited) == n - len(rung)


def prop_seen_bounded_and_retry_once(rng):
    svc, up, sent, raw = _svc(clock=lambda: 10 ** 9)
    svc._limiter = calls.RateLimiter(burst=10 ** 6)
    sids = ["s%d" % rng.randint(0, 400) for _ in range(rng.randint(1, 40))]
    first = {}
    for sid in sids:
        before = len(up.calls)
        assert asyncio.run(svc.on_message(_propose(sid))) is True
        if sid in first and sid in svc._seen:
            assert len(up.calls) == before             # a client retry gets a decline, never a second ring
        first.setdefault(sid, True)
        assert len(svc._seen) <= calls._SEEN_MAX
    for sid in ("", ""):
        asyncio.run(svc.on_message(_propose(sid)))     # id-less proposals are never remembered
    assert len(svc._seen) <= calls._SEEN_MAX


# -- REQ-CALL-002: stanzas are well formed and carry the data verbatim ---------


def _legal(t):
    return xep.xml_safe(t).replace("\r", "\n")


def prop_builders(frm, to, sid, iq_id):
    frm, to, sid, iq_id = _legal(frm), _legal(to), _legal(sid), _legal(iq_id)
    r = ET.fromstring(calls.build_reject(frm=frm, to=to, sid=sid))
    assert r.get("from") == frm.replace("\n", " ").replace("\t", " ") or "\n" in frm or "\t" in frm
    rej = r.find("{urn:xmpp:jingle-message:0}reject")
    assert rej is not None and (rej.get("id") == sid or "\n" in sid or "\t" in sid)
    assert len(list(r.iter())) == 4
    i = ET.fromstring(calls.build_iq_result(frm=frm, to=to, iq_id=iq_id))
    assert i.tag == "iq" and i.get("type") == "result" and len(i) == 0
    t = ET.fromstring(calls.build_session_terminate(frm=frm, to=to, sid=sid))
    assert t.find("{urn:xmpp:jingle:1}jingle").get("action") == "session-terminate"
    d = ET.fromstring(calls.build_disco_info(frm=frm, to=to, iq_id=iq_id, category=sid, ctype=iq_id))
    feats = [f.get("var") for f in d.iter("{http://jabber.org/protocol/disco#info}feature")]
    assert feats == list(calls.DISCO_FEATURES)


def prop_stanza_payloads_total(rng):
    tags = ["{urn:xmpp:jingle-message:0}propose", "{urn:xmpp:jingle-message:0}", "{urn:xmpp:jingle:1}jingle", "{urn:xmpp:jingle:apps:rtp:1}description", "x", ""]
    root = ET.Element("m")
    for _ in range(rng.randint(0, 5)):
        el = ET.SubElement(root, rng.choice(tags))
        el.set(rng.choice(["id", "sid", "action", "media"]), _text(rng, 6))
        if rng.random() < 0.5:
            ET.SubElement(el, rng.choice(tags)).set("media", _text(rng, 4))
        if rng.random() < 0.2:
            root.append(ET.Comment("c"))
    calls.jingle_message_kind(root)
    calls.jingle_iq_info(root)
    assert calls.jingle_message_kind(None) is None and calls.jingle_iq_info(None) is None
    assert calls.is_command(_text(rng, 10)) in (True, False)


def test_regression_control_characters_in_a_session_id_never_reach_the_wire_raw():
    svc, up, sent, raw = _svc(clock=lambda: 0.0)
    asyncio.run(svc._raw(calls.build_reject(frm=PEER_JID, to=FULL, sid="a\x00b\x1bc\ud800d")))
    assert len(raw) == 1
    ET.fromstring(raw[0])
    assert 'id="abcd"' in raw[0] or "id='abcd'" in raw[0]


# -- REQ-CALL-003: only the component's own contacts can start a call ---------


def prop_foreign_targets_never_ring(rng):
    svc, up, sent, raw = _svc(clock=lambda: 0.0)
    for _ in range(rng.randint(1, 6)):
        to = rng.choice(["evil@example.org", "+15551112222@evil.example", PEER + "@" + DOMAIN + ".evil", "grp-u" + "0" * 32 + "@" + DOMAIN,
                         "", "@" + DOMAIN, "x" + PEER_JID, _text(rng, 12)])
        asyncio.run(svc.on_message(_propose("s" + _text(rng, 3), to=to)))
        asyncio.run(svc.on_message(_Msg(None, to=to, body="facetime", **{"from": FULL})))
    assert up.calls == [] and sent == []


def test_calls_flow_and_text_command():
    svc, up, sent, raw = _svc(clock=lambda: 0.0)
    assert asyncio.run(svc.on_message(_propose("s1", media="video")))
    assert up.calls == [(PEER, True)] and "Ringing the contact now." in sent[0]["body"] and URL in sent[0]["body"]
    assert len(raw) == 1 and "<reject" in raw[0]
    assert asyncio.run(svc.on_message(_propose("s1")))                         # retry: declined again, not rung again
    assert up.calls == [(PEER, True)] and len(raw) == 2
    assert asyncio.run(svc.on_message(_Msg(None, to=DOMAIN, body=" /FaceTime ", **{"from": FULL})))
    assert up.calls[-1] == ("link",) and "NOT rung" in sent[-1]["body"]
    assert asyncio.run(svc.on_message(_Msg(None, to=PEER_JID, body="hello", **{"from": FULL}))) is False
    svc2, up2, sent2, raw2 = _svc(clock=lambda: 0.0, error="boom")
    asyncio.run(svc2.on_message(_propose("s9")))
    assert sent2[0]["body"] == "FaceTime unavailable: boom" and URL not in sent2[0]["body"]


def test_iq_and_disco():
    svc, up, sent, raw = _svc(clock=lambda: 0.0)
    xml = ET.fromstring("<iq xmlns='jabber:component:accept'><jingle xmlns='urn:xmpp:jingle:1' action='session-initiate' sid='j1'/></iq>")
    iq = _Msg(xml, to=PEER_JID, id="i1", type="set", **{"from": FULL})
    assert asyncio.run(svc.on_iq(iq)) is True
    assert any("session-terminate" in r for r in raw) and len(up.calls) == 1
    other = _Msg(xml, to=PEER_JID, id="i1", type="get", **{"from": FULL})
    assert asyncio.run(svc.on_iq(other)) is False
    dxml = ET.fromstring("<iq xmlns='jabber:component:accept'><query xmlns='http://jabber.org/protocol/disco#info'/></iq>")
    assert asyncio.run(svc.on_disco_info(_Msg(dxml, to=PEER_JID, id="d1", type="get", **{"from": FULL}))) is True
    assert asyncio.run(svc.on_disco_info(_Msg(dxml, to="evil@x", id="d1", type="get", **{"from": FULL}))) is False
    nxml = ET.fromstring("<iq xmlns='jabber:component:accept'><query xmlns='http://jabber.org/protocol/disco#info' node='n'/></iq>")
    assert asyncio.run(svc.on_disco_info(_Msg(nxml, to=DOMAIN, id="d1", type="get", **{"from": FULL}))) is False


def test_seeded_properties():
    rng = random.Random(_SEED)
    for _ in range(_N * 3):
        prop_rate_limit(rng.randint(0, 4), rng.choice([1.0, 60.0]), [rng.choice([0.0, 0.0, 0.5, 30.0, 61.0]) for _ in range(rng.randint(1, 15))])
        prop_builders(_text(rng), _text(rng), _text(rng), _text(rng))
        prop_stanza_payloads_total(rng)
    for _ in range(max(10, _N // 3)):
        prop_post_link_cannot_hammer(rng)
        prop_seen_bounded_and_retry_once(rng)
        prop_foreign_targets_never_ring(rng)


if not _TRACING:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:  # pragma: no cover
        given = None
    if given is not None:
        _S = settings(max_examples=_N * 3, deadline=None)

        @_S
        @given(st.integers(0, 5), st.sampled_from([1.0, 60.0]), st.lists(st.sampled_from([0.0, 0.5, 30.0, 61.0]), min_size=1, max_size=20))
        def test_hyp_rate_limit(b, w, gaps):
            prop_rate_limit(b, w, gaps)

        @_S
        @given(st.text(), st.text(), st.text(), st.text())
        def test_hyp_builders(a, b, c, d):
            prop_builders(a, b, c, d)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_hammer(rng):
            prop_post_link_cannot_hammer(rng)
            prop_seen_bounded_and_retry_once(rng)
            prop_foreign_targets_never_ring(rng)
            prop_stanza_payloads_total(rng)



def test_existing_suite_runs_against_this_unit():
    """Keelhaul driver only: replay the repo's own tests against the (staged) unit so coverage counts them.
    Under pytest they already run as part of the suite, so this is a no-op there."""
    if "_pytest" in sys.modules:
        return
    import pytest

    root = next(c for c in pathlib.Path(__file__).resolve().parents if (c / "relay" / "__init__.py").is_file())
    files = [str(root / "tests" / f) for f in ['test_calls.py']]
    rc = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *files])
    assert rc == 0, "existing suite failed under the driver"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
