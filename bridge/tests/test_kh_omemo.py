"""keelhaul driver for relay/component_omemo.py (+ omemo2.py policy) (REQ-OMEMO-*).

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
omemo2 = _load("omemo2")
component_omemo = _load("component_omemo")

import tempfile

from relay.config import Config
from relay.omemo2 import NoOmemoDevices, Omemo2Error

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"


def _cfg(policy, store):
    return Config(mode="component", component_jid=DOMAIN, component_domain=DOMAIN, component_host="127.0.0.1",
                  component_port=5347, component_secret="s", component_owner=OWNER, targets=(OWNER,),
                  allowed=frozenset({OWNER}), omemo=True, omemo_store=store, omemo_policy=policy)


class _Manager:
    def __init__(self):
        self.fail_encrypt = None
        self.fail_decrypt = None

    async def encrypt_multi(self, body, to_jid):
        if self.fail_encrypt:
            raise self.fail_encrypt
        return [ET.Element("{urn:xmpp:omemo:2}encrypted")]

    async def decrypt_any(self, element, from_jid):
        if self.fail_decrypt:
            raise self.fail_decrypt
        return "plain"


class _FakePeer:
    def __init__(self, manager):
        self.omemo = manager
        self.lock = asyncio.Lock()


async def _mk(policy, path):
    om = component_omemo.ComponentOmemo(_cfg(policy, path), fetcher=None)
    await om.start()
    manager = _Manager()
    peer = _FakePeer(manager)

    async def fake_peer(jid):
        return peer
    om._peer = fake_peer
    return om, manager


_JIDS = ["+15557654321@imessage.example.org", "+15557654321@IMessage.Example.org/res", "bob-40icloud.com@imessage.example.org", "+15551112222@imessage.example.org"]


def prop_policy_state_machine(rng):
    async def go():
        with tempfile.TemporaryDirectory() as d:
            path = d + "/store.json"
            policy = rng.choice(["required", "opportunistic"])
            om, mgr = await _mk(policy, path)
            sticky = set()
            was_denied = set()
            for _ in range(rng.randint(1, 25)):
                jid = rng.choice(_JIDS)
                key = component_omemo.bare_jid(jid)
                op = rng.choice(["enc_ok", "enc_none", "enc_fail", "dec_ok", "dec_fail", "check", "restart"])
                if op == "enc_ok":
                    mgr.fail_encrypt = None
                    await om.encrypt("hi", peer_jid=jid, to_jid=OWNER)
                    if policy == "opportunistic":
                        sticky.add(key)
                elif op in ("enc_none", "enc_fail"):
                    mgr.fail_encrypt = NoOmemoDevices("none") if op == "enc_none" else Omemo2Error("lookup failed")
                    try:
                        await om.encrypt("hi", peer_jid=jid, to_jid=OWNER)
                    except Omemo2Error:
                        pass
                    else:
                        raise AssertionError("failed encrypt returned")
                elif op == "dec_ok":
                    mgr.fail_decrypt = None
                    assert await om.decrypt(ET.Element("e"), peer_jid=jid, from_jid=OWNER) == "plain"
                    if policy == "opportunistic":
                        sticky.add(key)
                elif op == "dec_fail":
                    mgr.fail_decrypt = Omemo2Error("bad")
                    try:
                        await om.decrypt(ET.Element("e"), peer_jid=jid, from_jid=OWNER)
                    except Omemo2Error:
                        pass
                elif op == "restart":
                    om, mgr = await _mk(policy, path)       # a new process reading the same store
                allowed = await om.plaintext_allowed(jid)
                assert allowed == (policy == "opportunistic" and key not in sticky), (policy, op, jid, sticky)
                assert await om.is_sticky(jid) == (key in sticky)
                if policy == "required":
                    assert allowed is False                  # never plaintext under `required`
                if key in was_denied:
                    assert not allowed                       # sticky never downgrades
                if policy == "opportunistic" and key in sticky:
                    was_denied.add(key)
    asyncio.run(go())


def test_sticky_keys_ignore_case_and_resource_and_survive_restart():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            path = d + "/s.json"
            om, mgr = await _mk("opportunistic", path)
            assert await om.plaintext_allowed("Peer@Imessage.Example.org/phone") is True
            await om.encrypt("x", peer_jid="peer@imessage.example.org/a", to_jid=OWNER)
            for spelling in ("PEER@imessage.example.org", "peer@IMESSAGE.example.org/zzz", " peer@imessage.example.org "):
                assert await om.plaintext_allowed(spelling) is False, spelling
            assert await om.plaintext_allowed("other@imessage.example.org") is True
            om2, _ = await _mk("opportunistic", path)
            assert await om2.plaintext_allowed("peer@imessage.example.org") is False
            om3, _ = await _mk("required", path)
            assert await om3.plaintext_allowed("other@imessage.example.org") is False
    asyncio.run(go())


def test_notice_rate_limit_and_is_peer():
    om = component_omemo.ComponentOmemo(_cfg("required", "/nonexistent/x"), fetcher=None)
    assert om.notice_due("a@x") is True and om.notice_due("a@x") is False and om.notice_due("b@x") is True
    om._last_notice["a@x"] -= om.NOTICE_INTERVAL + 1
    assert om.notice_due("a@x") is True
    assert om.is_peer(DOMAIN) and om.is_peer("+15557654321@" + DOMAIN + "/r") and not om.is_peer("evil@other.example") and not om.is_peer("")
    assert not om.is_peer("grp-u" + "0" * 32 + "@evil." + DOMAIN)


def test_pep_surface_is_read_only_and_unknown_nodes_refused():
    assert component_omemo.PUBSUB_NS + "#publish" not in component_omemo.PEP_FEATURES
    q = component_omemo.disco_info_result(None)
    vars_ = [f.get("var") for f in q if f.tag.endswith("feature")]
    assert not any(v.endswith("#publish") for v in vars_)
    for node in ("evil", "", "eu.siacs.conversations.axolotl.bundles:1"):
        try:
            q = component_omemo.disco_info_result(node or None) if node else component_omemo.disco_info_result(None)
        except component_omemo.PepError:
            continue
    try:
        component_omemo.disco_info_result("not-a-node")
    except component_omemo.PepError as exc:
        assert exc.condition == "item-not-found"
    else:
        raise AssertionError("unknown node answered")
    items = component_omemo.items_result("n", "1", ET.Element("p"))
    assert items.find(".//{http://jabber.org/protocol/pubsub}item").get("id") == "1"


def test_nodevices_is_the_only_plaintext_trigger():
    assert issubclass(NoOmemoDevices, Omemo2Error)
    for exc in (Omemo2Error("x"), TimeoutError(), ValueError()):
        assert not isinstance(exc, NoOmemoDevices)


def test_seeded_properties():
    rng = random.Random(_SEED)
    for _ in range(max(6, _N // 4)):
        prop_policy_state_machine(rng)


if not _TRACING:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:  # pragma: no cover
        given = None
    if given is not None:
        _S = settings(max_examples=max(20, _N // 3), deadline=None)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_policy(rng):
            prop_policy_state_machine(rng)



def test_existing_suite_runs_against_this_unit():
    """Keelhaul driver only: replay the repo's own tests against the (staged) unit so coverage counts them.
    Under pytest they already run as part of the suite, so this is a no-op there."""
    if "_pytest" in sys.modules:
        return
    import pytest

    root = next(c for c in pathlib.Path(__file__).resolve().parents if (c / "relay" / "__init__.py").is_file())
    files = [str(root / "tests" / f) for f in ['test_component_omemo.py', 'test_omemo2.py']]
    rc = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *files])
    assert rc == 0, "existing suite failed under the driver"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
