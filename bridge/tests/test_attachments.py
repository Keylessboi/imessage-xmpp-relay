"""Inbound attachments: the `attachment` SSE event becomes an XEP-0066 message.

Fake transport only; no real XMPP server or Apple involved.
"""
from __future__ import annotations

import pytest
from conftest import run
from fakes import FakeComponentTransport, FakeTransport

from relay import xep
from relay.attachments import build_url, oob_elements
from relay.config import Config, ConfigError, parse_public_base_url
from relay.core import Bridge
from relay.upstream import UpstreamClient

from test_component import DOMAIN, OWNER, PEER, PEER_JID, component_config

BASE = "https://imessage-files.example.org"
ID = "a" * 32
SIG = "b" * 64
PATH = "/dl/" + ID + "?exp=1700000000&sig=" + SIG
URL = BASE + PATH


def att_event(**kw) -> dict:
    ev = {
        "type": "attachment", "guid": "G1", "chat_guid": "iMessage;-;" + PEER, "from": PEER,
        "id": ID, "name": "cat.jpg", "mime": "image/jpeg", "size": 10, "path": PATH,
    }
    ev.update(kw)
    return ev


def wire(cfg):
    transport = FakeComponentTransport()
    return Bridge(cfg, transport, UpstreamClient(cfg.api_url)), transport


def test_build_url_accepts_only_server_minted_paths():
    assert build_url(BASE, PATH) == URL
    assert build_url(BASE + "/", PATH) == URL
    assert build_url("", PATH) is None
    for bad in ["", "/im/send", "/dl/../x?exp=1&sig=" + SIG, "/dl/" + ID, "/dl/" + ID + "?exp=1&sig=zz",
                "http://evil/dl/" + ID + "?exp=1&sig=" + SIG, PATH + "&x=1", "//evil" + PATH]:
        assert build_url(BASE, bad) is None, bad


def test_base_url_config_is_validated():
    assert parse_public_base_url(None) == ""
    assert parse_public_base_url(" https://f.example.org/ ") == "https://f.example.org"
    for bad in ["ftp://x", "f.example.org", "https://x/?a=1", "https://x/#f"]:
        with pytest.raises(ConfigError):
            parse_public_base_url(bad)


def test_oob_element_shape():
    el = xep.oob(URL, "cat.jpg")
    assert el.tag == "{jabber:x:oob}x"
    assert el.find("{jabber:x:oob}url").text == URL
    assert el.find("{jabber:x:oob}desc").text == "cat.jpg"
    assert xep.oob(URL).find("{jabber:x:oob}desc") is None
    assert xep.parse_oob_url(el_in_message(el)) == URL
    assert xep.parse_oob_url(None) is None
    assert [e.tag for e in oob_elements(URL, "")][0] == "{jabber:x:oob}x"


def el_in_message(el):
    import xml.etree.ElementTree as ET
    m = ET.Element("{jabber:client}message")
    m.append(el)
    return m


def test_component_delivers_oob_from_the_peer_jid():
    async def scenario():
        bridge, transport = wire(component_config(public_file_base_url=BASE))
        await bridge.on_event(att_event())
        assert transport.sent == [
            {"to": OWNER, "body": URL, "mtype": "chat", "muc_nick": None, "frm": PEER_JID}
        ]
        tags = [e.tag for e in transport.extras[0]["elements"]]
        assert "{jabber:x:oob}x" in tags
        assert transport.extras[0]["elements"][0].find("{jabber:x:oob}url").text == URL
        assert bridge.stats["attachments_delivered"] == 1
        await bridge.upstream.aclose()

    run(scenario())


def test_group_attachment_is_sent_from_the_component_jid():
    async def scenario():
        bridge, transport = wire(component_config(public_file_base_url=BASE))
        await bridge.on_event(att_event(chat_guid="iMessage;+;abc"))
        assert transport.sent[0]["frm"] is None
        assert transport.sent[0]["body"] == URL
        await bridge.upstream.aclose()

    run(scenario())


def test_unconfigured_base_drops_the_event_without_a_stanza():
    async def scenario():
        bridge, transport = wire(component_config())
        await bridge.on_event(att_event())
        assert transport.sent == []
        assert bridge.stats["attachment_unlinked"] == 1
        await bridge.upstream.aclose()

    run(scenario())


def test_forged_path_is_not_forwarded():
    async def scenario():
        bridge, transport = wire(component_config(public_file_base_url=BASE))
        await bridge.on_event(att_event(path="/im/send"))
        assert transport.sent == []
        await bridge.upstream.aclose()

    run(scenario())


def test_client_mode_sends_the_url_as_the_body(make_config):
    async def scenario():
        cfg = make_config(public_file_base_url=BASE)
        transport = FakeTransport()
        bridge = Bridge(cfg, transport, UpstreamClient(cfg.api_url))
        await bridge.on_event(att_event())
        assert transport.sent and URL in transport.sent[0]["body"]
        assert transport.extras[0]["elements"] == []
        await bridge.upstream.aclose()

    run(scenario())
