"""In-memory transports used instead of a real XMPP server."""
from __future__ import annotations

import asyncio
from typing import Optional

from relay.models import IncomingXmpp


class FakeTransport:
    """The client-mode XmppTransport surface."""

    def __init__(self, *, fail_sends: bool = False) -> None:
        self.sent: list[dict] = []
        # Parallel to `sent`: msg_id/elements, kept apart so `sent` entries stay exact.
        self.extras: list[dict] = []
        self.handler = None
        self.started = False
        self.stopped = False
        self.fail_sends = fail_sends
        self._stop = asyncio.Event()

    def set_incoming_handler(self, handler) -> None:
        self.handler = handler

    async def start(self) -> None:
        self.started = True
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    async def send_message(
        self,
        *,
        to: str,
        body: str,
        mtype: str = "chat",
        muc_nick: Optional[str] = None,
        frm: Optional[str] = None,
        msg_id: Optional[str] = None,
        elements=(),
    ) -> None:
        if self.fail_sends:
            raise RuntimeError("fake transport failure")
        self.extras.append({"msg_id": msg_id, "elements": list(elements)})
        entry = {"to": to, "body": body, "mtype": mtype, "muc_nick": muc_nick}
        if frm is not None:  # client mode never sets it; keeps old assertions exact
            entry["frm"] = frm
        self.sent.append(entry)

    async def deliver(self, message: IncomingXmpp) -> None:
        assert self.handler is not None, "set_incoming_handler was never called"
        await self.handler(message)

    def bodies(self) -> list[str]:
        return [item["body"] for item in self.sent]


class FakeComponentTransport:
    """The component-mode XmppTransport surface (records the From JID)."""

    def __init__(self, *, fail_sends: bool = False) -> None:
        self.sent: list[dict] = []
        # Parallel to `sent`: msg_id/elements, kept apart so `sent` entries stay exact.
        self.extras: list[dict] = []
        self.handler = None
        self.started = False
        self.stopped = False
        self.fail_sends = fail_sends
        self._stop = asyncio.Event()

    def set_incoming_handler(self, handler) -> None:
        self.handler = handler

    async def start(self) -> None:
        self.started = True
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    async def send_message(
        self,
        *,
        to: str,
        body: str,
        mtype: str = "chat",
        muc_nick: Optional[str] = None,
        frm: Optional[str] = None,
        msg_id: Optional[str] = None,
        elements=(),
    ) -> None:
        if self.fail_sends:
            raise RuntimeError("fake component transport failure")
        self.extras.append({"msg_id": msg_id, "elements": list(elements)})
        self.sent.append(
            {"to": to, "body": body, "mtype": mtype, "muc_nick": muc_nick, "frm": frm}
        )

    async def deliver(self, message: IncomingXmpp) -> None:
        assert self.handler is not None, "set_incoming_handler was never called"
        await self.handler(message)

    def bodies(self) -> list[str]:
        return [item["body"] for item in self.sent]


class FakeStanza(dict):
    """Minimal stand-in for a slixmpp Message/Presence.

    slixmpp stanzas are mapping-like with '' for unset fields; the component
    transport only reads type/from/to/body, so this is enough to exercise it
    without importing slixmpp or opening a socket.
    """

    def __getitem__(self, key):
        return self.get(key, "")

    @property
    def xml(self):
        """The etree element of the stanza (tests pass one as `xml=`), else None."""
        return self.get("xml")


class _FakeOutgoingStanza:
    def __init__(self, xmpp, record: dict) -> None:
        import xml.etree.ElementTree as ET

        self._xmpp = xmpp
        self._record = record
        self.xml = ET.Element("message")

    def __setitem__(self, key, value) -> None:
        self._record[key] = value

    def send(self) -> None:
        record = dict(self._record)
        if len(self.xml):
            record["elements"] = list(self.xml)
        self._xmpp.sent.append(record)


class FakeComponentXmpp:
    """Stand-in for slixmpp.ComponentXMPP: records outgoing stanzas, no XML."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.sent: list[dict] = []
        self.connected: list[tuple] = []
        self.disconnect_calls = 0
        self.is_component = True

    def add_event_handler(self, name: str, handler) -> None:
        self.handlers.setdefault(name, []).append(handler)

    def make_message(self, mto=None, mbody=None, mtype=None, mfrom=None):
        return _FakeOutgoingStanza(
            self, {"kind": "message", "to": mto, "body": mbody, "type": mtype, "from": mfrom}
        )

    def make_presence(self, pto=None, pfrom=None, ptype=None):
        return _FakeOutgoingStanza(
            self, {"kind": "presence", "to": pto, "from": pfrom, "type": ptype}
        )

    def connect(self, host=None, port=None):
        self.connected.append((host, port))
        return None

    async def disconnect(self, wait=None):
        self.disconnect_calls += 1

    async def fire(self, event: str, stanza) -> None:
        """Dispatch an event exactly like slixmpp would."""
        for handler in self.handlers.get(event, []):
            result = handler(stanza)
            if asyncio.iscoroutine(result):
                await result

    def messages(self) -> list[dict]:
        return [item for item in self.sent if item["kind"] == "message"]

    def presences(self) -> list[dict]:
        return [item for item in self.sent if item["kind"] == "presence"]
