"""Stanza-level building blocks for the message features (pure, no slixmpp).

Standard XEPs only:

* XEP-0184 message delivery receipts       (urn:xmpp:receipts)
* XEP-0333 chat markers                    (urn:xmpp:chat-markers:0)
* XEP-0085 chat state notifications        (http://jabber.org/protocol/chatstates)
* XEP-0444 message reactions               (urn:xmpp:reactions:0)
* XEP-0428 fallback indication             (urn:xmpp:fallback:0)
* XEP-0461 message replies                 (urn:xmpp:reply:0)
* XEP-0308 last message correction         (urn:xmpp:message-correct:0)
* XEP-0424 message retraction              (urn:xmpp:message-retract:1)
* XEP-0334 message processing hints        (urn:xmpp:hints)
* XEP-0066 out of band data                 (jabber:x:oob)

Everything is xml.etree so slixmpp stanzas (whose `.xml` is an Element) can
take the built elements via `append` and be parsed with `parse_features`.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

NS_RECEIPTS = "urn:xmpp:receipts"
NS_MARKERS = "urn:xmpp:chat-markers:0"
NS_CHATSTATES = "http://jabber.org/protocol/chatstates"
NS_REACTIONS = "urn:xmpp:reactions:0"
NS_FALLBACK = "urn:xmpp:fallback:0"
NS_REPLY = "urn:xmpp:reply:0"
NS_CORRECT = "urn:xmpp:message-correct:0"
NS_RETRACT = "urn:xmpp:message-retract:1"
NS_HINTS = "urn:xmpp:hints"
NS_OOB = "jabber:x:oob"

CHAT_STATES = ("active", "composing", "paused", "inactive", "gone")
# Chat states that mean "typing right now".
TYPING_STATES = frozenset({"composing"})

# iMessage tapback <-> a reaction emoji. Apple's own tapbacks carry no emoji;
# these are the glyphs XMPP clients show for them.
TAPBACK_EMOJI = {
    "heart": "❤️",
    "like": "\U0001f44d",
    "dislike": "\U0001f44e",
    "laugh": "\U0001f602",
    "emphasize": "‼️",
    "question": "❓",
}
_EMOJI_TAPBACK = {
    "❤️": "heart",
    "❤": "heart",
    "\U0001f44d": "like",
    "\U0001f44d\U0001f3fb": "like",
    "\U0001f44e": "dislike",
    "\U0001f602": "laugh",
    "\U0001f923": "laugh",
    "‼️": "emphasize",
    "‼": "emphasize",
    "❓": "question",
    "?": "question",
}
TAPBACK_VERB = {
    "heart": "Loved",
    "like": "Liked",
    "dislike": "Disliked",
    "laugh": "Laughed at",
    "emphasize": "Emphasized",
    "question": "Questioned",
}


def _q(ns: str, tag: str) -> str:
    return "{" + ns + "}" + tag


# Characters XML 1.0 forbids (C0 controls except tab/LF/CR, lone surrogates, U+FFFE/F).
# One of them in a stanza makes the whole stanza ill-formed and the server closes the stream.
_XML_ILLEGAL = re.compile("[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def xml_safe(text: str) -> str:
    """`text` without the characters XML 1.0 cannot carry (untrusted iMessage text goes through this)."""
    return _XML_ILLEGAL.sub("", text)


# -- emoji <-> tapback --------------------------------------------------------


def emoji_to_reaction(emoji: str) -> tuple[str, Optional[str]]:
    """XMPP reaction emoji -> (iMessage reaction name, emoji for `emoji`)."""
    name = _EMOJI_TAPBACK.get(emoji)
    if name:
        return name, None
    return "emoji", emoji


def reaction_to_emoji(reaction: str, emoji: Optional[str]) -> Optional[str]:
    """iMessage reaction event -> the emoji shown over XMPP (None: no emoji form)."""
    if reaction == "emoji":
        return emoji or None
    return TAPBACK_EMOJI.get(reaction)


# -- building -----------------------------------------------------------------


def _hint(name: str) -> ET.Element:
    return ET.Element(_q(NS_HINTS, name))


def store_hint() -> ET.Element:
    return _hint("store")


def no_store_hint() -> ET.Element:
    return _hint("no-store")


def oob(url: str, desc: Optional[str] = None) -> ET.Element:
    """XEP-0066 <x xmlns='jabber:x:oob'><url/><desc/></x>. The message body must
    be the same URL: clients that ignore the element still show a plain link."""
    el = ET.Element(_q(NS_OOB, "x"))
    ET.SubElement(el, _q(NS_OOB, "url")).text = url
    if desc:
        ET.SubElement(el, _q(NS_OOB, "desc")).text = desc
    return el


def parse_oob_url(xml: Optional[ET.Element]) -> Optional[str]:
    """The first XEP-0066 <url/> of a <message/>, or None."""
    if xml is None:
        return None
    for child in xml.findall(_q(NS_OOB, "x")):
        url = child.find(_q(NS_OOB, "url"))
        if url is not None and (url.text or "").strip():
            return url.text.strip()
    return None


def receipt_request() -> ET.Element:
    return ET.Element(_q(NS_RECEIPTS, "request"))


def markable() -> ET.Element:
    return ET.Element(_q(NS_MARKERS, "markable"))


def received_receipt(msg_id: str) -> list[ET.Element]:
    el = ET.Element(_q(NS_RECEIPTS, "received"))
    el.set("id", msg_id)
    return [el, no_store_hint()]


def displayed_marker(msg_id: str) -> list[ET.Element]:
    el = ET.Element(_q(NS_MARKERS, "displayed"))
    el.set("id", msg_id)
    return [el, no_store_hint()]


def chat_state(state: str) -> list[ET.Element]:
    if state not in CHAT_STATES:
        raise ValueError("unknown chat state: " + state)
    return [ET.Element(_q(NS_CHATSTATES, state)), no_store_hint()]


def fallback(for_ns: str, start: Optional[int] = None, end: Optional[int] = None) -> ET.Element:
    """XEP-0428 fallback indication; no range means the whole body is fallback."""
    el = ET.Element(_q(NS_FALLBACK, "fallback"))
    el.set("for", for_ns)
    if start is not None and end is not None:
        body = ET.SubElement(el, _q(NS_FALLBACK, "body"))
        body.set("start", str(start))
        body.set("end", str(end))
    return el


def reactions(target_id: str, emojis: list[str]) -> list[ET.Element]:
    """XEP-0444: the sender's FULL reaction set for one message (empty clears it)."""
    el = ET.Element(_q(NS_REACTIONS, "reactions"))
    el.set("id", target_id)
    for emoji in emojis:
        ET.SubElement(el, _q(NS_REACTIONS, "reaction")).text = emoji
    return [el, fallback(NS_REACTIONS), store_hint()]


def quote_fallback(text: str) -> str:
    """The "> quoted" lines a client without XEP-0461 shows (XEP-0428 range)."""
    lines = (text or "").splitlines() or [""]
    return "".join("> " + line + "\n" for line in lines[:3])


def reply(
    target_id: str, to_jid: Optional[str], quoted: Optional[str]
) -> tuple[str, list[ET.Element]]:
    """XEP-0461 reply elements. Returns (fallback body prefix, [reply, fallback?]).

    The caller puts the prefix at the very start of the body; the fallback
    range (0..len(prefix)) then lets a XEP-0461 client hide it.
    """
    el = ET.Element(_q(NS_REPLY, "reply"))
    el.set("id", target_id)
    if to_jid:
        el.set("to", to_jid)
    if quoted:
        prefix = quote_fallback(quoted)
        return prefix, [el, fallback(NS_REPLY, 0, len(prefix))]
    return "", [el]


def correction(original_id: str) -> list[ET.Element]:
    el = ET.Element(_q(NS_CORRECT, "replace"))
    el.set("id", original_id)
    return [el, store_hint()]


RETRACT_FALLBACK_TEXT = (
    "This person attempted to retract a previous message, but it's unsupported by your client."
)


def retraction(original_id: str) -> list[ET.Element]:
    el = ET.Element(_q(NS_RETRACT, "retract"))
    el.set("id", original_id)
    return [el, fallback(NS_RETRACT), store_hint()]


# -- parsing ------------------------------------------------------------------


@dataclass(frozen=True)
class Features:
    """What a message stanza asks for, beyond its body."""

    msg_id: Optional[str] = None
    receipt_request: bool = False
    received_id: Optional[str] = None
    markable: bool = False
    marker: Optional[tuple[str, str]] = None      # ("displayed", id) etc.
    chat_state: Optional[str] = None
    reactions_id: Optional[str] = None
    reactions: Optional[tuple[str, ...]] = None   # None: no <reactions/> element
    reply_id: Optional[str] = None
    reply_to: Optional[str] = None
    replace_id: Optional[str] = None
    retract_id: Optional[str] = None
    # (for_ns, start, end); start/end None = the whole body is fallback.
    fallbacks: tuple[tuple[str, Optional[int], Optional[int]], ...] = field(default=())

    @property
    def interesting(self) -> bool:
        """True when a body-less stanza still carries something to act on."""
        return bool(
            self.marker
            or self.chat_state
            or self.reactions is not None
            or self.retract_id
        )


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_features(xml: Optional[ET.Element]) -> Features:
    """Read the feature elements out of a <message/> element."""
    if xml is None:
        return Features()
    values: dict = {"msg_id": xml.get("id") or None}
    fallbacks: list[tuple[str, Optional[int], Optional[int]]] = []
    for child in xml:
        tag = child.tag
        if not isinstance(tag, str):
            continue  # comment / processing instruction
        if tag == _q(NS_RECEIPTS, "request"):
            values["receipt_request"] = True
        elif tag == _q(NS_RECEIPTS, "received"):
            values["received_id"] = child.get("id")
        elif tag == _q(NS_MARKERS, "markable"):
            values["markable"] = True
        elif tag in (
            _q(NS_MARKERS, "displayed"),
            _q(NS_MARKERS, "received"),
            _q(NS_MARKERS, "acknowledged"),
        ):
            if child.get("id"):
                values["marker"] = (tag.rsplit("}", 1)[1], child.get("id"))
        elif tag.startswith("{" + NS_CHATSTATES + "}"):
            state = tag.rsplit("}", 1)[1]
            if state in CHAT_STATES:
                values["chat_state"] = state
        elif tag == _q(NS_REACTIONS, "reactions"):
            if child.get("id"):
                seen: list[str] = []
                for r in child.findall(_q(NS_REACTIONS, "reaction")):
                    text = (r.text or "").strip()
                    if text and text not in seen:
                        seen.append(text)
                values["reactions_id"] = child.get("id")
                values["reactions"] = tuple(seen)
        elif tag == _q(NS_REPLY, "reply"):
            values["reply_id"] = child.get("id")
            values["reply_to"] = child.get("to")
        elif tag == _q(NS_CORRECT, "replace"):
            values["replace_id"] = child.get("id")
        elif tag == _q(NS_RETRACT, "retract"):
            values["retract_id"] = child.get("id")
        elif tag == _q(NS_FALLBACK, "fallback"):
            for_ns = child.get("for") or ""
            ranges = child.findall(_q(NS_FALLBACK, "body"))
            if not ranges:
                fallbacks.append((for_ns, None, None))
            for r in ranges:
                fallbacks.append((for_ns, _int(r.get("start")), _int(r.get("end"))))
    return Features(fallbacks=tuple(fallbacks), **values)


def strip_fallback(body: str, features: Features) -> str:
    """Remove XEP-0428 fallback text so only what the user typed is forwarded.

    A fallback with no range marks the whole body (reactions, retractions).
    Ranges are character offsets into the body.
    """
    remove: list[tuple[int, int]] = []
    for _for_ns, start, end in features.fallbacks:
        if start is None or end is None:
            return ""
        if 0 <= start <= end <= len(body):
            remove.append((start, end))
    # Merge overlaps first: cutting overlapping ranges one after another would eat typed text.
    merged: list[list[int]] = []
    for start, end in sorted(remove):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        body = body[:start] + body[end:]
    return body
