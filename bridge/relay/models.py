"""Small value types shared by the SSE side and the XMPP side."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# "+15551234567: hello" or "+1 (555) 123-4567 > hello"
_HANDLE_RE = re.compile(r"^\s*(\+?[0-9][0-9()\-\s]{5,20}?)\s*[:>]\s*(.+)$", re.DOTALL)
_EMAIL_HANDLE_RE = re.compile(r"^\s*([^\s@]+@[^\s@]+)\s*[:>]\s*(.+)$", re.DOTALL)


def bare_jid(jid: str) -> str:
    """alice@example.org/phone -> alice@example.org."""
    return (jid or "").split("/", 1)[0].strip().lower()


def resource(jid: str) -> Optional[str]:
    parts = (jid or "").split("/", 1)
    return parts[1] if len(parts) == 2 and parts[1] else None


@dataclass(frozen=True)
class IncomingXmpp:
    """A message received from XMPP that may become a POST /im/send."""

    from_jid: str          # full JID as seen on the wire
    body: str
    mtype: str = "chat"    # chat | normal | groupchat
    muc_room: Optional[str] = None   # bare room JID when mtype == groupchat
    # Component mode only: the recipient JID on the wire. The peer handle is
    # encoded in its localpart (see relay/component.py).
    to_jid: Optional[str] = None
    # Message features (component mode; see relay/xep.py). All additive: a
    # plain text stanza leaves them at their defaults.
    msg_id: Optional[str] = None
    receipt_request: bool = False          # XEP-0184 <request/>
    markable: bool = False                 # XEP-0333 <markable/>
    displayed_id: Optional[str] = None     # XEP-0333 <displayed id=/>
    chat_state: Optional[str] = None       # XEP-0085 composing/active/...
    reactions_id: Optional[str] = None     # XEP-0444 target id
    reactions: Optional[tuple[str, ...]] = None  # the sender's full emoji set
    reply_id: Optional[str] = None         # XEP-0461 target id
    replace_id: Optional[str] = None       # XEP-0308 corrected message id
    retract_id: Optional[str] = None       # XEP-0424 retracted message id

    @property
    def has_features(self) -> bool:
        return bool(
            self.displayed_id
            or self.chat_state
            or self.reactions is not None
            or self.replace_id
            or self.retract_id
        )

    @property
    def bare(self) -> str:
        return bare_jid(self.from_jid)

    @property
    def nick(self) -> Optional[str]:
        return resource(self.from_jid)

    @property
    def is_group(self) -> bool:
        return self.mtype == "groupchat"


@dataclass(frozen=True)
class OutgoingXmpp:
    """A stanza the bridge wants to send."""

    to: str
    body: str
    mtype: str = "chat"             # chat | groupchat
    muc_nick: Optional[str] = None  # only meaningful for groupchat
    # Component mode only: the JID the stanza is sent FROM. Client mode leaves
    # this None and the logged-in account is the implicit sender.
    frm: Optional[str] = None


@dataclass(frozen=True)
class IMessageEvent:
    """One message event from GET /im/events."""

    guid: str
    chat_guid: str
    sender: str
    to: tuple[str, ...]
    text: str
    timestamp: int = 0
    is_group: bool = False
    sender_name: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "IMessageEvent":
        to = event.get("to") or []
        if isinstance(to, str):
            to = [to]
        return cls(
            guid=str(event.get("guid") or ""),
            chat_guid=str(event.get("chat_guid") or ""),
            sender=str(event.get("from") or ""),
            to=tuple(str(x) for x in to),
            text=str(event.get("text") or ""),
            timestamp=int(event.get("timestamp") or 0),
            is_group=bool(event.get("is_group")),
            sender_name=(str(event["sender_name"]) if event.get("sender_name") else None),
            raw=dict(event),
        )

    @property
    def label(self) -> str:
        """Human label for the sender, used when a stanza must carry it in the body."""
        if self.sender_name:
            return self.sender_name + " (" + self.sender + ")" if self.sender else self.sender_name
        return self.sender or "unknown"

    @property
    def group_label(self) -> str:
        if self.raw.get("chat_name"):
            return str(self.raw["chat_name"])
        return self.chat_guid or "group"


def parse_target_prefix(body: str) -> tuple[Optional[str], str]:
    """Parse an explicit destination out of a message body.

    '+15551234567: hello' -> ('+15551234567', 'hello').
    Returns (None, body) when the body carries no explicit target.
    """
    for pattern in (_HANDLE_RE, _EMAIL_HANDLE_RE):
        match = pattern.match(body or "")
        if match:
            handle = match.group(1).strip()
            rest = match.group(2)
            if sum(ch.isdigit() for ch in handle) >= 7:
                return handle, rest.strip()
    return None, body


def normalize_handle(value: str) -> str:
    """Canonical form of a phone/Apple-ID handle (digit-only for phone numbers)."""
    value = (value or "").strip()
    if value.startswith("+") or (value and value[0].isdigit()):
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            return "+" + digits
    return value.lower()
