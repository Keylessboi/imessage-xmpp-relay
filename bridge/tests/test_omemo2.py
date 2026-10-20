"""OMEMO 2: SCE envelope, session round trip, fail-closed behaviour.

Everything here is offline: the PEP layer is an in-memory store and the XMPP
stanzas are built by slixmpp without a connection.  The crypto is real
(python-omemo + python-twomemo), so these tests exercise the same code paths the
bridge uses on the wire.
"""
from __future__ import annotations

import os
from xml.etree import ElementTree as ET

import pytest

from conftest import run

from relay.config import Config, ConfigError
from relay.omemo2 import (
    DEVICE_LIST_NODE,
    BUNDLE_NODE,
    ENCRYPTED_TAG,
    NAMESPACE,
    SCE_NAMESPACE,
    InMemoryPepStore,
    Omemo2,
    Omemo2Error,
    build_sce_envelope,
    parse_sce_envelope,
)

ALICE = "alice@example.org"
BOB = "bob@example.org"
BOT = "bot@example.org"


def _manager(jid, path, pep, **kwargs):
    return Omemo2(own_bare_jid=jid, store_path=str(path), pep=pep, **kwargs)


async def _start_pair(tmp_path, sender=ALICE, recipient=BOB):
    pep = InMemoryPepStore()
    a = _manager(sender, tmp_path / "a.json", pep)
    b = _manager(recipient, tmp_path / "b.json", pep)
    await a.start()
    await b.start()
    return pep, a, b


# ------------------------------------------------------------------ SCE envelope


def test_sce_envelope_round_trip():
    payload = build_sce_envelope("hello world", from_jid=ALICE, to_jid=BOB)
    assert b"urn:xmpp:sce:1" in payload
    assert parse_sce_envelope(payload, expect_from=ALICE, expect_to=BOB) == "hello world"


def test_sce_envelope_carries_random_padding():
    first = build_sce_envelope("x", from_jid=ALICE)
    second = build_sce_envelope("x", from_jid=ALICE)
    assert first != second  # a fixed ciphertext would leak message equality
    assert b"rpad" in first


def test_sce_envelope_rejects_mismatched_affix():
    payload = build_sce_envelope("hi", from_jid=ALICE, to_jid=BOB)
    with pytest.raises(Omemo2Error):
        parse_sce_envelope(payload, expect_from="mallory@example.org")
    with pytest.raises(Omemo2Error):
        parse_sce_envelope(payload, expect_to="mallory@example.org")


def test_sce_envelope_rejects_garbage_and_wrong_namespace():
    with pytest.raises(Omemo2Error):
        parse_sce_envelope(b"this is not xml")
    with pytest.raises(Omemo2Error):
        parse_sce_envelope(b"<envelope xmlns='urn:not-sce'/>")


# ---------------------------------------------------------------- session round trip


def test_omemo2_round_trip_both_directions(tmp_path):
    async def scenario():
        pep, alice, bob = await _start_pair(tmp_path)
        try:
            first = await alice.encrypt_body("hello bob", to_jid=BOB)
            assert first.tag == ENCRYPTED_TAG
            assert await bob.decrypt_element(first, from_jid=ALICE) == "hello bob"

            second = await bob.encrypt_body("hi alice, unicode: \u00e9\u00e8\u4f60\u597d", to_jid=ALICE)
            assert await alice.decrypt_element(second, from_jid=BOB) == "hi alice, unicode: \u00e9\u00e8\u4f60\u597d"
        finally:
            await alice.close()
            await bob.close()

    run(scenario())


def test_encrypted_stanza_carries_the_omemo2_namespace(tmp_path):
    async def scenario():
        pep, alice, bob = await _start_pair(tmp_path)
        try:
            element = await alice.encrypt_body("secret", to_jid=BOB)
            xml = ET.tostring(element, encoding="unicode")
            assert NAMESPACE in xml
            assert "secret" not in xml  # the body must not leak in cleartext
            # Bob's device list is published under the OMEMO 2 node.
            assert BOB in pep.device_lists
            assert any(jid == BOB for jid, _ in pep.bundles)
        finally:
            await alice.close()
            await bob.close()

    run(scenario())


def test_identity_persists_across_restart(tmp_path):
    async def scenario():
        pep, alice, bob = await _start_pair(tmp_path)
        bob_device = bob.device_id
        assert bob_device is not None
        await bob.close()

        bob2 = _manager(BOB, tmp_path / "b.json", pep)
        await bob2.start()
        try:
            assert bob2.device_id == bob_device
            element = await alice.encrypt_body("after restart", to_jid=BOB)
            assert await bob2.decrypt_element(element, from_jid=ALICE) == "after restart"
        finally:
            await alice.close()
            await bob2.close()

    run(scenario())


def test_store_file_is_private_and_holds_no_plaintext(tmp_path):
    async def scenario():
        pep, alice, bob = await _start_pair(tmp_path)
        try:
            marker = "plaintext-marker-9f2c"
            element = await alice.encrypt_body(marker, to_jid=BOB)
            await bob.decrypt_element(element, from_jid=ALICE)
            path = tmp_path / "b.json"
            assert os.stat(path).st_mode & 0o777 == 0o600
            assert marker not in path.read_text(encoding="utf-8")
            assert "identity_key" in path.read_text(encoding="utf-8")
        finally:
            await alice.close()
            await bob.close()

    run(scenario())


# ------------------------------------------------------------------- fail closed


def test_encrypt_refuses_when_no_devices_are_known(tmp_path):
    async def scenario():
        pep, alice, bob = await _start_pair(tmp_path)
        try:
            with pytest.raises(Omemo2Error) as excinfo:
                await alice.encrypt_body("nope", to_jid="carol@example.org")
            assert "publishes no OMEMO 2 devices" in str(excinfo.value)
        finally:
            await alice.close()
            await bob.close()

    run(scenario())


def test_encrypt_and_decrypt_refuse_when_not_started(tmp_path):
    async def scenario():
        pep = InMemoryPepStore()
        alice = _manager(ALICE, tmp_path / "a.json", pep)
        with pytest.raises(Omemo2Error):
            await alice.encrypt_body("nope", to_jid=BOB)
        with pytest.raises(Omemo2Error):
            await alice.decrypt_element(ET.fromstring("<x/>"), from_jid=BOB)

    run(scenario())


def test_encrypt_fails_closed_when_pep_goes_away(tmp_path):
    async def scenario():
        pep, alice, bob = await _start_pair(tmp_path)
        try:
            pep.reachable = False
            with pytest.raises(Omemo2Error):
                await alice.encrypt_body("nope", to_jid=BOB)
        finally:
            pep.reachable = True
            await alice.close()
            await bob.close()

    run(scenario())


def test_decrypt_rejects_a_foreign_element(tmp_path):
    async def scenario():
        pep, alice, bob = await _start_pair(tmp_path)
        try:
            wrong = ET.fromstring("<encrypted xmlns='urn:xmpp:omemo:1'/>")
            with pytest.raises(Omemo2Error):
                await bob.decrypt_element(wrong, from_jid=ALICE)
        finally:
            await alice.close()
            await bob.close()

    run(scenario())


# ------------------------------------------------------------------ transport wiring


def _transport(cfg):
    from relay.xmpp_side import SlixmppTransport
    return SlixmppTransport(cfg)


def test_transport_decrypts_an_omemo2_stanza(tmp_path):
    async def scenario():
        pep, alice, bot = await _start_pair(tmp_path, sender=ALICE, recipient=BOT)
        try:
            cfg = Config(jid=BOT, password="p", targets=("pipe@example.org",), omemo=True,
                         omemo_store=str(tmp_path / "bot.json"))
            transport = _transport(cfg)
            transport._omemo = bot
            element = await alice.encrypt_body("through the transport", to_jid=BOT)
            msg = transport.xmpp.make_message(mto=BOT, mfrom=ALICE + "/phone", mtype="chat")
            msg.append(element)
            body = await transport._extract_body(msg, mtype="chat", sender=ALICE + "/phone")
            assert body == "through the transport"
        finally:
            await alice.close()
            await bot.close()

    run(scenario())


def test_transport_refuses_plaintext_when_omemo_is_on(tmp_path):
    async def scenario():
        pep, alice, bot = await _start_pair(tmp_path, sender=ALICE, recipient=BOT)
        try:
            cfg = Config(jid=BOT, password="p", targets=("pipe@example.org",), omemo=True,
                         omemo_store=str(tmp_path / "bot.json"))
            transport = _transport(cfg)
            transport._omemo = bot
            msg = transport.xmpp.make_message(mto=BOT, mfrom=ALICE + "/phone",
                                              mbody="cleartext", mtype="chat")
            assert await transport._extract_body(msg, mtype="chat", sender=ALICE + "/phone") is None
            # Group chat stays plaintext, but is explicitly out of OMEMO scope.
            group = transport.xmpp.make_message(mto="room@example.org", mfrom=ALICE + "/phone",
                                                mbody="room text", mtype="groupchat")
            assert await transport._extract_body(group, mtype="groupchat", sender=ALICE + "/phone") == "room text"
        finally:
            await alice.close()
            await bot.close()

    run(scenario())


def test_transport_refuses_to_send_plaintext_without_omemo(tmp_path):
    async def scenario():
        cfg = Config(jid=BOT, password="p", targets=("pipe@example.org",), omemo=True,
                     omemo_store=str(tmp_path / "bot.json"))
        transport = _transport(cfg)
        with pytest.raises(Omemo2Error):
            await transport.send_message(to=ALICE, body="must not leak", mtype="chat")
        # Group chat is not covered by OMEMO and is still allowed.
        transport.xmpp.send = lambda *a, **k: None  # noqa: ARG005 - avoid a live stanza
        await transport.send_message(to="room@example.org", body="room", mtype="groupchat")

    run(scenario())


# -------------------------------------------------------------------------- config


def test_config_parses_omemo_settings():
    cfg = Config.from_env({
        "XMPP_JID": BOT,
        "XMPP_PASSWORD": "p",
        "XMPP_TARGET": "pipe@example.org",
        "XMPP_OMEMO": "1",
        "XMPP_OMEMO_STORE": "/tmp/omemo-test.json",
        "XMPP_OMEMO_BTBV": "0",
    })
    assert cfg.omemo is True
    assert cfg.omemo_store == "/tmp/omemo-test.json"
    assert cfg.omemo_btbv is False
    assert "omemo=on" in cfg.describe()
    assert "/tmp/omemo-test.json" in cfg.describe()


def test_config_omemo_requires_a_store_path():
    with pytest.raises(ConfigError):
        Config.from_env({
            "XMPP_JID": BOT,
            "XMPP_PASSWORD": "p",
            "XMPP_TARGET": "pipe@example.org",
            "XMPP_OMEMO": "1",
        })


def test_config_omemo_defaults_to_off():
    cfg = Config.from_env({
        "XMPP_JID": BOT,
        "XMPP_PASSWORD": "p",
        "XMPP_TARGET": "pipe@example.org",
    })
    assert cfg.omemo is False
    assert cfg.omemo_btbv is True
    assert "omemo=off" in cfg.describe()
