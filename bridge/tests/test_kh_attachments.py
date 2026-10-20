"""keelhaul driver for relay/attachments.py + relay/dedup.py (REQ-ATT-*, REQ-DEDUP-*).

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
attachments = _load("attachments")

import re
from collections import Counter

from relay.config import Config

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"
BASE = "https://files.example.org"
_STRICT = re.compile(r"/dl/[0-9a-f]{32}\?exp=[0-9]{1,12}&sig=[0-9a-f]{64}")
_POOL = "abXY01 _-.:/@+#?&=%;,'\"<>\n\r\t\\é中\U0001f600\u0000"


def _text(rng, n=16):
    return "".join(rng.choice(_POOL) for _ in range(rng.randint(0, n)))


def _cfg(**kw):
    return Config(mode="component", component_jid=DOMAIN, component_domain=DOMAIN, component_host="127.0.0.1",
                  component_port=5347, component_secret="s", component_owner=OWNER, targets=(OWNER,),
                  allowed=frozenset({OWNER}), public_file_base_url=BASE, **kw)


def _good(rng):
    return "/dl/%032x?exp=%d&sig=%064x" % (rng.getrandbits(128), rng.randint(0, 10 ** 12 - 1), rng.getrandbits(256))


def _mutate(rng, p):
    k = rng.randrange(8)
    if k == 0:
        return p + rng.choice(["\n", "\r\n", " ", "\x00", "&x=1", "#f", "/", "١"])
    if k == 1:
        return rng.choice([" ", "\n", "/x"]) + p
    if k == 2:
        i = rng.randrange(len(p))
        return p[:i] + rng.choice(["G", "A", "٣", "-", "%", "\n"]) + p[i + 1:]
    if k == 3:
        return p.replace("/dl/", "/dl//", 1)
    if k == 4:
        return p.replace("exp=", "exp=-", 1)
    if k == 5:
        return p[:-1]
    if k == 6:
        return p.upper()
    return p


# -- REQ-ATT-001: only the server's own signed /dl path ever becomes a URL ------


def prop_build_url(path, base=BASE):
    out = attachments.build_url(base, path)
    if _STRICT.fullmatch(path):
        assert out == base.rstrip("/") + path
    else:
        assert out is None, repr(path)
    if out is not None:
        assert out.isprintable() and not any(c.isspace() for c in out)
    assert attachments.build_url("", path) is None and attachments.build_url(None, path) is None


def prop_plan(rng):
    cfg = _cfg()
    url = BASE + _good(rng)
    sender = rng.choice(["+15557654321", "bob@icloud.com", "", "x", "\ud800", _text(rng, 8)])
    chat = rng.choice(["iMessage;-;+15557654321", "iMessage;+;chat123", ""])
    try:
        plan = attachments.plan_attachment_delivery(cfg, {"from": sender, "chat_guid": chat, "guid": "g"}, url)
    except UnicodeEncodeError:
        raise AssertionError("lone surrogate sender crashed planning: " + repr(sender))
    assert [p.to for p in plan] == list(cfg.targets)
    for p in plan:
        assert p.body == url and p.mtype == "chat"
        if ";+;" in chat:
            assert p.frm is None
        else:
            assert p.frm is None or (p.frm.endswith("@" + DOMAIN) and "/" not in p.frm)


# -- REQ-DEDUP-001: the replay cache is bounded and exact ------------------------


def prop_dedup(rng):
    size = rng.randint(1, 6)
    d = _load("dedup").DedupCache(size)
    model = []
    for _ in range(rng.randint(1, 40)):
        key = rng.choice([None, "", "a", "b", "c", "d", "e", "f", "g", 1, 2, 3])
        new = d.check_and_add(key)
        if key is None or key == "":
            assert new is True
            continue
        assert new == (key not in model)
        if key in model:
            model.remove(key)
        model.append(key)
        while len(model) > size:
            model.pop(0)
        assert len(d) <= size and len(d) == len(model)
        assert all(k in d for k in model)
    d.clear()
    assert len(d) == 0


def test_dedup_rejects_nonpositive_size():
    dedup = _load("dedup")
    for bad in (0, -1):
        try:
            dedup.DedupCache(bad)
        except ValueError:
            continue
        raise AssertionError("accepted size %d" % bad)


def test_regression_signed_path_with_trailing_newline_is_refused():
    good = "/dl/" + "ab" * 16 + "?exp=1700000000&sig=" + "cd" * 32
    assert attachments.build_url(BASE, good) == BASE + good
    for bad in (good + "\n", good + "\r\n", good + " ", "\n" + good):
        assert attachments.build_url(BASE, bad) is None, repr(bad)


class _Tr:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, **kw):
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(kw)


def test_on_event_flow_and_failure_accounting():
    stats = Counter()
    tr = _Tr()
    att = attachments.Attachments(_cfg(), tr, stats)
    good = "/dl/" + "ab" * 16 + "?exp=1700000000&sig=" + "cd" * 32
    asyncio.run(att.on_event({"path": good, "from": "+15557654321", "chat_guid": "iMessage;-;+1", "guid": "g", "name": "a.png"}))
    assert stats["attachments_delivered"] == 1 and tr.sent[0]["body"] == BASE + good
    oob = tr.sent[0]["elements"][0]
    assert oob.tag == "{jabber:x:oob}x" and xep.parse_oob_url(ET.fromstring("<m>" + ET.tostring(oob, encoding="unicode") + "</m>")) == BASE + good
    asyncio.run(att.on_event({"path": "/etc/passwd", "guid": "g2"}))
    assert stats["attachment_unlinked"] == 1 and len(tr.sent) == 1
    bad = attachments.Attachments(_cfg(), _Tr(fail=True), stats)
    asyncio.run(bad.on_event({"path": good, "chat_guid": "iMessage;-;+1", "from": "+15557654321"}))
    assert stats["delivery_errors"] == 1

    class G:
        async def on_attachment(self, event, url, name):
            return True
    att.groups = G()
    asyncio.run(att.on_event({"path": good, "chat_guid": "iMessage;+;c1", "from": "+1"}))
    assert len(tr.sent) == 1 and stats["attachments_delivered"] == 2


def test_seeded_properties():
    rng = random.Random(_SEED)
    for _ in range(_N * 5):
        p = _good(rng)
        prop_build_url(p)
        prop_build_url(_mutate(rng, p))
        prop_build_url(_text(rng, 30))
    for _ in range(_N):
        prop_plan(rng)
        prop_dedup(rng)


if not _TRACING:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:  # pragma: no cover
        given = None
    if given is not None:
        _S = settings(max_examples=_N * 3, deadline=None)

        @_S
        @given(st.text(max_size=120))
        def test_hyp_build_url(path):
            prop_build_url(path)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_mutated(rng):
            prop_build_url(_mutate(rng, _good(rng)))

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_plan_dedup(rng):
            prop_plan(rng)
            prop_dedup(rng)



def test_existing_suite_runs_against_this_unit():
    """Keelhaul driver only: replay the repo's own tests against the (staged) unit so coverage counts them.
    Under pytest they already run as part of the suite, so this is a no-op there."""
    if "_pytest" in sys.modules:
        return
    import pytest

    root = next(c for c in pathlib.Path(__file__).resolve().parents if (c / "relay" / "__init__.py").is_file())
    files = [str(root / "tests" / f) for f in ['test_attachments.py', 'test_dedup.py']]
    rc = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *files])
    assert rc == 0, "existing suite failed under the driver"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
