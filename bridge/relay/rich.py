"""Message-level features on top of plain text relaying (component mode).

iMessage -> XMPP: delivered/read receipts, typing, tapbacks, replies, edits,
unsends, and readable placeholders for stickers/attachments.
XMPP -> iMessage: displayed markers (read receipts), chat states (typing),
reactions, replies, corrections (edits) and retractions (unsend).

The wire shapes live in relay/xep.py; this module owns the state that ties the
two id spaces together:

* an inbound iMessage is sent to XMPP with its guid as the stanza id, so an
  XMPP reference to it (reaction, reply, displayed marker) names the guid;
* a message the owner sends gets the guid /im/send returned, remembered
  against the owner's own stanza id, so a later correction, retraction or
  reaction on it can be mapped back.

All state is in-memory and bounded; after a restart references to older
messages degrade (inbound guids still work, owner-side ids do not).
"""
from __future__ import annotations

import copy
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional

from . import xep
from .component import peer_jid
from .config import Config
from .logsetup import get_logger
from .models import IMessageEvent, IncomingXmpp, bare_jid
from .upstream import UpstreamClient, UpstreamError

log = get_logger("relay.rich")

OBJECT_REPLACEMENT = "￼"
# Events handled here; anything else keeps its previous "ignored" path.
RICH_EVENTS = frozenset(
    {"message_meta", "delivered", "read", "typing", "reaction", "edit", "unsend"}
)

Notify = Callable[[IncomingXmpp, str], Awaitable[None]]


# -- pure helpers -------------------------------------------------------------


def attachment_placeholder(info: dict) -> str:
    """One readable stand-in for an attachment or sticker (bytes are not fetched)."""
    # The names come from the sender: the glyph this module replaces must not come back in them.
    name = str(info.get("name") or "").replace(OBJECT_REPLACEMENT, "").strip()
    mime = str(info.get("mime") or "").replace(OBJECT_REPLACEMENT, "").strip()
    if info.get("sticker"):
        return "[sticker: " + name + "]" if name else "[sticker]"
    detail = name + (" (" + mime + ")" if name and mime else mime)
    return "[attachment: " + detail + "]" if detail else "[attachment]"


def render_attachments(text: str, attachments: list[dict]) -> str:
    """Replace each U+FFFC, in order, with the matching attachment placeholder.

    A U+FFFC with no matching attachment info becomes "[attachment]" so the
    object-replacement glyph never reaches the user; leftover infos are appended.
    """
    if OBJECT_REPLACEMENT not in text and not attachments:
        return text
    pending = list(attachments)
    out: list[str] = []
    for ch in text:
        if ch == OBJECT_REPLACEMENT:
            out.append(attachment_placeholder(pending.pop(0)) if pending else "[attachment]")
        else:
            out.append(ch)
    rendered = "".join(out)
    if pending:
        extra = " ".join(attachment_placeholder(info) for info in pending)
        rendered = (rendered + " " + extra).strip()
    return rendered


def tapback_text(reaction: str, emoji: Optional[str], enable: bool, quoted: Optional[str]) -> str:
    """Apple-style description, also the XEP-0444 fallback body."""
    target = "“" + quoted + "”" if quoted else "a message"
    glyph = xep.reaction_to_emoji(reaction, emoji)
    if reaction == "sticker":
        return ("Reacted with a sticker to " if enable else "Removed a sticker from ") + target
    verb = xep.TAPBACK_VERB.get(reaction)
    if enable:
        return (verb + " " + target) if verb else ("Reacted " + (glyph or "") + " to " + target)
    return "Removed " + (glyph or "a reaction") + " from " + target


def _part(event: dict) -> int:
    """The message part an edit/unsend targets; anything unreadable counts as part 1 (text-only notice)."""
    try:
        return int(event.get("part") or 0)
    except (TypeError, ValueError):
        return 1


def short(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- state --------------------------------------------------------------------


@dataclass
class MsgRec:
    """One message the bridge has relayed, in either direction."""

    guid: str
    peer: str                      # iMessage handle of the conversation peer
    text: str
    outbound: bool                 # True: sent by the owner
    xmpp_id: Optional[str]         # id on the XMPP wire (owner's id if outbound)
    owner: Optional[str] = None    # outbound: the owner JID that sent it
    want_received: bool = False    # owner asked for a XEP-0184 receipt
    want_displayed: bool = False   # owner's client is XEP-0333 markable
    is_group: bool = False
    read_sent: bool = False


class MessageIndex:
    """Bounded guid <-> XMPP id map."""

    def __init__(self, maxsize: int = 2048) -> None:
        self.maxsize = maxsize
        self._by_guid: "OrderedDict[str, MsgRec]" = OrderedDict()
        self._by_xid: dict[str, str] = {}

    def remember(self, rec: MsgRec) -> None:
        if not rec.guid:
            return
        old = self._by_guid.pop(rec.guid, None)
        if old is not None:
            self._unmap(old)
        self._by_guid[rec.guid] = rec
        if rec.xmpp_id:
            self._by_xid[rec.xmpp_id] = rec.guid
        while len(self._by_guid) > self.maxsize:
            _, evicted = self._by_guid.popitem(last=False)
            self._unmap(evicted)

    def _unmap(self, gone: MsgRec) -> None:
        """Drop `gone`'s xmpp-id mapping, but only if it is ours; another live record may share the id."""
        if not gone.xmpp_id or self._by_xid.get(gone.xmpp_id) != gone.guid:
            return
        del self._by_xid[gone.xmpp_id]
        for other in reversed(self._by_guid.values()):
            if other.xmpp_id == gone.xmpp_id:
                self._by_xid[gone.xmpp_id] = other.guid
                break

    def by_guid(self, guid: Optional[str]) -> Optional[MsgRec]:
        return self._by_guid.get(guid or "")

    def by_xmpp_id(self, xmpp_id: Optional[str]) -> Optional[MsgRec]:
        guid = self._by_xid.get(xmpp_id or "")
        return self._by_guid.get(guid) if guid else None


class ReactionSets:
    """Per (message, reactor) emoji set, because XEP-0444 always carries the full set."""

    def __init__(self, maxsize: int = 2048) -> None:
        self.maxsize = maxsize
        self._sets: "OrderedDict[tuple[str, str], list[str]]" = OrderedDict()

    def get(self, guid: str, who: str) -> list[str]:
        return list(self._sets.get((guid, who), ()))

    def set(self, guid: str, who: str, emojis: list[str]) -> None:
        key = (guid, who)
        self._sets.pop(key, None)
        if emojis:
            self._sets[key] = list(emojis)
        while len(self._sets) > self.maxsize:
            self._sets.popitem(last=False)

    def apply(self, guid: str, who: str, emoji: str, enable: bool) -> list[str]:
        current = self.get(guid, who)
        if enable and emoji not in current:
            current.append(emoji)
        elif not enable and emoji in current:
            current.remove(emoji)
        self.set(guid, who, current)
        return current


@dataclass
class OutExtras:
    """What to add to the stanza carrying an inbound iMessage."""

    msg_id: str
    elements: list
    quote_prefix: str = ""


# -- the feature engine --------------------------------------------------------


class RichFeatures:
    def __init__(
        self,
        cfg: Config,
        transport: Any,
        upstream: UpstreamClient,
        notify: Notify,
        stats: Any,
    ) -> None:
        self.cfg = cfg
        self.transport = transport
        self.upstream = upstream
        self.notify = notify
        self.stats = stats
        self.component = cfg.mode == "component"
        self.index = MessageIndex()
        self.reactions = ReactionSets()
        self._meta: "OrderedDict[str, dict]" = OrderedDict()
        self._typing: dict[str, bool] = {}

    # -- iMessage -> XMPP ----------------------------------------------------

    def note_meta(self, event: dict) -> None:
        guid = str(event.get("guid") or "")
        if not guid:
            return
        self._meta.pop(guid, None)
        self._meta[guid] = event
        while len(self._meta) > 256:
            self._meta.popitem(last=False)

    def decorate(self, message: IMessageEvent) -> tuple[IMessageEvent, Optional[OutExtras]]:
        """Apply the pending message_meta: placeholders always, XEP stanza extras in component mode."""
        meta = self._meta.pop(message.guid, None) or {}
        attachments = [a for a in (meta.get("attachments") or []) if isinstance(a, dict)]
        text = render_attachments(message.text, attachments)
        if text != message.text:
            message = replace(message, text=text)
        if not self.component:
            return message, None

        elements: list = []
        prefix = ""
        reply_guid = meta.get("reply_to")
        if reply_guid:
            target = self.index.by_guid(str(reply_guid))
            if target is not None:
                to_jid = (
                    target.owner if target.outbound else peer_jid(target.peer, self.cfg.component_domain)
                )
                prefix, reply_elements = xep.reply(
                    target.xmpp_id or target.guid, to_jid, short(target.text, 200) or None
                )
            else:
                # Unknown parent (before a restart): the guid is still the id we used for inbound ones.
                prefix, reply_elements = xep.reply(str(reply_guid), None, None)
            elements += reply_elements
        elements += [xep.markable(), xep.receipt_request(), xep.store_hint()]
        return message, OutExtras(msg_id=message.guid, elements=elements, quote_prefix=prefix)

    def remember_inbound(self, message: IMessageEvent) -> None:
        self.index.remember(
            MsgRec(
                guid=message.guid,
                peer=message.sender,
                text=message.text,
                outbound=False,
                xmpp_id=message.guid,
                is_group=message.is_group,
            )
        )

    async def on_event(self, etype: str, event: dict) -> None:
        if etype == "message_meta":
            self.note_meta(event)
            return
        if not self.component:
            log.debug("ignoring event outside component mode", event="rich_ignored", type=etype)
            return
        sender = str(event.get("from") or "")
        chat_guid = str(event.get("chat_guid") or "")
        is_group = ";+;" in chat_guid
        frm = peer_jid(sender, self.cfg.component_domain) if not is_group else None
        handler = {
            "delivered": self._on_delivered,
            "read": self._on_read,
            "typing": self._on_typing,
            "reaction": self._on_reaction,
            "edit": self._on_edit,
            "unsend": self._on_unsend,
        }.get(etype)
        if handler is None:
            return
        await handler(event, sender, frm, is_group, chat_guid)

    async def _send(self, *, frm: Optional[str], body: str = "", msg_id: Optional[str] = None,
                    elements: Optional[list] = None, to: Optional[str] = None) -> None:
        for target in ([to] if to else list(self.cfg.targets)):
            try:
                await self.transport.send_message(
                    to=target,
                    body=body,
                    mtype="chat",
                    frm=frm,
                    msg_id=msg_id,
                    elements=[copy.deepcopy(e) for e in (elements or [])],
                )
                self.stats["rich_sent"] += 1
            except Exception:
                self.stats["delivery_errors"] += 1
                log.exception("failed to deliver feature stanza", event="rich_send_error", to=target)

    def _label(self, sender: str, chat_guid: str, is_group: bool) -> str:
        return ("[" + chat_guid + "] " if is_group else "") + sender

    async def _on_delivered(self, event, sender, frm, is_group, chat_guid) -> None:
        rec = self.index.by_guid(str(event.get("guid") or ""))
        if rec is None or not rec.outbound or not rec.want_received or not rec.xmpp_id or frm is None:
            return
        await self._send(frm=frm, to=rec.owner, elements=xep.received_receipt(rec.xmpp_id))

    async def _on_read(self, event, sender, frm, is_group, chat_guid) -> None:
        rec = self.index.by_guid(str(event.get("guid") or ""))
        if rec is None or not rec.outbound or not rec.want_displayed or not rec.xmpp_id or frm is None:
            return
        await self._send(frm=frm, to=rec.owner, elements=xep.displayed_marker(rec.xmpp_id))

    async def _on_typing(self, event, sender, frm, is_group, chat_guid) -> None:
        if frm is None or is_group:
            return
        state = "composing" if event.get("typing") else "active"
        await self._send(frm=frm, elements=xep.chat_state(state))

    async def _on_reaction(self, event, sender, frm, is_group, chat_guid) -> None:
        reaction = str(event.get("reaction") or "")
        emoji = event.get("emoji")
        emoji = None if emoji is None else str(emoji)
        enable = bool(event.get("enable", True))
        to_guid = str(event.get("to_guid") or "")
        rec = self.index.by_guid(to_guid)
        quoted = short(rec.text) if rec and rec.text else None
        body = tapback_text(reaction, emoji, enable, quoted)
        glyph = xep.reaction_to_emoji(reaction, emoji)
        if frm is None or is_group:
            # No peer JID to react as (group / unencodable handle): a text notice.
            await self._send(frm=frm, body=self._label(sender, chat_guid, is_group) + ": " + body)
            return
        if glyph is None or not to_guid:
            await self._send(frm=frm, body=body)  # e.g. a sticker tapback: text only
            return
        full = self.reactions.apply(to_guid, sender, glyph, enable)
        target_id = rec.xmpp_id if rec and rec.xmpp_id else to_guid
        await self._send(frm=frm, body=body, elements=xep.reactions(target_id, full))

    async def _on_edit(self, event, sender, frm, is_group, chat_guid) -> None:
        guid = str(event.get("guid") or "")
        text = render_attachments(str(event.get("text") or ""), [])
        part = _part(event)
        rec = self.index.by_guid(guid)
        if rec is not None:
            rec.text = text
        if frm is None or is_group or part != 0:
            await self._send(frm=frm, body=self._label(sender, chat_guid, is_group)
                             + " edited a message: " + text)
            return
        target_id = rec.xmpp_id if rec and rec.xmpp_id else guid
        await self._send(frm=frm, body=text, msg_id=uuid.uuid4().hex,
                         elements=xep.correction(target_id))

    async def _on_unsend(self, event, sender, frm, is_group, chat_guid) -> None:
        guid = str(event.get("guid") or "")
        part = _part(event)
        rec = self.index.by_guid(guid)
        if frm is None or is_group or part != 0:
            await self._send(frm=frm, body=self._label(sender, chat_guid, is_group)
                             + " unsent a message")
            return
        target_id = rec.xmpp_id if rec and rec.xmpp_id else guid
        await self._send(frm=frm, body=xep.RETRACT_FALLBACK_TEXT, msg_id=uuid.uuid4().hex,
                         elements=xep.retraction(target_id))

    # -- XMPP -> iMessage ------------------------------------------------------

    def reply_guid(self, message: IncomingXmpp) -> Optional[str]:
        """The iMessage guid an XEP-0461 reply points at (stanza ids of inbound messages are guids)."""
        if not message.reply_id:
            return None
        rec = self.index.by_xmpp_id(message.reply_id)
        return rec.guid if rec else message.reply_id

    def record_outbound(self, message: IncomingXmpp, handle: str, text: str, result: Any) -> None:
        guid = (result or {}).get("guid") if isinstance(result, dict) else None
        if not guid:
            return
        self.index.remember(
            MsgRec(
                guid=str(guid),
                peer=handle,
                text=text,
                outbound=True,
                xmpp_id=message.msg_id,
                owner=bare_jid(message.from_jid),
                want_received=message.receipt_request,
                want_displayed=message.markable,
            )
        )

    async def on_xmpp(self, message: IncomingXmpp, handle: str) -> bool:
        """Handle a feature stanza. True means it was consumed (nothing else to send)."""
        if message.displayed_id:
            await self._send_read(message, handle)
            return True
        if message.reactions is not None and message.reactions_id:
            await self._send_reactions(message, handle)
            return True
        if message.retract_id:
            await self._retract(message, handle)
            return True
        if message.replace_id and message.body.strip():
            await self._edit(message, handle)
            return True
        if message.chat_state and not message.body.strip():
            await self._send_typing(message, handle)
            return True
        return False

    async def _action(self, kind: str, message: IncomingXmpp, **fields) -> Optional[dict]:
        try:
            return await self.upstream.action(kind, **fields)
        except UpstreamError as exc:
            self.stats["action_errors"] += 1
            log.error("imessage-server rejected the action", event="upstream_action_error",
                      kind=kind, error=str(exc))
            await self.notify(message, "iMessage " + kind + " failed: " + str(exc))
            return None

    async def _send_read(self, message: IncomingXmpp, handle: str) -> None:
        if not self.cfg.send_read_receipts:
            return
        rec = self.index.by_xmpp_id(message.displayed_id)
        guid = rec.guid if rec else str(message.displayed_id)
        if rec is not None:
            if rec.outbound or rec.is_group or rec.read_sent:
                return
            rec.read_sent = True
            handle = rec.peer or handle
        try:
            await self.upstream.action("read", to=handle, guid=guid)
            self.stats["read_receipts"] += 1
        except UpstreamError as exc:
            # A missing receipt is cosmetic: log, never message the owner about it.
            log.warning("could not send read receipt", event="read_receipt_error",
                        guid=guid, error=str(exc))

    async def _send_typing(self, message: IncomingXmpp, handle: str) -> None:
        if not self.cfg.send_typing:
            return
        typing = message.chat_state in xep.TYPING_STATES
        if self._typing.get(handle, False) == typing:
            return
        self._typing[handle] = typing
        try:
            await self.upstream.action("typing", to=handle, typing=typing)
        except UpstreamError as exc:
            log.warning("could not send typing state", event="typing_error", error=str(exc))

    async def _send_reactions(self, message: IncomingXmpp, handle: str) -> None:
        rec = self.index.by_xmpp_id(message.reactions_id)
        guid = rec.guid if rec else str(message.reactions_id)
        who = "owner"
        previous = self.reactions.get(guid, who)
        wanted = list(message.reactions or ())
        changes = [(e, True) for e in wanted if e not in previous] + [
            (e, False) for e in previous if e not in wanted
        ]
        current = list(previous)
        for emoji, enable in changes:
            name, custom = xep.emoji_to_reaction(emoji)
            result = await self._action(
                "react", message, to=handle, guid=guid, reaction=name, emoji=custom,
                enable=enable, part=0 if rec else None,
                to_text=(rec.text if rec else None),
            )
            if result is None:
                break
            if enable:
                current.append(emoji)
            elif emoji in current:
                current.remove(emoji)
        self.reactions.set(guid, who, current)

    async def _edit(self, message: IncomingXmpp, handle: str) -> None:
        rec = self.index.by_xmpp_id(message.replace_id)
        if rec is None or not rec.outbound:
            await self.notify(message, "Cannot edit: that message is not one this bridge sent "
                                       "(or it was sent before the bridge restarted).")
            return
        result = await self._action("edit", message, to=rec.peer, guid=rec.guid,
                                    text=message.body, part=0)
        if result is not None:
            rec.text = message.body

    async def _retract(self, message: IncomingXmpp, handle: str) -> None:
        rec = self.index.by_xmpp_id(message.retract_id)
        if rec is None or not rec.outbound:
            await self.notify(message, "Cannot unsend: that message is not one this bridge sent "
                                       "(or it was sent before the bridge restarted).")
            return
        await self._action("unsend", message, to=rec.peer, guid=rec.guid, part=0)
