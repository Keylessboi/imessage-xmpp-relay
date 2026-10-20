"""OMEMO in component mode: per-peer identities, the PEP responder, policy, persistence.

Offline.  The crypto is real (python-omemo, Twomemo + Oldmemo).  The owner is a
second python-omemo manager whose PEP lookups for the component's peer JIDs are
answered by ComponentOmemo.serve_items -- the same code path an IQ takes -- and
whose own device list/bundles live in an in-memory dict.  ComponentOmemo's
fetches of the owner's PEP are answered from that dict.  This proves the bridge
against python-omemo; it is NOT interop with Conversations/Dino/Gajim.
"""
from __future__ import annotations

import errno
import json
import os
from xml.etree import ElementTree as ET

import pytest

from conftest import run

from relay.component import ComponentTransport
from relay.component_omemo import (
    PUBSUB_NS,
    ComponentOmemo,
    PepError,
    disco_info_result,
)
from relay.config import Config, ConfigError
from relay.omemo2 import (
    BUNDLE_NODE,
    DEVICE_LIST_NODE,
    LEGACY_BUNDLE_NODE_PREFIX,
    LEGACY_DEVICE_LIST_NODE,
    InMemoryPepStore,
    JsonFileStorage,
    NoOmemoDevices,
    Omemo2,
    Omemo2Error,
)

DOMAIN = "imessage.example.org"
OWNER = "you@example.org"
PEER_A = "+15557654321@" + DOMAIN
PEER_B = "+15550001111@" + DOMAIN
LEGACY_TAG = "{eu.siacs.conversations.axolotl}encrypted"
OMEMO2_TAG = "{urn:xmpp:omemo:2}encrypted"


def cfg_for(tmp_path, **overrides) -> Config:
    values = dict(
        mode="component", component_jid=DOMAIN, component_domain=DOMAIN,
        component_host="127.0.0.1", component_port=5347, component_secret="s",
        component_owner=OWNER, targets=(OWNER,), allowed=frozenset({OWNER}),
        omemo=True, omemo_store=str(tmp_path / "omemo" / "store.json"),
    )
    values.update(overrides)
    return Config(**values)


class OwnerWorld:
    """The owner's PEP (dicts) and the fetcher ComponentOmemo uses to reach it."""

    def __init__(self) -> None:
        self.omemo2 = InMemoryPepStore()
        self.legacy = InMemoryPepStore()
        self.hide_omemo2 = False       # the peer side sees a legacy-only owner
        self.hide_peer_omemo2 = False  # the owner sees a legacy-only peer
        self.fetch_log: list = []
        self.unreachable = False

    async def fetch_item_payload(self, *, frm, to, node, item_id=None):
        self.fetch_log.append((frm, to, node, item_id))
        if self.unreachable:
            raise Omemo2Error("timed out fetching " + node)
        if node == DEVICE_LIST_NODE:
            return None if self.hide_omemo2 else await self.omemo2.fetch_device_list(to)
        if node == LEGACY_DEVICE_LIST_NODE:
            return await self.legacy.fetch_device_list(to)
        if node == BUNDLE_NODE:
            return await self.omemo2.fetch_bundle(to, int(item_id))
        if node.startswith(LEGACY_BUNDLE_NODE_PREFIX):
            return await self.legacy.fetch_bundle(to, int(node[len(LEGACY_BUNDLE_NODE_PREFIX):]))
        return None


class ServedPep:
    """The owner's PepStore: own data in dicts, peer JIDs answered by the responder."""

    def __init__(self, world: OwnerWorld, co: ComponentOmemo, legacy: bool) -> None:
        self.world, self.co, self.legacy = world, co, legacy
        self.own = world.legacy if legacy else world.omemo2

    async def _serve(self, jid, node, ids):
        try:
            pubsub = await self.co.serve_items(jid, node, ids)
        except PepError:
            return None
        item = pubsub.find("{" + PUBSUB_NS + "}items/{" + PUBSUB_NS + "}item")
        return item[0]

    async def fetch_device_list(self, jid):
        if self.co.is_peer(jid):
            if not self.legacy and self.world.hide_peer_omemo2:
                return None
            return await self._serve(jid, LEGACY_DEVICE_LIST_NODE if self.legacy else DEVICE_LIST_NODE, [])
        return await self.own.fetch_device_list(jid)

    async def fetch_bundle(self, jid, device_id):
        if self.co.is_peer(jid):
            if self.legacy:
                return await self._serve(jid, LEGACY_BUNDLE_NODE_PREFIX + str(device_id), [])
            return await self._serve(jid, BUNDLE_NODE, [str(device_id)])
        return await self.own.fetch_bundle(jid, device_id)

    async def publish_device_list(self, jid, element):
        await self.own.publish_device_list(jid, element)

    async def publish_bundle(self, jid, device_id, element):
        await self.own.publish_bundle(jid, device_id, element)

    async def delete_bundle(self, jid, device_id):
        await self.own.delete_bundle(jid, device_id)


def new_owner(world, co, tmp_path, name="owner.json"):
    return Omemo2(
        own_bare_jid=OWNER, store_path=str(tmp_path / name),
        pep=ServedPep(world, co, False), legacy_pep=ServedPep(world, co, True),
    )


async def start_pair(tmp_path, **cfg_overrides):
    world = OwnerWorld()
    co = ComponentOmemo(cfg_for(tmp_path, **cfg_overrides), fetcher=world)
    await co.start()
    owner = new_owner(world, co, tmp_path)
    await owner.start()
    return world, co, owner


async def close_all(co, owner):
    await owner.close()
    await co.close()


def clear_owner_devices(world):
    world.omemo2.device_lists.clear()
    world.legacy.device_lists.clear()


def namespaces(elements):
    return sorted(e.tag[1:].split("}")[0] for e in elements)


# ---------------------------------------------------------------- responder


def test_responder_serves_device_list_and_bundles_for_both_backends(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        try:
            devices = await co.serve_items(PEER_A, DEVICE_LIST_NODE, [])
            item = devices.find("{" + PUBSUB_NS + "}items/{" + PUBSUB_NS + "}item")
            assert item.get("id") == "current"
            assert devices.find("{" + PUBSUB_NS + "}items").get("node") == DEVICE_LIST_NODE
            (device,) = list(item[0])
            device_id = device.get("id")

            bundle = await co.serve_items(PEER_A, BUNDLE_NODE, [device_id])
            assert bundle.find("{" + PUBSUB_NS + "}items/{" + PUBSUB_NS + "}item").get("id") == device_id
            await co.serve_items(PEER_A, BUNDLE_NODE, [])  # no item id: the single device

            legacy_devices = await co.serve_items(PEER_A, LEGACY_DEVICE_LIST_NODE, ["current"])
            assert legacy_devices.find("{" + PUBSUB_NS + "}items").get("node") == LEGACY_DEVICE_LIST_NODE
            legacy_bundle = await co.serve_items(PEER_A, LEGACY_BUNDLE_NODE_PREFIX + device_id, [])
            assert legacy_bundle.find("{" + PUBSUB_NS + "}items/{" + PUBSUB_NS + "}item") is not None
        finally:
            await close_all(co, owner)

    run(scenario())


def test_responder_errors(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        try:
            await co.serve_items(PEER_A, DEVICE_LIST_NODE, [])  # create the identity
            cases = [
                ("stranger@example.net", DEVICE_LIST_NODE, [], "service-unavailable"),
                (PEER_A, "urn:example:other", [], "item-not-found"),
                (PEER_A, DEVICE_LIST_NODE, ["old"], "item-not-found"),
                (PEER_A, BUNDLE_NODE, ["12345"], "item-not-found"),
                (PEER_A, LEGACY_BUNDLE_NODE_PREFIX + "12345", [], "item-not-found"),
                (PEER_A, BUNDLE_NODE, ["1", "2"], "item-not-found"),
            ]
            for jid, node, ids, condition in cases:
                with pytest.raises(PepError) as excinfo:
                    await co.serve_items(jid, node, ids)
                assert excinfo.value.condition == condition, (jid, node, ids)
        finally:
            await close_all(co, owner)

    run(scenario())


def test_is_peer_matches_domain_and_generated_jids_only(tmp_path):
    co = ComponentOmemo(cfg_for(tmp_path), fetcher=OwnerWorld())
    assert co.is_peer(DOMAIN)
    assert co.is_peer(PEER_A + "/resource")
    assert not co.is_peer("+15557654321@other.example")
    assert not co.is_peer("plainword@" + DOMAIN)
    assert not co.is_peer("")


def test_disco_info_is_read_only_pep():
    query = disco_info_result(None)
    features = {f.get("var") for f in query if f.tag.endswith("feature")}
    assert PUBSUB_NS + "#retrieve-items" in features
    assert PUBSUB_NS + "#publish" not in features  # every write is refused
    assert [i.get("type") for i in query if i.tag.endswith("identity")] == ["pep"]
    assert disco_info_result(DEVICE_LIST_NODE).get("node") == DEVICE_LIST_NODE
    with pytest.raises(PepError):
        disco_info_result("urn:example:other")


# --------------------------------------------------------- crypto round trips


def test_owner_to_peer_and_back_omemo2_preferred(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        try:
            sent = await owner.encrypt_multi("hello peer", to_jid=PEER_A)
            assert namespaces(sent) == ["urn:xmpp:omemo:2"]
            assert await co.decrypt(sent[0], peer_jid=PEER_A, from_jid=OWNER + "/phone") == "hello peer"

            back = await co.encrypt("hello owner é你", peer_jid=PEER_A, to_jid=OWNER + "/phone")
            assert namespaces(back) == ["urn:xmpp:omemo:2"]
            assert await owner.decrypt_any(back[0], from_jid=PEER_A) == "hello owner é你"
            # The fetches of the owner's PEP were made *as the peer JID*.
            assert {entry[0] for entry in world.fetch_log} == {PEER_A}
        finally:
            await close_all(co, owner)

    run(scenario())


def test_legacy_only_round_trip_both_directions(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        try:
            world.hide_omemo2 = True       # the peer side sees only the owner's legacy device
            world.hide_peer_omemo2 = True  # the owner sees only the peer's legacy device
            back = await co.encrypt("legacy hello", peer_jid=PEER_A, to_jid=OWNER)
            assert namespaces(back) == ["eu.siacs.conversations.axolotl"]
            assert await owner.decrypt_any(back[0], from_jid=PEER_A) == "legacy hello"

            sent = await owner.encrypt_multi("legacy back", to_jid=PEER_A)
            assert namespaces(sent) == ["eu.siacs.conversations.axolotl"]
            assert await co.decrypt(sent[0], peer_jid=PEER_A, from_jid=OWNER) == "legacy back"
        finally:
            await close_all(co, owner)

    run(scenario())


def test_each_peer_jid_has_its_own_identity(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        try:
            a = await co.serve_items(PEER_A, DEVICE_LIST_NODE, [])
            b = await co.serve_items(PEER_B, DEVICE_LIST_NODE, [])
            id_a = a.find(".//{urn:xmpp:omemo:2}device").get("id")
            id_b = b.find(".//{urn:xmpp:omemo:2}device").get("id")
            assert id_a != id_b

            for_a = await owner.encrypt_multi("only for A", to_jid=PEER_A)
            assert await co.decrypt(for_a[0], peer_jid=PEER_A, from_jid=OWNER) == "only for A"
            with pytest.raises(Omemo2Error):  # B's identity holds no key for A's message
                await co.decrypt(for_a[0], peer_jid=PEER_B, from_jid=OWNER)
        finally:
            await close_all(co, owner)

    run(scenario())


def test_encrypt_fails_closed_when_the_owner_lookup_fails(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path, omemo_policy="opportunistic")
        try:
            world.unreachable = True
            with pytest.raises(Omemo2Error) as excinfo:
                await co.encrypt("x", peer_jid=PEER_A, to_jid=OWNER)
            # a failed lookup is NOT "no devices": opportunistic must not downgrade on it
            assert not isinstance(excinfo.value, NoOmemoDevices)
        finally:
            await close_all(co, owner)

    run(scenario())


def test_untrusted_owner_device_is_refused_without_btbv(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path, omemo_btbv=False)
        try:
            with pytest.raises(Omemo2Error):
                await co.encrypt("x", peer_jid=PEER_A, to_jid=OWNER)
        finally:
            await close_all(co, owner)

    run(scenario())


# ------------------------------------------------------------- persistence


def test_identity_ratchet_and_published_bundle_survive_a_restart(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        sent = await owner.encrypt_multi("one", to_jid=PEER_A)
        assert await co.decrypt(sent[0], peer_jid=PEER_A, from_jid=OWNER) == "one"
        before = ET.tostring(await co.serve_items(PEER_A, DEVICE_LIST_NODE, []))
        await co.close()

        co2 = ComponentOmemo(cfg_for(tmp_path), fetcher=world)
        await co2.start()
        try:
            assert ET.tostring(await co2.serve_items(PEER_A, DEVICE_LIST_NODE, [])) == before
            sent = await owner.encrypt_multi("two", to_jid=PEER_A)  # same session
            assert await co2.decrypt(sent[0], peer_jid=PEER_A, from_jid=OWNER) == "two"
            back = await co2.encrypt("three", peer_jid=PEER_A, to_jid=OWNER)
            assert await owner.decrypt_any(back[0], from_jid=PEER_A) == "three"
        finally:
            await owner.close()
            await co2.close()

    run(scenario())


def test_lost_store_mints_a_new_device_id(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        first = ET.tostring(await co.serve_items(PEER_A, DEVICE_LIST_NODE, []))
        await co.close()
        os.unlink(tmp_path / "omemo" / "store.json")

        co2 = ComponentOmemo(cfg_for(tmp_path), fetcher=world)
        await co2.start()
        try:
            assert ET.tostring(await co2.serve_items(PEER_A, DEVICE_LIST_NODE, [])) != first
        finally:
            await owner.close()
            await co2.close()

    run(scenario())


def test_store_is_created_private_and_holds_no_plaintext(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        try:
            marker = "marker-7c1e-plaintext"
            sent = await owner.encrypt_multi(marker, to_jid=PEER_A)
            await co.decrypt(sent[0], peer_jid=PEER_A, from_jid=OWNER)
            path = tmp_path / "omemo" / "store.json"
            assert os.stat(path).st_mode & 0o777 == 0o600
            assert marker not in path.read_text()
        finally:
            await close_all(co, owner)

    run(scenario())


def test_corrupt_store_refuses_to_start(tmp_path):
    async def scenario():
        (tmp_path / "omemo").mkdir()
        (tmp_path / "omemo" / "store.json").write_text("{not json")
        co = ComponentOmemo(cfg_for(tmp_path), fetcher=OwnerWorld())
        with pytest.raises(Omemo2Error):
            await co.start()

    run(scenario())


def test_unwritable_store_path_fails_at_startup(tmp_path):
    async def scenario():
        blocker = tmp_path / "file"
        blocker.write_text("x")
        co = ComponentOmemo(cfg_for(tmp_path, omemo_store=str(blocker / "store.json")), fetcher=OwnerWorld())
        with pytest.raises(Omemo2Error):
            await co.start()

    run(scenario())


def test_store_falls_back_to_in_place_write_when_rename_hits_a_mount(tmp_path, monkeypatch):
    path = tmp_path / "store.json"
    storage = JsonFileStorage(str(path))
    real_replace = os.replace

    def busy(src, dst):
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(os, "replace", busy)
    run(storage._store("k", {"v": 1}))
    monkeypatch.setattr(os, "replace", real_replace)
    assert json.loads(path.read_text()) == {"k": {"v": 1}}
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".omemo-")]


# ------------------------------------------------------------------- policy


def test_required_policy_never_allows_plaintext(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path)
        try:
            assert not await co.plaintext_allowed(PEER_A)
        finally:
            await close_all(co, owner)

    run(scenario())


def test_opportunistic_allows_plaintext_only_until_the_conversation_is_encrypted(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path, omemo_policy="opportunistic")
        try:
            clear_owner_devices(world)
            with pytest.raises(NoOmemoDevices):
                await co.encrypt("x", peer_jid=PEER_A, to_jid=OWNER)
            assert await co.plaintext_allowed(PEER_A)

            owner2 = new_owner(world, co, tmp_path, "owner2.json")  # the owner turns OMEMO on
            await owner2.start()
            try:
                back = await co.encrypt("now encrypted", peer_jid=PEER_A, to_jid=OWNER)
                assert await owner2.decrypt_any(back[0], from_jid=PEER_A) == "now encrypted"
            finally:
                await owner2.close()

            assert not await co.plaintext_allowed(PEER_A)  # sticky
            assert await co.plaintext_allowed(PEER_B)      # other conversations unaffected
            clear_owner_devices(world)                     # a stripped device list
            with pytest.raises(Omemo2Error) as excinfo:    # (known devices turn inactive: no plaintext path)
                await co.encrypt("x", peer_jid=PEER_A, to_jid=OWNER)
            assert not isinstance(excinfo.value, NoOmemoDevices)
            assert not await co.plaintext_allowed(PEER_A)  # ...still cannot downgrade
        finally:
            await close_all(co, owner)

    run(scenario())


def test_sticky_encrypted_conversation_survives_a_restart(tmp_path):
    async def scenario():
        world, co, owner = await start_pair(tmp_path, omemo_policy="opportunistic")
        sent = await owner.encrypt_multi("hi", to_jid=PEER_A)
        await co.decrypt(sent[0], peer_jid=PEER_A, from_jid=OWNER)  # inbound encrypted also marks it
        await co.close()
        co2 = ComponentOmemo(cfg_for(tmp_path, omemo_policy="opportunistic"), fetcher=world)
        await co2.start()
        try:
            assert not await co2.plaintext_allowed(PEER_A)
            assert await co2.plaintext_allowed(PEER_B)
        finally:
            await owner.close()
            await co2.close()

    run(scenario())


def test_policy_config():
    base = {
        "XMPP_MODE": "component", "COMPONENT_JID": DOMAIN, "COMPONENT_DOMAIN": DOMAIN,
        "COMPONENT_SECRET": "s", "COMPONENT_OWNER": OWNER,
        "XMPP_OMEMO": "1", "XMPP_OMEMO_STORE": "/tmp/x.json",
    }
    assert Config.from_env(base).omemo_policy == "required"
    assert Config.from_env({**base, "XMPP_OMEMO_POLICY": "Opportunistic"}).omemo_policy == "opportunistic"
    assert "policy=required" in Config.from_env(base).describe()
    with pytest.raises(ConfigError):
        Config.from_env({**base, "XMPP_OMEMO_POLICY": "sometimes"})
    with pytest.raises(ConfigError):
        Config.from_env({
            "XMPP_JID": "bot@example.org", "XMPP_PASSWORD": "p", "XMPP_TARGET": "t@example.org",
            "XMPP_OMEMO": "1", "XMPP_OMEMO_STORE": "/tmp/x.json", "XMPP_OMEMO_POLICY": "opportunistic",
        })


# ------------------------------------------------------- transport (real slixmpp)


class Capture:
    def __init__(self, transport):
        self.sent = []

        async def send(stanza):
            self.sent.append(stanza)

        transport._send = send


async def make_transport(tmp_path, **cfg_overrides):
    world = OwnerWorld()
    cfg = cfg_for(tmp_path, **cfg_overrides)
    transport = ComponentTransport(cfg)
    co = ComponentOmemo(cfg, fetcher=world)
    await co.start()
    transport._omemo = co
    owner = new_owner(world, co, tmp_path)
    await owner.start()
    return transport, co, owner, world, Capture(transport)


def test_transport_sends_one_encrypted_stanza_with_fallback_hints(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            await transport.send_message(to=OWNER + "/phone", body="secret body", frm=PEER_A)
            (msg,) = cap.sent
            xml = ET.tostring(msg.xml, encoding="unicode")
            assert "secret body" not in xml
            assert "urn:xmpp:hints" in xml and "urn:xmpp:eme:0" in xml
            assert str(msg["from"]) == PEER_A
            assert await owner.decrypt_any(msg.xml.find(OMEMO2_TAG), from_jid=PEER_A) == "secret body"
        finally:
            await close_all(co, owner)

    run(scenario())


def test_transport_packs_every_backend_into_a_single_stanza(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            async def two_backends(body, *, peer_jid, to_jid):
                return [ET.Element(OMEMO2_TAG), ET.Element(LEGACY_TAG)]

            co.encrypt = two_backends
            await transport.send_message(to=OWNER, body="x", frm=PEER_A)
            (msg,) = cap.sent
            assert msg.xml.find(OMEMO2_TAG) is not None and msg.xml.find(LEGACY_TAG) is not None
        finally:
            await close_all(co, owner)

    run(scenario())


def test_transport_required_policy_never_sends_plaintext(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            clear_owner_devices(world)
            with pytest.raises(Omemo2Error):
                await transport.send_message(to=OWNER, body="never in clear", frm=PEER_A)
            texts = [str(m["body"]) for m in cap.sent]
            assert not any("never in clear" in t for t in texts)
            assert len(texts) == 1 and "NOT" in texts[0]  # the content-free notice
        finally:
            await close_all(co, owner)

    run(scenario())


def test_transport_opportunistic_sends_plaintext_only_without_devices(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path, omemo_policy="opportunistic")
        try:
            clear_owner_devices(world)
            await transport.send_message(to=OWNER, body="plain hello", frm=PEER_A)
            assert [str(m["body"]) for m in cap.sent] == ["plain hello"]

            owner2 = new_owner(world, co, tmp_path, "o2.json")  # the owner enables OMEMO
            await owner2.start()
            try:
                await transport.send_message(to=OWNER, body="enc hello", frm=PEER_A)
                assert "enc hello" not in ET.tostring(cap.sent[-1].xml, encoding="unicode")
                clear_owner_devices(world)  # stripped again: refused, not downgraded
                count = len(cap.sent)
                with pytest.raises(Omemo2Error):
                    await transport.send_message(to=OWNER, body="downgrade me", frm=PEER_A)
                assert not any("downgrade me" in str(m["body"]) for m in cap.sent[count:])
            finally:
                await owner2.close()
        finally:
            await close_all(co, owner)

    run(scenario())


def owner_message(transport, *, body=None, element=None, to=PEER_A):
    msg = transport.xmpp.make_message(mto=to, mfrom=OWNER + "/phone", mtype="chat", mbody=body)
    if element is not None:
        msg.xml.append(element)
    return msg


def test_inbound_plaintext_is_refused_under_required(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            assert await transport._inbound_body(owner_message(transport, body="clear"), to_jid=PEER_A) is None
            assert len(cap.sent) == 1 and cap.sent[0]["type"] == "error"
        finally:
            await close_all(co, owner)

    run(scenario())


def test_inbound_opportunistic_accepts_plaintext_until_sticky(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path, omemo_policy="opportunistic")
        try:
            assert await transport._inbound_body(owner_message(transport, body="clear"), to_jid=PEER_A) == "clear"
            sent = await owner.encrypt_multi("enc", to_jid=PEER_A)
            assert await transport._inbound_body(owner_message(transport, element=sent[0]), to_jid=PEER_A) == "enc"
            assert await transport._inbound_body(owner_message(transport, body="clear"), to_jid=PEER_A) is None
            assert await transport._inbound_body(
                owner_message(transport, body="clear", to=PEER_B), to_jid=PEER_B) == "clear"
        finally:
            await close_all(co, owner)

    run(scenario())


def test_inbound_tries_every_encrypted_element(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            world.hide_peer_omemo2 = True  # the owner encrypts with the legacy backend
            sent = await owner.encrypt_multi("to legacy", to_jid=PEER_A)
            assert namespaces(sent) == ["eu.siacs.conversations.axolotl"]
            # a keyless OMEMO 2 element ahead of the legacy one must not hide it
            msg = owner_message(transport, element=ET.Element(OMEMO2_TAG))
            msg.xml.append(sent[0])
            assert await transport._inbound_body(msg, to_jid=PEER_A) == "to legacy"
        finally:
            await close_all(co, owner)

    run(scenario())


def test_inbound_garbage_is_refused_not_forwarded(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            msg = owner_message(transport, element=ET.Element(OMEMO2_TAG))
            assert await transport._inbound_body(msg, to_jid=PEER_A) is None
            assert cap.sent and cap.sent[0]["type"] == "error"
        finally:
            await close_all(co, owner)

    run(scenario())


# ------------------------------------------------------------ IQ over the wire


def pubsub_iq(transport, *, node, ids=(), frm=OWNER + "/phone", to=PEER_A):
    iq = transport.xmpp.make_iq_get(ito=to, ifrom=frm)
    pubsub = ET.Element("{" + PUBSUB_NS + "}pubsub")
    items = ET.SubElement(pubsub, "{" + PUBSUB_NS + "}items", {"node": node})
    for item_id in ids:
        ET.SubElement(items, "{" + PUBSUB_NS + "}item", {"id": item_id})
    iq.xml.append(pubsub)
    return iq


def test_iq_pubsub_get_is_answered_from_the_peer_jid(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            await transport._handle_iq_get(pubsub_iq(transport, node=DEVICE_LIST_NODE))
            (reply,) = cap.sent
            assert reply["type"] == "result"
            assert str(reply["from"]) == PEER_A and str(reply["to"]).startswith(OWNER)
            assert reply.xml.find(".//{urn:xmpp:omemo:2}devices") is not None
        finally:
            await close_all(co, owner)

    run(scenario())


def test_iq_from_a_non_owner_or_to_a_non_peer_is_refused(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            await transport._handle_iq_get(pubsub_iq(transport, node=DEVICE_LIST_NODE, frm="mallory@example.net/x"))
            await transport._handle_iq_get(pubsub_iq(transport, node=DEVICE_LIST_NODE, to="x@other.example"))
            await transport._handle_iq_get(pubsub_iq(transport, node="urn:example:other"))
            assert [r["type"] for r in cap.sent] == ["error"] * 3
            assert [r["error"]["condition"] for r in cap.sent] == [
                "service-unavailable", "service-unavailable", "item-not-found"]
            assert not world.fetch_log
        finally:
            await close_all(co, owner)

    run(scenario())


def test_iq_disco_info_and_items(tmp_path):
    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            for ns in ("info", "items"):
                iq = transport.xmpp.make_iq_get(ito=PEER_A, ifrom=OWNER + "/phone")
                iq.xml.append(ET.Element("{http://jabber.org/protocol/disco#" + ns + "}query"))
                await transport._handle_iq_get(iq)
            assert [r["type"] for r in cap.sent] == ["result", "result"]
            identity = cap.sent[0].xml.find(".//{http://jabber.org/protocol/disco#info}identity")
            assert identity.get("type") == "pep"
        finally:
            await close_all(co, owner)

    run(scenario())


def test_only_iq_get_is_routed_to_the_responder():
    import inspect

    from relay import component
    assert 'StanzaPath("iq@type=get")' in inspect.getsource(component.ComponentTransport._register_handlers)


def test_fetch_item_payload_parses_a_pubsub_reply(tmp_path):
    async def scenario():
        transport = ComponentTransport(cfg_for(tmp_path))
        reply_xml = ET.fromstring(
            "<iq xmlns='jabber:client'><pubsub xmlns='" + PUBSUB_NS + "'><items node='n'>"
            "<item id='current'><devices xmlns='urn:xmpp:omemo:2'><device id='7'/></devices></item>"
            "</items></pubsub></iq>")
        seen = {}

        class FakeIq:
            xml = ET.Element("iq")

            async def send(self, timeout=None):
                seen["sent"] = ET.tostring(self.xml, encoding="unicode")

                class Reply:
                    xml = reply_xml
                return Reply()

        def make_iq_get(ito=None, ifrom=None):
            seen["ito"], seen["ifrom"] = ito, ifrom
            return FakeIq()

        transport.xmpp.make_iq_get = make_iq_get
        got = await transport.fetch_item_payload(frm=PEER_A, to=OWNER, node=DEVICE_LIST_NODE)
        assert got.tag == "{urn:xmpp:omemo:2}devices"
        assert seen["ito"] == OWNER and seen["ifrom"] == PEER_A
        assert "max_items" in seen["sent"] and DEVICE_LIST_NODE in seen["sent"]

    run(scenario())


def test_inbound_attachment_url_never_rides_in_clear_under_omemo(tmp_path):
    from relay import xep

    async def scenario():
        transport, co, owner, world, cap = await make_transport(tmp_path)
        try:
            url = "https://files.example/dl/abc?exp=1&sig=2"
            await transport.send_message(
                to=OWNER + "/phone", body=url, frm=PEER_A,
                elements=[xep.oob(url, "pic.jpg"), xep.store_hint()],
            )
            (msg,) = cap.sent
            xml = ET.tostring(msg.xml, encoding="unicode")
            assert "files.example" not in xml and "jabber:x:oob" not in xml
            assert await owner.decrypt_any(msg.xml.find(OMEMO2_TAG), from_jid=PEER_A) == url
        finally:
            await close_all(co, owner)

    run(scenario())
