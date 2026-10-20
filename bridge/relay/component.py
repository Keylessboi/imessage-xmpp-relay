"""XEP-0114 external component transport ("component mode").

In component mode the bridge is not a logged-in account. It connects to the
server's component listener (default port 5347, shared secret) and owns one
domain, e.g. `imessage.example.org`. Every iMessage/SMS peer then becomes
its own XMPP contact JID:

    +15557654321        ->  +15557654321@imessage.example.org
    alice@example.com   ->  alice.example.com... (sanitised localpart)

Phone handles keep their `+digits` form as the localpart (a legal JID
localpart). Anything else is encoded character by character so the mapping is
lossless and reversible: every byte outside `[a-z0-9._]` becomes `-xx` (two
lowercase hex digits), including an ordinary `-`, which is why decoding is
unambiguous. For example `john.doe@icloud.com` becomes
`john.doe-40icloud.com@imessage.example.org`.

Only the configured owner JID may talk to the component: the server is
usually federated, so the transport itself is deny-by-default and drops every
stanza whose bare `from` is not the owner. Inbound messages are accepted only
when their `to` localpart decodes back to a handle this module would have
generated.

The slixmpp object is injected in tests (`ComponentTransport(cfg, xmpp=...)`),
so the whole stanza layer runs with no socket and no XMPP server.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional, Sequence
from xml.etree import ElementTree as ET

from .config import Config
from .logsetup import get_logger
from .models import IncomingXmpp, bare_jid, normalize_handle
from .upstream import Backoff
from .xep import parse_features, parse_oob_url, strip_fallback
from .xmpp_side import IncomingHandler

log = get_logger("relay.component")

# OMEMO wire elements (OMEMO 2 and legacy) and the helper elements around them.
OMEMO2_ENCRYPTED_TAG = "{urn:xmpp:omemo:2}encrypted"
LEGACY_ENCRYPTED_TAG = "{eu.siacs.conversations.axolotl}encrypted"
HINTS_NS = "urn:xmpp:hints"
EME_NS = "urn:xmpp:eme:0"
OMEMO_FALLBACK_BODY = (
    "I sent you an OMEMO encrypted message but your client doesn't seem to "
    "support that. Find more information on https://conversations.im/omemo"
)
PUBSUB_NS = "http://jabber.org/protocol/pubsub"
DISCO_INFO_NS = "http://jabber.org/protocol/disco#info"
DISCO_ITEMS_NS = "http://jabber.org/protocol/disco#items"
# How long to wait for the owner's server to answer a device-list/bundle fetch.
IQ_TIMEOUT = 15.0

# E.164-ish: a leading + and 7..15 digits, the first of them nonzero.
E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
_SAFE_LOCALPART = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._")
_HEX = frozenset("0123456789abcdef")
# RFC 7622 limits a localpart to 1023 bytes; stay well below it.
_MAX_LOCALPART = 900


def is_phone_handle(handle: str) -> bool:
    """True when the handle normalises to an E.164-shaped +digits string."""
    return bool(E164_RE.match(normalize_handle(handle or "")))


def handle_to_localpart(handle: str) -> Optional[str]:
    """Map an iMessage handle to a JID localpart, or None when unmappable."""
    value = (handle or "").strip()
    if not value:
        return None
    phone = normalize_handle(value)
    if is_phone_handle(phone):
        return phone
    # Non-phone (Apple-ID email, relay identifier, ...): lowercase and escape
    # every byte that is not a safe JID localpart character.
    encoded_parts: list[str] = []
    for ch in normalize_handle(value):
        if ch in _SAFE_LOCALPART:
            encoded_parts.append(ch)
        else:
            encoded_parts.extend("-" + format(byte, "02x") for byte in ch.encode("utf-8"))
    encoded = "".join(encoded_parts)
    if not encoded or len(encoded) > _MAX_LOCALPART:
        return None
    return encoded


def localpart_to_handle(localpart: str) -> Optional[str]:
    """Reverse of handle_to_localpart; None for anything we did not generate."""
    value = (localpart or "").strip().lower()
    if not value or len(value) > _MAX_LOCALPART:
        return None
    if is_phone_handle(value):
        return normalize_handle(value)

    raw = bytearray()
    index = 0
    while index < len(value):
        ch = value[index]
        if ch == "-":
            pair = value[index + 1:index + 3]
            if len(pair) != 2 or any(c not in _HEX for c in pair):
                return None
            raw.append(int(pair, 16))
            index += 3
        else:
            if ch not in _SAFE_LOCALPART:
                return None
            raw.append(ord(ch))
            index += 1
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Round-trip check: only accept localparts this module could have produced.
    if not decoded or handle_to_localpart(decoded) != value:
        return None
    # A plain word localpart ("unknown", "alice") is not a handle: phone JIDs
    # were handled above, and non-phone handles either are emails or needed an
    # escape. Anything else is a typo, not a contact.
    if "@" not in decoded and "-" not in value:
        return None
    return normalize_handle(decoded)


def peer_jid(handle: str, domain: str) -> Optional[str]:
    """The contact JID a handle gets inside the component's domain."""
    if not (domain or "").strip():
        return None
    localpart = handle_to_localpart(handle)
    if not localpart:
        return None
    return localpart + "@" + domain.strip().lower()


def jid_to_handle(jid: str, domain: str) -> Optional[str]:
    """The iMessage handle behind a JID in the component's domain, or None."""
    if not (jid or "").strip() or not (domain or "").strip():
        return None
    bare = (jid or "").split("/", 1)[0].strip().lower()
    localpart, sep, dom = bare.rpartition("@")
    if not sep or not localpart or dom != domain.strip().lower():
        return None
    return localpart_to_handle(localpart)


def _is_reaction_payload(element: Any) -> bool:
    """A XEP-0444 reactions element, or the XEP-0428 fallback marker that points at it."""
    tag = getattr(element, "tag", "")
    if tag == "{urn:xmpp:reactions:0}reactions":
        return True
    return tag == "{urn:xmpp:fallback:0}fallback" and element.get("for") == "urn:xmpp:reactions:0"


def _is_oob_payload(element: Any) -> bool:
    """A XEP-0066 element: it carries the (bearer) file URL, so it never rides in clear."""
    return getattr(element, "tag", "") == "{jabber:x:oob}x"


class ComponentTransport:
    """XmppTransport implemented with slixmpp's ComponentXMPP (XEP-0114).

    Connects to `COMPONENT_HOST:COMPONENT_PORT` with `COMPONENT_SECRET` and
    owns `COMPONENT_JID`. The secret is only ever handed to slixmpp; it is
    never logged here and `Config.describe()` omits it.
    """

    def __init__(self, cfg: Config, xmpp: Optional[Any] = None) -> None:
        if cfg.mode != "component":
            raise ValueError("ComponentTransport requires XMPP_MODE=component")
        if not cfg.component_secret:
            raise ValueError("ComponentTransport requires COMPONENT_SECRET")

        self.cfg = cfg
        self._handler: Optional[IncomingHandler] = None
        self._stopping = False
        self._session_started: Optional[asyncio.Future] = None
        self._disconnected: Optional[asyncio.Event] = None
        self._handshaken = False
        self._backoff = Backoff(
            initial=cfg.backoff_initial,
            factor=cfg.backoff_factor,
            max_delay=cfg.backoff_max,
            jitter=cfg.backoff_jitter,
        )
        self._owner = bare_jid(cfg.component_owner)
        self._omemo: Optional[Any] = None  # ComponentOmemo once started (OMEMO on)
        self._omemo_failed = False

        if xmpp is None:
            import slixmpp  # imported lazily so tests can inject a fake

            xmpp = slixmpp.ComponentXMPP(
                cfg.component_jid,
                cfg.component_secret,
                cfg.component_host,
                cfg.component_port,
            )
        self.xmpp = xmpp
        self._calls: Optional[Any] = None  # relay.calls.CallService, see attach_calls
        self._register_handlers()
        self._advertise_features(("jabber:x:oob",))

    # -- wiring ------------------------------------------------------------

    def _register_handlers(self) -> None:
        self.xmpp.add_event_handler("session_start", self._on_session_start)
        self.xmpp.add_event_handler("message", self._on_message)
        self.xmpp.add_event_handler("presence_subscribe", self._on_presence_subscribe)
        self.xmpp.add_event_handler("presence_probe", self._on_presence_probe)
        self.xmpp.add_event_handler("disconnected", self._on_disconnected)
        if self.cfg.omemo:
            from slixmpp.xmlstream.handler import CoroutineCallback
            from slixmpp.xmlstream.matcher import StanzaPath

            # Components have no PEP: the bridge answers disco/pubsub GETs itself.
            self.xmpp.register_handler(
                CoroutineCallback("omemo-iq-get", StanzaPath("iq@type=get"), self._on_iq_get)
            )

    def _advertise_features(self, features) -> None:
        """XEP-0030: answer disco#info with the namespaces we understand.

        Best effort: a fake or minimal XMPP object without the plugin is fine.
        """
        register = getattr(self.xmpp, "register_plugin", None)
        if register is None:
            return
        try:
            register("xep_0030")
            disco = self.xmpp.plugin["xep_0030"]
            for feature in features:
                disco.add_feature(feature)
        except Exception:  # noqa: BLE001 - never let discovery break the stream
            log.warning("could not set up service discovery", event="component_disco_error", exc_info=True)

    def set_incoming_handler(self, handler: IncomingHandler) -> None:
        self._handler = handler

    def attach_calls(self, calls: Any) -> None:
        """Enable call-to-FaceTime-link handling (relay.calls.CallService)."""
        self._calls = calls
        register = getattr(self.xmpp, "register_handler", None)
        if register is None:  # injected fakes without a handler registry
            return
        from slixmpp.xmlstream.handler import CoroutineCallback as Callback
        from slixmpp.xmlstream.matcher import MatchXPath
        from slixmpp.xmlstream.matcher.base import MatcherBase

        ns = getattr(self.xmpp, "default_ns", "jabber:component:accept")

        class _BodylessJingleMessage(MatcherBase):
            # slixmpp's own "IM" handler only matches messages that carry a
            # <body/>, so a XEP-0353 <propose/> never reaches the "message"
            # event. Match body-less messages with a jingle-message child.
            def match(self, stanza) -> bool:
                xml = getattr(stanza, "xml", stanza)
                if xml.tag != "{%s}message" % ns:
                    return False
                if xml.find("{%s}body" % ns) is not None:
                    return False
                return any(
                    isinstance(c.tag, str) and c.tag.startswith("{urn:xmpp:jingle-message:0}")
                    for c in xml
                )

        register(Callback("facetime jingle message", _BodylessJingleMessage(None),
                          self._on_jingle_message))
        register(Callback("facetime jingle iq", MatchXPath(
            "{%s}iq/{urn:xmpp:jingle:1}jingle" % ns), self._on_jingle_iq))
        register(Callback("facetime disco info", MatchXPath(
            "{%s}iq/{http://jabber.org/protocol/disco#info}query" % ns), self._on_disco_iq))

    async def send_raw(self, xml: str) -> None:
        result = self.xmpp.send_raw(xml)
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result

    # -- XmppTransport -----------------------------------------------------

    async def start(self) -> None:
        if self.cfg.omemo:
            await self._start_omemo()
        loop = asyncio.get_running_loop()
        self._disconnected = asyncio.Event()
        while not self._stopping:
            self._disconnected.clear()
            self._handshaken = False
            self._session_started = loop.create_future()
            log.info(
                "connecting to the XMPP component port",
                event="component_connect",
                jid=self.cfg.component_jid,
                domain=self.cfg.component_domain,
                host=self.cfg.component_host,
                port=self.cfg.component_port,
            )
            self.xmpp.connect(host=self.cfg.component_host, port=self.cfg.component_port)
            try:
                await self._session_started
                self._backoff.reset()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    break
                log.error("component session failed", event="component_session_error", error=str(exc))
                await self._sleep_or_stop(self._backoff.next_delay())
                continue
            await self._disconnected.wait()
            if self._stopping:
                break
            delay = self._backoff.next_delay()
            log.warning(
                "XEP-0114 component disconnected; reconnecting",
                event="component_reconnect",
                delay=round(delay, 3),
            )
            await self._sleep_or_stop(delay)

    async def stop(self) -> None:
        self._stopping = True
        if self._omemo is not None:
            try:
                await self._omemo.close()
            except Exception:  # noqa: BLE001
                log.debug("OMEMO shutdown raised", exc_info=True, event="omemo_shutdown_error")
        # Wake start() if it is still waiting for a handshake that will never
        # come (e.g. stopping right after a rejected connection).
        if self._session_started is not None and not self._session_started.done():
            self._session_started.set_exception(
                RuntimeError("component transport stopped")
            )
        if self._disconnected is not None:
            self._disconnected.set()
        try:
            await asyncio.wait_for(self.xmpp.disconnect(wait=1.0), timeout=5)
        except asyncio.TimeoutError:
            log.warning("component disconnect timed out", event="component_disconnect_timeout")
        except Exception:
            log.debug("component disconnect raised", exc_info=True, event="component_disconnect_error")

    async def send_message(
        self,
        *,
        to: str,
        body: str,
        mtype: str = "chat",
        muc_nick: Optional[str] = None,
        frm: Optional[str] = None,
        msg_id: Optional[str] = None,
        elements: Sequence[Any] = (),
    ) -> None:
        target = (to or "").strip()
        if not target:
            raise ValueError("refusing to send a stanza without a destination")
        if bare_jid(target) == bare_jid(self.cfg.component_jid):
            log.warning(
                "refusing to send a stanza to the component itself",
                event="component_self_send",
                to=target,
            )
            return

        sender = (frm or "").strip() or self.cfg.component_jid
        if bare_jid(sender) != bare_jid(self.cfg.component_jid):
            if jid_to_handle(sender, self.cfg.component_domain) is None:
                # Never let a caller spoof a From outside the component domain.
                log.warning(
                    "ignoring a From JID outside the component domain",
                    event="component_bad_from",
                    frm=sender,
                )
                sender = self.cfg.component_jid
        if self.cfg.omemo and mtype in ("chat", "normal"):
            # 1:1 traffic is encrypted or not delivered: never plaintext.
            await self._send_omemo_stanza(
                to=target, body=body, mtype=mtype, sender=sender, msg_id=msg_id, elements=elements
            )
            return
        msg = self.xmpp.make_message(mto=target, mbody=body or None, mtype=mtype, mfrom=sender)
        if msg_id:
            msg["id"] = msg_id
        for element in elements:
            # Feature payloads (receipts, reactions, ...) are plain etree elements.
            msg.xml.append(element)
        await self._send(msg)

    # -- OMEMO -----------------------------------------------------------------

    async def _start_omemo(self) -> None:
        from .component_omemo import ComponentOmemo
        from .omemo2 import Omemo2Error

        omemo = ComponentOmemo(self.cfg, fetcher=self)
        try:
            await omemo.start()
        except Exception as exc:  # noqa: BLE001 - a broken store must not kill the service
            # Fail closed: _omemo stays None, so no message is sent in plaintext
            # and every encrypted stanza / OMEMO IQ is refused.
            self._omemo_failed = True
            log.error(
                "OMEMO initialisation failed; messages will NOT be sent in plaintext",
                event="omemo_init_error",
                error=str(exc) if isinstance(exc, Omemo2Error) else type(exc).__name__,
            )
            return
        self._omemo = omemo
        log.info("component OMEMO ready", event="omemo_ready", store_mode="0600")

    async def fetch_item_payload(
        self, *, frm: str, to: str, node: str, item_id: Optional[str] = None
    ) -> Optional[ET.Element]:
        """ItemFetcher: a pubsub items GET from `frm` to `to` over the component stream."""
        from slixmpp.exceptions import IqError, IqTimeout

        from .omemo2 import Omemo2Error

        pubsub = ET.Element("{" + PUBSUB_NS + "}pubsub")
        items = ET.SubElement(pubsub, "{" + PUBSUB_NS + "}items", {"node": node})
        if item_id is not None:
            ET.SubElement(items, "{" + PUBSUB_NS + "}item", {"id": item_id})
        else:
            items.set("max_items", "1")
        iq = self.xmpp.make_iq_get(ito=to, ifrom=frm)
        iq.xml.append(pubsub)
        try:
            reply = await iq.send(timeout=IQ_TIMEOUT)
        except IqTimeout as exc:
            raise Omemo2Error("timed out fetching " + node + " from " + to) from exc
        except IqError as exc:
            if exc.condition in ("item-not-found", "service-unavailable"):
                return None
            raise Omemo2Error("fetching " + node + " from " + to + " failed: " + str(exc.condition)) from exc
        item = reply.xml.find(
            "{" + PUBSUB_NS + "}pubsub/{" + PUBSUB_NS + "}items/{" + PUBSUB_NS + "}item"
        )
        if item is None or len(item) == 0:
            return None
        return item[0]

    async def _send_omemo_stanza(
        self, *, to: str, body: str, mtype: str, sender: str, msg_id: Optional[str], elements: Sequence[Any]
    ) -> None:
        """OMEMO policy for stanzas that carry feature payloads (receipts, reactions, replies...).

        Content never travels in clear: a body is always encrypted. Of the payload
        elements only id-bearing metadata rides the outer stanza (markers, receipt
        requests, replies/corrections/retractions, chat states). A reactions element
        carries the emoji itself, so it is dropped (the encrypted fallback body says
        the same thing). Body-less stanzas (typing, receipts, displayed markers) have
        no text to protect and go out as-is: they leak only that the owner is
        typing/reading, which XEP-0384 accepts for chat states.
        """
        keep = [e for e in elements if not _is_reaction_payload(e) and not _is_oob_payload(e)]
        if not (body or "").strip():
            if len(keep) != len(elements):
                log.warning("dropping a reaction with no encrypted fallback body", event="omemo_reaction_dropped", to=to)
                return
            if not keep:
                return
            msg = self.xmpp.make_message(mto=to, mtype=mtype, mfrom=sender)
            if msg_id:
                msg["id"] = msg_id
            for element in keep:
                msg.xml.append(element)
            await self._send(msg)
            return
        await self._send_encrypted(
            to=to, body=body, mtype=mtype, sender=sender, msg_id=msg_id, elements=keep, plain_elements=elements
        )

    async def _send_encrypted(
        self,
        *,
        to: str,
        body: str,
        mtype: str,
        sender: str,
        msg_id: Optional[str] = None,
        elements: Sequence[Any] = (),
        plain_elements: Optional[Sequence[Any]] = None,
    ) -> None:
        from .omemo2 import NoOmemoDevices, Omemo2Error

        try:
            if self._omemo is None:
                raise Omemo2Error("OMEMO is enabled but not initialised")
            try:
                encrypted = await self._omemo.encrypt(body, peer_jid=bare_jid(sender), to_jid=to)
            except NoOmemoDevices:
                # Only an explicit opportunistic policy, on a conversation that was
                # never encrypted, may fall back; everything else is fail-closed.
                if not await self._omemo.plaintext_allowed(bare_jid(sender)):
                    raise
                log.info(
                    "recipient advertises no OMEMO device; sending in plaintext (opportunistic policy)",
                    event="omemo_plaintext_sent",
                    to=to,
                    frm=sender,
                )
                plain = self.xmpp.make_message(mto=to, mbody=body, mtype=mtype, mfrom=sender)
                if msg_id:
                    plain["id"] = msg_id
                for element in (elements if plain_elements is None else plain_elements):
                    plain.xml.append(element)
                await self._send(plain)
                return
        except Exception as exc:  # noqa: BLE001 - every failure is fail-closed
            log.error(
                "could not encrypt; the message was NOT sent (no plaintext fallback)",
                event="omemo_send_failed",
                to=to,
                frm=sender,
                error=str(exc) if isinstance(exc, Omemo2Error) else type(exc).__name__,
            )
            await self._send_failure_notice(to=to, sender=sender)
            raise Omemo2Error("message to " + to + " not delivered: OMEMO encryption failed") from exc

        # One stanza carries every backend's <encrypted/> (each device has a key in
        # exactly one of them), so a device never sees a second, keyless copy.
        msg = self.xmpp.make_message(mto=to, mbody=OMEMO_FALLBACK_BODY, mtype=mtype, mfrom=sender)
        if msg_id:
            msg["id"] = msg_id
        for element in encrypted:
            msg.xml.append(element)
        for element in elements:
            msg.xml.append(element)
        ET.SubElement(msg.xml, "{" + HINTS_NS + "}store")
        namespace = encrypted[0].tag[1:].split("}", 1)[0]
        ET.SubElement(msg.xml, "{" + EME_NS + "}encryption", {"namespace": namespace, "name": "OMEMO"})
        await self._send(msg)

    async def _send_failure_notice(self, *, to: str, sender: str) -> None:
        """Tell the owner (plaintext, no content) that a message was withheld."""
        if self._omemo is not None and not self._omemo.notice_due(bare_jid(sender)):
            return
        text = (
            "A message from this contact could not be delivered encrypted "
            "(no OMEMO device found for you, or the lookup failed). It was NOT "
            "sent unencrypted. Enable OMEMO in your client and check that its "
            "device list is public."
        )
        try:
            msg = self.xmpp.make_message(mto=to, mbody=text, mtype="chat", mfrom=sender)
            await self._send(msg)
        except Exception:  # noqa: BLE001
            log.debug("could not send the OMEMO failure notice", exc_info=True, event="omemo_notice_error")

    async def _send_refusal(self, *, owner_msg, text: str) -> None:
        """An error-type reply to the owner's stanza, from the peer JID it was sent to."""
        try:
            err = self.xmpp.make_message(
                mto=str(owner_msg["from"]), mtype="error", mfrom=str(owner_msg["to"])
            )
            err["id"] = str(owner_msg["id"] or "")
            err["error"]["type"] = "modify"
            err["error"]["condition"] = "not-acceptable"
            err["error"]["text"] = text
            await self._send(err)
        except Exception:  # noqa: BLE001
            log.debug("could not send the refusal", exc_info=True, event="omemo_refusal_error")

    # -- IQ (PEP emulation) -------------------------------------------------------

    async def _on_iq_get(self, iq) -> None:
        try:
            await self._handle_iq_get(iq)
        except Exception:  # noqa: BLE001 - never leave an IQ unanswered
            log.exception("failed to answer an IQ", event="component_iq_error")
            try:
                await self._reply_iq_error(iq, "internal-server-error", "wait")
            except Exception:  # noqa: BLE001
                pass

    async def _handle_iq_get(self, iq) -> None:
        from .component_omemo import PepError, disco_info_result, disco_items_result

        sender = bare_jid(str(iq["from"] or ""))
        target = bare_jid(str(iq["to"] or ""))
        if sender != self._owner:
            log.warning(
                "DENYING IQ from a non-owner JID",
                event="component_iq_denied",
                from_jid=str(iq["from"] or ""),
            )
            await self._reply_iq_error(iq, "service-unavailable", "cancel")
            return
        if self._omemo is None:
            await self._reply_iq_error(iq, "service-unavailable", "cancel", "OMEMO unavailable")
            return
        if not self._omemo.is_peer(target):
            await self._reply_iq_error(iq, "service-unavailable", "cancel")
            return

        try:
            query = iq.xml.find("{" + DISCO_INFO_NS + "}query")
            if query is not None:
                payload = disco_info_result(query.get("node"))
            elif iq.xml.find("{" + DISCO_ITEMS_NS + "}query") is not None:
                payload = disco_items_result()
            else:
                items = iq.xml.find("{" + PUBSUB_NS + "}pubsub/{" + PUBSUB_NS + "}items")
                if items is None:
                    await self._reply_iq_error(iq, "feature-not-implemented", "cancel")
                    return
                ids = [i.get("id") or "" for i in items.findall("{" + PUBSUB_NS + "}item")]
                payload = await self._omemo.serve_items(target, items.get("node") or "", ids)
        except PepError as exc:
            await self._reply_iq_error(iq, exc.condition, exc.etype, exc.text)
            return
        reply = iq.reply()
        reply.xml.append(payload)
        await self._send(reply)

    async def _reply_iq_error(self, iq, condition: str, etype: str, text: str = "") -> None:
        reply = iq.reply()
        reply["type"] = "error"
        reply["error"]["condition"] = condition
        reply["error"]["type"] = etype
        if text:
            reply["error"]["text"] = text
        await self._send(reply)

    # -- inbound stanzas ---------------------------------------------------

    async def _on_message(self, msg) -> None:
        try:
            await self._handle_message(msg)
        except Exception:  # noqa: BLE001 - a malformed stanza must not kill the stream
            log.exception("ignoring malformed component message", event="component_message_error")

    async def _handle_message(self, msg) -> None:
        mtype = (msg["type"] or "normal")
        if mtype not in ("chat", "normal"):
            log.debug("ignoring non-chat stanza", event="component_ignored", type=str(mtype))
            return

        sender = bare_jid(str(msg["from"] or ""))
        if sender != self._owner:
            log.warning(
                "DENYING stanza from a non-owner JID (component mode is deny-by-default)",
                event="component_denied",
                from_jid=str(msg["from"] or ""),
                owner=self._owner,
            )
            return

        if self._calls is not None and await self._calls.on_message(msg):
            return

        to_jid = str(msg["to"] or "")
        handle = jid_to_handle(to_jid, self.cfg.component_domain)
        if not handle:
            log.warning(
                "ignoring stanza to an unknown or malformed component JID",
                event="component_unknown_jid",
                from_jid=sender,
                to_jid=to_jid,
            )
            return

        body = await self._inbound_body(msg, to_jid=to_jid)
        if body is None:
            return
        features = parse_features(getattr(msg, "xml", None))
        text = strip_fallback(str(body), features) if body else ""
        oob_url = parse_oob_url(getattr(msg, "xml", None))
        if oob_url and self.cfg.omemo and oob_url.strip() not in text:
            # The outer <x/> is unauthenticated plaintext; under OMEMO the URL must
            # also be in the decrypted body, which is the trusted copy.
            log.warning("ignoring a cleartext OOB URL that is not in the encrypted body", event="omemo_oob_ignored")
            oob_url = None
        if not text.strip() and not features.interesting and not oob_url:
            # Plain receipts (XEP-0184 <received/>), carbons: nothing to act on.
            log.debug("ignoring stanza without text", event="component_no_body", to_jid=to_jid)
            return

        if self._handler is None:
            log.warning("no incoming handler registered; dropping message", event="xmpp_no_handler")
            return

        marker_kind, marker_id = features.marker or (None, None)
        await self._handler(
            IncomingXmpp(
                from_jid=str(msg["from"]),
                body=text,
                mtype="chat",
                to_jid=to_jid,
                msg_id=features.msg_id,
                receipt_request=features.receipt_request,
                markable=features.markable,
                displayed_id=marker_id if marker_kind == "displayed" else None,
                chat_state=features.chat_state,
                reactions_id=features.reactions_id,
                reactions=features.reactions,
                reply_id=features.reply_id,
                replace_id=features.replace_id,
                retract_id=features.retract_id,
                oob_url=oob_url,
            )
        )

    async def _inbound_body(self, msg, *, to_jid: str):
        """The text of an owner message: decrypted when OMEMO is on, else the body.

        Returns None when the stanza is dropped (undecryptable, or plaintext
        while OMEMO is on).  OMEMO never reaches here for a non-owner: the
        owner check precedes this call.
        """
        xml = getattr(msg, "xml", None)  # unit-test fakes carry no XML
        encrypted = None
        if xml is not None:
            # A stanza may carry both backends; OMEMO 2 is tried before legacy.
            encrypted = [
                el for el in (xml.find(OMEMO2_ENCRYPTED_TAG), xml.find(LEGACY_ENCRYPTED_TAG))
                if el is not None
            ] or None
        if not self.cfg.omemo:
            if encrypted is not None:
                log.warning(
                    "dropping an OMEMO message: XMPP_OMEMO is off",
                    event="omemo_disabled_drop",
                    to_jid=to_jid,
                )
                return None
            return msg["body"]

        if encrypted is None and self._omemo is not None and await self._omemo.plaintext_allowed(bare_jid(to_jid)):
            log.info(
                "accepting a plaintext message (opportunistic policy, conversation never encrypted)",
                event="omemo_plaintext_accepted",
                to_jid=to_jid,
            )
            return msg["body"]
        if encrypted is None:
            log.warning(
                "refusing an unencrypted message while XMPP_OMEMO is on",
                event="omemo_plaintext_refused",
                to_jid=to_jid,
            )
            if msg["body"]:
                await self._send_refusal(
                    owner_msg=msg,
                    text="XMPP_OMEMO is on: this message was not forwarded because it was not OMEMO-encrypted.",
                )
            return None
        try:
            if self._omemo is None:
                raise RuntimeError("OMEMO is not initialised")
            plaintext = None
            last_error: Optional[Exception] = None
            for element in encrypted:
                try:
                    plaintext = await self._omemo.decrypt(
                        element, peer_jid=bare_jid(to_jid), from_jid=str(msg["from"])
                    )
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 - e.g. no key for us in this element
                    last_error = exc
            if last_error is not None:
                raise last_error
        except Exception as exc:  # noqa: BLE001 - any failure drops the message
            log.error(
                "could not decrypt OMEMO message; dropping it",
                event="omemo_decrypt_error",
                from_jid=bare_jid(str(msg["from"])),
                to_jid=to_jid,
                error=str(exc) if exc.__class__.__name__ == "Omemo2Error" else type(exc).__name__,
            )
            await self._send_refusal(owner_msg=msg, text="could not decrypt this OMEMO message; not forwarded")
            return None
        return plaintext  # None for an empty (key transport) message

    async def _on_jingle_message(self, msg) -> None:
        try:
            if self._calls is None:
                return
            if bare_jid(str(msg["from"] or "")) != self._owner:
                log.warning(
                    "DENYING jingle message from a non-owner JID",
                    event="component_denied",
                    from_jid=str(msg["from"] or ""),
                )
                return
            await self._calls.on_message(msg)
        except Exception:  # noqa: BLE001
            log.exception("ignoring malformed jingle message", event="component_message_error")

    async def _on_jingle_iq(self, iq) -> None:
        await self._owner_iq(iq, "on_iq")

    async def _on_disco_iq(self, iq) -> None:
        await self._owner_iq(iq, "on_disco_info")

    async def _owner_iq(self, iq, method: str) -> None:
        try:
            if self._calls is None:
                return
            if bare_jid(str(iq["from"] or "")) != self._owner:
                log.warning(
                    "DENYING iq from a non-owner JID",
                    event="component_denied",
                    from_jid=str(iq["from"] or ""),
                )
                return
            await getattr(self._calls, method)(iq)
        except Exception:  # noqa: BLE001
            log.exception("ignoring malformed iq", event="component_iq_error")

    async def _on_presence_subscribe(self, pres) -> None:
        try:
            await self._handle_presence_subscribe(pres)
        except Exception:  # noqa: BLE001
            log.exception("ignoring malformed subscription stanza", event="component_presence_error")

    async def _handle_presence_subscribe(self, pres) -> None:
        sender = str(pres["from"] or "")
        if bare_jid(sender) != self._owner:
            log.warning(
                "DENYING presence subscription from a non-owner JID",
                event="component_presence_denied",
                from_jid=sender,
            )
            return
        peer = bare_jid(str(pres["to"] or ""))
        if not jid_to_handle(peer, self.cfg.component_domain):
            log.warning(
                "ignoring subscription to an unknown component JID",
                event="component_presence_unknown_jid",
                to_jid=str(pres["to"] or ""),
            )
            return
        # Make the contact look like a normal presence-capable contact.
        await self._send_presence(to=self._owner, frm=peer, ptype="subscribed")
        await self._send_presence(to=self._owner, frm=peer, ptype="available")
        log.info(
            "accepted presence subscription for a peer JID",
            event="component_subscribed",
            peer=peer,
        )

    async def _on_presence_probe(self, pres) -> None:
        try:
            sender = bare_jid(str(pres["from"] or ""))
            if sender != self._owner:
                log.warning(
                    "DENYING presence probe from a non-owner JID",
                    event="component_presence_denied",
                    from_jid=str(pres["from"] or ""),
                )
                return
            peer = bare_jid(str(pres["to"] or ""))
            if not jid_to_handle(peer, self.cfg.component_domain):
                return
            await self._send_presence(to=self._owner, frm=peer, ptype="available")
        except Exception:  # noqa: BLE001
            log.exception("ignoring malformed presence probe", event="component_presence_error")

    # -- helpers -----------------------------------------------------------

    async def _send_presence(self, *, to: str, frm: str, ptype: str) -> None:
        presence = self.xmpp.make_presence(pto=to, pfrom=frm, ptype=ptype)
        await self._send(presence)

    async def _send(self, stanza) -> None:
        result = stanza.send()
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result

    def _on_session_start(self, _event=None) -> None:
        log.info(
            "XEP-0114 component handshake accepted",
            event="component_session_start",
            jid=self.cfg.component_jid,
            domain=self.cfg.component_domain,
        )
        self._handshaken = True
        if self._session_started is not None and not self._session_started.done():
            self._session_started.set_result(True)

    def _on_disconnected(self, reason=None) -> None:
        log.warning("component connection lost", event="component_disconnected", reason=str(reason))
        session = self._session_started
        if session is not None and not session.done():
            # The stream died before XEP-0114's <handshake/> was accepted (a bad
            # secret, or the server closed the port). Fail the handshake so the
            # start loop retries with backoff instead of waiting forever; do NOT
            # set _disconnected here or the backoff would be skipped.
            session.set_exception(
                RuntimeError("component connection closed before the handshake")
            )
            return
        if self._handshaken and self._disconnected is not None:
            self._disconnected.set()

    async def _sleep_or_stop(self, delay: float) -> None:
        assert self._disconnected is not None
        try:
            await asyncio.wait_for(self._disconnected.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            pass
