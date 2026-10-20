"""keelhaul driver for relay/xep.py (REQ-XEP-*).

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

_POOL = ("abcXYZ019 _-.:/@+#?&=%;,'\"<>\n\r\t\\"
         "é中\U0001f600❤️‼‍‮￼\u0000\u0008\u000b\u001b\ud800￾￿")


def _text(rng, maxlen=24):
    return "".join(rng.choice(_POOL) for _ in range(rng.randint(0, maxlen)))


def _legal(text):
    # XML parsers normalise CR/CRLF to LF in character data; that is not under test here.
    return xep.xml_safe(text).replace("\r", "\n")


def _wrap(elements):
    msg = ET.Element("{jabber:client}message")
    for el in elements:
        msg.append(el)
    return msg


def _reparse(elements):
    """Serialize and parse again, as the wire would."""
    return ET.fromstring(ET.tostring(_wrap(elements), encoding="unicode"))


# -- REQ-XEP-001: builders emit well-formed, injection-free XML ---------------


def prop_builders_roundtrip(a, b, emojis):
    a, b = _legal(a), _legal(b)
    emojis = [_legal(e) for e in emojis]
    cases = [
        xep.received_receipt(a), xep.displayed_marker(a), xep.reactions(a, emojis),
        xep.correction(a), xep.retraction(a), [xep.oob(a, b or None)],
        xep.reply(a, b or None, None)[1], [xep.receipt_request(), xep.markable()],
        xep.chat_state("composing"),
    ]
    prefix, rep = xep.reply(a, b or None, "line1\n" + b)
    cases.append(rep)
    for els in cases:
        tree = _reparse(els)  # must be well formed
        assert len(tree) == len(els)
    f = xep.parse_features(_reparse(xep.received_receipt(a)))
    assert f.received_id == a
    f = xep.parse_features(_reparse(xep.displayed_marker(a)))
    assert f.marker == (("displayed", a) if a else None)
    f = xep.parse_features(_reparse(xep.reactions(a, emojis)))
    expect = []
    for e in emojis:
        if e.strip() and e.strip() not in expect:
            expect.append(e.strip())
    assert (f.reactions == tuple(expect)) if a else (f.reactions is None)
    assert xep.parse_features(_reparse(xep.correction(a))).replace_id == (a or None) or not a
    assert xep.parse_features(_reparse(xep.retraction(a))).retract_id == (a or None) or not a
    url = xep.parse_oob_url(_reparse([xep.oob(a, b or None)]))
    assert url == (a.strip() or None)
    # reply: body prefix + typed text strips back to exactly the typed text
    typed = _legal(b)
    feats = xep.parse_features(_reparse(rep))
    assert xep.strip_fallback(prefix + typed, feats) == typed


def prop_text_never_becomes_markup(text):
    """Hostile text only ever lands as character data/attribute values."""
    text = _legal(text)
    for els in (xep.reactions("id", [text]), [xep.oob(text, text)], xep.received_receipt(text)):
        tree = _reparse(els)
        for node in tree.iter():
            assert node.tag.startswith("{"), node.tag
        assert len(list(tree.iter())) == 1 + sum(len(list(e.iter())) for e in els)


def prop_xml_safe(text):
    out = xep.xml_safe(text)
    assert xep.xml_safe(out) == out  # idempotent
    ET.fromstring("<a>" + out.replace("&", "&amp;").replace("<", "&lt;") + "</a>")
    out.encode("utf-8")  # no lone surrogates left
    assert all(ch in out for ch in text if ch in "abcXYZ019 \n\t")


def prop_tapback_roundtrip():
    for name, emoji in xep.TAPBACK_EMOJI.items():
        assert xep.emoji_to_reaction(emoji) == (name, None)
        assert xep.reaction_to_emoji(name, None) == emoji
    assert xep.emoji_to_reaction("\U0001f389") == ("emoji", "\U0001f389")
    assert xep.reaction_to_emoji("emoji", None) is None
    assert xep.reaction_to_emoji("emoji", "x") == "x"


def prop_chat_state_closed():
    for s in xep.CHAT_STATES:
        assert xep.chat_state(s)
    for bad in ("", "typing", "COMPOSING", "x}y"):
        try:
            xep.chat_state(bad)
        except ValueError:
            continue
        raise AssertionError("accepted unknown chat state " + repr(bad))


# -- REQ-XEP-002: parsing/stripping is total and never eats typed text --------


def prop_strip_fallback_confined(body, ranges):
    """Removed characters lie inside the union of the declared (valid) ranges."""
    body = _legal(body)
    fb = tuple(("urn:x", s, e) for s, e in ranges)
    feats = xep.Features(fallbacks=fb)
    out = xep.strip_fallback(body, feats)
    covered = set()
    whole = any(s is None or e is None for _n, s, e in fb)
    for _n, s, e in fb:
        if s is not None and e is not None and 0 <= s <= e <= len(body):
            covered.update(range(s, e))
    if whole:
        assert out == ""
        return
    expect = "".join(ch for i, ch in enumerate(body) if i not in covered)
    assert out == expect, (body, ranges, out, expect)


def prop_parse_total(rng):
    tags = ["{urn:xmpp:reactions:0}reactions", "{urn:xmpp:fallback:0}fallback", "{urn:xmpp:fallback:0}body",
            "{urn:xmpp:reply:0}reply", "{urn:xmpp:chat-markers:0}displayed", "{urn:xmpp:receipts}received",
            "{http://jabber.org/protocol/chatstates}composing", "{http://jabber.org/protocol/chatstates}",
            "{urn:xmpp:message-retract:1}retract", "{urn:xmpp:message-correct:0}replace", "plain", ""]
    msg = ET.Element("message")
    if rng.random() < 0.5:
        msg.set("id", _legal(_text(rng)))
    for _ in range(rng.randint(0, 6)):
        el = ET.SubElement(msg, rng.choice(tags))
        for k in ("id", "for", "start", "end", "to"):
            if rng.random() < 0.5:
                el.set(k, rng.choice(["", "x", "-1", "99999999999999999999", "3", "0", "١"]))
        if rng.random() < 0.3:
            ET.SubElement(el, rng.choice(tags)).text = _legal(_text(rng))
        if rng.random() < 0.2:
            msg.append(ET.Comment("c"))
            msg.append(ET.ProcessingInstruction("pi"))
    feats = xep.parse_features(msg)
    _ = feats.interesting
    xep.strip_fallback(_legal(_text(rng, 30)), feats)
    xep.parse_oob_url(msg)
    assert xep.parse_features(None) == xep.Features()
    assert xep.parse_oob_url(None) is None


# -- named regression inputs (found by the attack slot) -----------------------


def test_regression_overlapping_fallback_ranges_do_not_eat_typed_text():
    body = "> q\nhello world"
    feats = xep.Features(fallbacks=(("urn:x", 0, 5), ("urn:x", 3, 8)))
    assert xep.strip_fallback(body, feats) == body[8:]
    assert xep.strip_fallback("abcdefghij", xep.Features(fallbacks=(("n", 0, 5), ("n", 3, 8)))) == "ij"


def test_regression_comment_child_does_not_crash_parse():
    msg = ET.Element("message")
    msg.append(ET.Comment("x"))
    msg.append(ET.ProcessingInstruction("pi"))
    assert xep.parse_features(msg) == xep.Features()


def test_regression_control_chars_and_lone_surrogate_are_stripped():
    assert xep.xml_safe("a\x00b\x0bc\ud800d￾e\x1f") == "abcde"
    assert xep.xml_safe("tab\there\nnl\rcr ❤️ \U0001f600") == "tab\there\nnl\rcr ❤️ \U0001f600"


# -- deterministic cases + seeded loop (the keelhaul driver body) -------------


def test_seeded_properties():
    rng = random.Random(_SEED)
    prop_tapback_roundtrip()
    prop_chat_state_closed()
    for _ in range(_N):
        prop_builders_roundtrip(_text(rng), _text(rng), [_text(rng, 4) for _ in range(rng.randint(0, 3))])
        prop_text_never_becomes_markup(_text(rng))
        prop_xml_safe(_text(rng))
        body = _text(rng, 12)
        rs = [(rng.randint(-2, 14), rng.randint(-2, 14)) for _ in range(rng.randint(0, 3))]
        prop_strip_fallback_confined(body, rs)
        prop_parse_total(rng)


if not _TRACING:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:  # pragma: no cover
        given = None
    if given is not None:
        _S = settings(max_examples=_N, deadline=None)

        @_S
        @given(st.text(), st.text(), st.lists(st.text(max_size=4), max_size=3))
        def test_hyp_builders(a, b, emojis):
            prop_builders_roundtrip(a, b, emojis)

        @_S
        @given(st.text())
        def test_hyp_markup(text):
            prop_text_never_becomes_markup(text)
            prop_xml_safe(text)

        @_S
        @given(st.text(max_size=12), st.lists(st.tuples(st.integers(-2, 14), st.integers(-2, 14)), max_size=4))
        def test_hyp_strip(body, ranges):
            prop_strip_fallback_confined(body, ranges)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_parse_total(rng):
            prop_parse_total(rng)



def test_existing_suite_runs_against_this_unit():
    """Keelhaul driver only: replay the repo's own tests against the (staged) unit so coverage counts them.
    Under pytest they already run as part of the suite, so this is a no-op there."""
    if "_pytest" in sys.modules:
        return
    import pytest

    root = next(c for c in pathlib.Path(__file__).resolve().parents if (c / "relay" / "__init__.py").is_file())
    files = [str(root / "tests" / f) for f in ['test_msgfeatures.py']]
    rc = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *files])
    assert rc == 0, "existing suite failed under the driver"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
