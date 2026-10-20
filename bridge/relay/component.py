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

from .config import Config
from .logsetup import get_logger
from .models import IncomingXmpp, bare_jid, normalize_handle
from .upstream import Backoff
from .xep import parse_features, parse_oob_url, strip_fallback
from .xmpp_side import IncomingHandler

log = get_logger("relay.component")

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

        if xmpp is None:
            import slixmpp  # imported lazily so tests can inject a fake

            xmpp = slixmpp.ComponentXMPP(
                cfg.component_jid,
                cfg.component_secret,
                cfg.component_host,
                cfg.component_port,
            )
        self.xmpp = xmpp
        self._register_handlers()
        self._advertise_features(("jabber:x:oob",))

    # -- wiring ------------------------------------------------------------

    def _register_handlers(self) -> None:
        self.xmpp.add_event_handler("session_start", self._on_session_start)
        self.xmpp.add_event_handler("message", self._on_message)
        self.xmpp.add_event_handler("presence_subscribe", self._on_presence_subscribe)
        self.xmpp.add_event_handler("presence_probe", self._on_presence_probe)
        self.xmpp.add_event_handler("disconnected", self._on_disconnected)

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

    # -- XmppTransport -----------------------------------------------------

    async def start(self) -> None:
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
        msg = self.xmpp.make_message(mto=target, mbody=body or None, mtype=mtype, mfrom=sender)
        if msg_id:
            msg["id"] = msg_id
        for element in elements:
            # Feature payloads (receipts, reactions, ...) are plain etree elements.
            msg.xml.append(element)
        await self._send(msg)

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

        body = msg["body"]
        features = parse_features(getattr(msg, "xml", None))
        text = strip_fallback(str(body), features) if body else ""
        oob_url = parse_oob_url(getattr(msg, "xml", None))
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
