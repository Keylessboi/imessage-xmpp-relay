"""The bridge itself: routing between SSE events and XMPP stanzas.

Everything here depends only on the tiny XmppTransport protocol, so the whole
routing layer can be exercised with a fake transport in tests.
"""
from __future__ import annotations

import copy
import os
from collections import Counter, OrderedDict
from dataclasses import replace
from typing import Optional

from .component import jid_to_handle, peer_jid
from .attachments import Attachments
from .config import Config
from .dedup import DedupCache
from .logsetup import get_logger
from .muc import maybe_make_group_service
from .models import (
    IMessageEvent,
    IncomingXmpp,
    OutgoingXmpp,
    bare_jid,
    normalize_handle,
    parse_target_prefix,
)
from .outbound import FetchError, fetch_to_tempfile, guess_name
from .rich import RICH_EVENTS, RichFeatures
from .upstream import UpstreamClient, UpstreamError
from .xmpp_side import XmppTransport

log = get_logger("relay.core")


class ReplyTracker:
    """Bounded memory of which inbound guid a conversation last produced.

    Used to fill the reply_to field of POST /im/send so an iMessage reply is
    threaded onto the message the user is answering.
    """

    def __init__(self, maxsize: int = 1024) -> None:
        self.maxsize = maxsize
        self._items: "OrderedDict[str, str]" = OrderedDict()

    def remember(self, key: Optional[str], guid: Optional[str]) -> None:
        if not key or not guid:
            return
        key = key.lower()
        if key in self._items:
            del self._items[key]
        self._items[key] = guid
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def get(self, key: Optional[str]) -> Optional[str]:
        if not key:
            return None
        return self._items.get(key.lower())


def plan_component_delivery(cfg: Config, event: IMessageEvent) -> list[OutgoingXmpp]:
    """Component mode: every peer is its own JID, the owner gets the message.

    The stanza is sent FROM the peer's JID (e.g. +15557654321@domain) so the
    user's client shows the person, not the bridge. A handle that cannot be
    encoded as a localpart falls back to the component's own JID and keeps the
    "Name (handle): " prefix so the message is not lost.
    """
    out: list[OutgoingXmpp] = []
    frm = peer_jid(event.sender, cfg.component_domain)
    if not event.is_group and frm is None:
        log.warning(
            "handle cannot be encoded as a component JID; falling back to the component JID",
            event="component_unmappable_handle",
            guid=event.guid,
            sender=event.sender,
        )
    prefix = ""
    if event.is_group:
        prefix = "[" + event.group_label + "] "
        if event.sender:
            prefix += event.label + ": "
    elif frm is None:
        prefix = (event.label + ": ") if event.label else ""
    for target in cfg.targets:
        out.append(
            OutgoingXmpp(to=target, body=prefix + event.text, mtype="chat", frm=frm)
        )
    return out


def plan_delivery(cfg: Config, event: IMessageEvent) -> list[OutgoingXmpp]:
    """Map one iMessage event onto zero or more XMPP stanzas."""
    if cfg.mode == "component":
        return plan_component_delivery(cfg, event)

    out: list[OutgoingXmpp] = []

    if event.is_group:
        body = (event.label + ": " + event.text) if event.sender else event.text
        if cfg.muc:
            out.append(
                OutgoingXmpp(to=cfg.muc, body=body, mtype="groupchat", muc_nick=event.label)
            )
            return out
        for target in cfg.targets:
            out.append(
                OutgoingXmpp(
                    to=target,
                    body="[" + event.group_label + "] " + body,
                    mtype="chat",
                )
            )
        return out

    contact_jid = cfg.contacts.get(normalize_handle(event.sender))
    if contact_jid:
        out.append(OutgoingXmpp(to=contact_jid, body=event.text))
        return out

    body = (event.label + ": " + event.text) if cfg.prefix_sender else event.text
    for target in cfg.targets:
        out.append(OutgoingXmpp(to=target, body=body))
    return out


def looks_like_handle(value: Optional[str]) -> bool:
    if not value:
        return False
    value = value.strip()
    if "@" in value and " " not in value:
        return True
    digits = sum(ch.isdigit() for ch in value)
    return value.startswith("+") and digits >= 7


class Bridge:
    """Wires the SSE subscriber and the XMPP transport together."""

    def __init__(
        self,
        cfg: Config,
        transport: XmppTransport,
        upstream: UpstreamClient,
        dedup: Optional[DedupCache] = None,
        replies: Optional[ReplyTracker] = None,
    ) -> None:
        self.cfg = cfg
        self.transport = transport
        self.upstream = upstream
        self.dedup = dedup or DedupCache(cfg.dedup_size)
        self.replies = replies or ReplyTracker()
        self.stats: Counter = Counter()
        self._deny_logged = False
        self.rich = RichFeatures(cfg, transport, upstream, self._notify, self.stats)
        self.attachments = Attachments(cfg, transport, self.stats)
        transport.set_incoming_handler(self.on_xmpp_message)
        # Component mode only: iMessage groups as XEP-0045 rooms (relay/muc.py).
        self.groups = maybe_make_group_service(cfg, transport, upstream)
        self.attachments.groups = self.groups

    # -- iMessage -> XMPP --------------------------------------------------

    async def on_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "ready":
            log.info("imessage-server reported ready", event="upstream_ready")
            if self.groups is not None:
                await self.groups.hydrate()
            return
        if etype == "group":
            if self.groups is not None:
                await self.groups.on_group_event(event)
            return
        if etype == "attachment":
            await self.attachments.on_event(event)
            return
        if etype in RICH_EVENTS:
            await self.rich.on_event(str(etype), event)
            return
        if etype != "message":
            log.debug("ignoring event", event="upstream_event_ignored", type=str(etype))
            return

        message = IMessageEvent.from_event(event)
        if not self.dedup.check_and_add(message.guid):
            self.stats["deduped"] += 1
            log.info(
                "dropping replayed message",
                event="dedup_drop",
                guid=message.guid,
                sender=message.sender,
            )
            return

        message, extras = self.rich.decorate(message)
        if self.groups is not None and message.is_group:
            # Rooms get placeholders and the "> quoted" reply fallback as plain text;
            # per-message ids/receipts are not used there (the room stanza id is the guid,
            # so XEP-0461 replies from the owner still resolve).
            self.rich.remember_inbound(message)
            room_msg = message
            if extras is not None and extras.quote_prefix:
                room_msg = replace(message, text=extras.quote_prefix + message.text)
            if await self.groups.on_message(room_msg):
                self.stats["delivered"] += 1
                return

        outgoing = plan_delivery(self.cfg, message)
        if not outgoing:
            self.stats["unrouted"] += 1
            log.warning(
                "no XMPP route for iMessage",
                event="unrouted",
                guid=message.guid,
                sender=message.sender,
                is_group=message.is_group,
            )
            return

        self.rich.remember_inbound(message)
        for stanza in outgoing:
            self.replies.remember(message.sender, message.guid)
            self.replies.remember(stanza.to, message.guid)
            body = stanza.body
            # Component mode only: stanza id, receipt/marker requests, reply payload.
            rich_kwargs: dict = {}
            if extras is not None:
                body = extras.quote_prefix + body
                rich_kwargs = {
                    "msg_id": extras.msg_id,
                    "elements": [copy.deepcopy(e) for e in extras.elements],
                }
            try:
                await self.transport.send_message(
                    to=stanza.to,
                    body=body,
                    mtype=stanza.mtype,
                    muc_nick=stanza.muc_nick,
                    frm=stanza.frm,
                    **rich_kwargs,
                )
            except Exception:
                self.stats["delivery_errors"] += 1
                log.exception(
                    "failed to deliver stanza",
                    event="xmpp_send_error",
                    guid=message.guid,
                    to=stanza.to,
                )
                continue
            self.stats["delivered"] += 1
            log.info(
                "delivered iMessage to XMPP",
                event="delivered",
                guid=message.guid,
                to=stanza.to,
                mtype=stanza.mtype,
                group=message.is_group,
            )

    # -- XMPP -> iMessage --------------------------------------------------

    async def on_xmpp_message(self, message: IncomingXmpp) -> None:
        if message.mtype == "error":
            return
        if not message.body.strip() and not message.has_features:
            return

        if not self._is_allowed(message):
            self.stats["denied"] += 1
            if not self._deny_logged:
                self._deny_logged = True
                log.warning(
                    "DENYING inbound XMPP message: sender is not in XMPP_ALLOWED "
                    "(empty XMPP_ALLOWED denies everyone)",
                    event="denied",
                    from_jid=message.from_jid,
                    allowed=sorted(self.cfg.allowed) if self.cfg.allowed else "<empty>",
                )
            else:
                log.debug("denied inbound XMPP message", event="denied", from_jid=message.from_jid)
            return

        handle, text = self._resolve_target(message)
        if not handle:
            self.stats["no_target"] += 1
            log.warning(
                "cannot resolve an iMessage destination for inbound XMPP message",
                event="no_target",
                from_jid=message.from_jid,
            )
            await self._notify(
                message,
                "No iMessage destination known for this sender. Address the message "
                "as '+15551234567: text', or set IMESSAGE_DEFAULT_TO.",
            )
            return

        # Receipts, typing, reactions, corrections and retractions (component mode).
        if await self.rich.on_xmpp(message, handle):
            return

        if message.oob_url:
            await self._send_oob(message, handle, text)
            return

        if not text.strip():
            return

        reply_to = (
            self.rich.reply_guid(message)
            or self.replies.get(handle)
            or self.replies.get(message.bare)
        )
        try:
            result = await self.upstream.send(handle, text, reply_to=reply_to)
        except UpstreamError as exc:
            self.stats["send_errors"] += 1
            log.error(
                "imessage-server rejected the send",
                event="upstream_send_error",
                to=handle,
                error=str(exc),
            )
            await self._notify(message, "iMessage send failed: " + str(exc))
            return

        self.stats["sent"] += 1
        self.rich.record_outbound(message, handle, text, result)
        log.info(
            "sent XMPP message to iMessage",
            event="sent",
            to=handle,
            from_jid=message.from_jid,
            reply_to=reply_to,
            guid=(result or {}).get("guid"),
        )

    async def _send_oob(self, message: IncomingXmpp, handle: str, text: str) -> None:
        """XEP-0066: fetch the URL the owner sent and deliver it as an iMessage attachment."""
        url = message.oob_url or ""
        try:
            path, ctype, size = await fetch_to_tempfile(
                url,
                max_bytes=self.cfg.outbound_max_bytes,
                timeout=self.cfg.outbound_fetch_timeout,
            )
        except FetchError as exc:
            self.stats["oob_refused"] += 1
            log.warning("refused an outbound attachment URL", event="oob_refused", error=str(exc))
            await self._notify(message, "Not sent: " + str(exc))
            return
        name = guess_name(url)
        try:
            result = await self.upstream.send_attachment(handle, path, name, ctype)
        except UpstreamError as exc:
            self.stats["send_errors"] += 1
            log.error("imessage-server rejected the attachment", event="upstream_attachment_error",
                      to=handle, error=str(exc))
            await self._notify(message, "iMessage attachment failed: " + str(exc))
            return
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.stats["attachments_sent"] += 1
        self.rich.record_outbound(message, handle, "[attachment: " + name + "]", result)
        log.info("sent XMPP attachment to iMessage", event="attachment_sent", to=handle,
                 size=size, guid=(result or {}).get("guid"))
        # A caption (body that is not just the URL) goes as a normal text after the file.
        caption = text.strip()
        if caption and caption != url.strip():
            try:
                await self.upstream.send(handle, caption)
            except UpstreamError as exc:
                await self._notify(message, "iMessage caption failed: " + str(exc))

    def _is_allowed(self, message: IncomingXmpp) -> bool:
        allowed = self.cfg.allowed
        if not allowed:
            return False
        candidates = {
            message.bare,
            (message.from_jid or "").lower(),
            (message.muc_room or "").lower(),
        }
        return bool(candidates & set(allowed))

    def _resolve_target(self, message: IncomingXmpp) -> tuple[Optional[str], str]:
        if self.cfg.mode == "component":
            # The destination is the JID the message was addressed to; the peer
            # handle lives in its localpart. Only peer JIDs in our own domain
            # are accepted (the transport enforces this too).
            handle = jid_to_handle(message.to_jid or "", self.cfg.component_domain)
            if handle:
                return handle, message.body

        explicit, rest = parse_target_prefix(message.body)
        if explicit:
            return normalize_handle(explicit), rest

        mapped = self.cfg.handle_for_jid(message.from_jid) if not message.is_group else None
        if mapped:
            return mapped, message.body

        if message.is_group and looks_like_handle(message.nick):
            return normalize_handle(message.nick or ""), message.body

        if self.cfg.default_to:
            return normalize_handle(self.cfg.default_to), message.body

        return None, message.body

    async def _notify(self, message: IncomingXmpp, text: str) -> None:
        try:
            if message.is_group:
                await self.transport.send_message(
                    to=message.muc_room or bare_jid(message.from_jid),
                    body=text,
                    mtype="groupchat",
                )
            else:
                await self.transport.send_message(to=message.from_jid, body=text, mtype="chat")
        except Exception:
            log.exception("could not send notice back over XMPP", event="notice_error")
