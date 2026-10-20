"""Call-to-FaceTime-link: decline XMPP calls and answer with a FaceTime link.

The owner starts an audio/video call from their XMPP client (Conversations,
monocles chat) to a contact JID of the component. The bridge cannot carry the
XMPP call, so it declines it at once (which ends the call UI), asks the server
to start a real FaceTime call to that contact (POST /im/facetime-call: the
contact's devices ring) and posts "FaceTime: <url>" into the same chat, FROM
the contact's JID, so the owner can open the link in a browser and join.

Stanzas handled (standard XEPs only: 0353, 0166, 0167, 0030):

* XEP-0353 Jingle Message Initiation. Evidence for what the clients send is in
  bridge/README.md ("Calls"): both Conversations and monocles chat open a call
  with a type=chat <message> carrying <propose xmlns='urn:xmpp:jingle-message:0'
  id=SID><description xmlns='urn:xmpp:jingle:apps:rtp:1' media='audio|video'/>
  </propose> (plus <request/> and <store/>). We never send <proceed>, so no
  session-initiate follows. The decline is a <reject/> to the initiator's FULL
  JID (clients ignore it unless it is addressed directly), mirroring the one
  Conversations itself sends: same id, an empty <description/>, and <store/>.
* XEP-0166/0167 fallback: a Jingle session-initiate IQ is answered with an IQ
  result and a session-terminate carrying <reason><decline/></reason>.
* XEP-0030 disco#info on the component and its contact JIDs advertises the
  Jingle features, since clients hide the call button for contacts that do not.
* A plain-text command: the owner sends "facetime" (or "/facetime") to a
  contact or to the component JID itself.

The link is an ordinary FaceTime invite link (facetime.apple.com/join#...):
anyone who opens it can join, so it goes to the owner only and is never sent
over iMessage by the bridge. If the server could not start the call it falls
back to a plain link and says so ("rang": false); the message then states that
the contact was NOT rung. Ringing from a synthetic identity is unproven against
Apple. The URL is never logged.
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Any, Awaitable, Callable, Deque, Optional
from uuid import uuid4
from xml.sax.saxutils import quoteattr

from .component import jid_to_handle
from .config import Config
from .logsetup import get_logger
from .models import bare_jid
from .upstream import UpstreamError

log = get_logger("relay.calls")

NS_JINGLE_MESSAGE = "urn:xmpp:jingle-message:0"
NS_JINGLE = "urn:xmpp:jingle:1"
NS_RTP = "urn:xmpp:jingle:apps:rtp:1"
NS_DISCO_INFO = "http://jabber.org/protocol/disco#info"
NS_HINTS = "urn:xmpp:hints"

DISCO_FEATURES = (
    NS_DISCO_INFO,
    NS_JINGLE_MESSAGE,
    NS_JINGLE,
    NS_RTP,
    "urn:xmpp:jingle:apps:rtp:audio",
    "urn:xmpp:jingle:apps:rtp:video",
)

COMMANDS = frozenset({"facetime", "/facetime"})
# Inbound Jingle Message elements that are not a new call; logged, never acted on.
_JM_OTHER = frozenset({"proceed", "ringing", "retract", "reject", "finish"})
# Each request can ring a real person: two per minute at most, so a stuck
# client retrying a call cannot hammer Apple or the contact.
RATE_BURST = 2
RATE_WINDOW = 60.0
_SEEN_MAX = 256


def is_command(body: Optional[str]) -> bool:
    """True for the plain-text FaceTime command."""
    return (body or "").strip().lower() in COMMANDS


def jingle_message_kind(xml: Any) -> Optional[tuple[str, str, list[str]]]:
    """(element name, session id, media list) of a XEP-0353 child, or None."""
    if xml is None:
        return None
    for child in xml:
        tag = child.tag
        if not isinstance(tag, str) or not tag.startswith("{" + NS_JINGLE_MESSAGE + "}"):
            continue
        name = tag.split("}", 1)[1]
        media = [
            d.get("media", "")
            for d in child
            if d.tag == "{" + NS_RTP + "}description"
        ]
        return name, child.get("id", "") or "", media
    return None


def jingle_iq_info(xml: Any) -> Optional[tuple[str, str]]:
    """(action, sid) of a Jingle IQ payload, or None when it is not Jingle."""
    if xml is None:
        return None
    for child in xml:
        if child.tag == "{" + NS_JINGLE + "}jingle":
            return child.get("action", "") or "", child.get("sid", "") or ""
    return None


def build_reject(*, frm: str, to: str, sid: str) -> str:
    """XEP-0353 <reject/> as Conversations sends it (see module docstring)."""
    return (
        "<message type='chat' from=" + quoteattr(frm) + " to=" + quoteattr(to) + ">"
        "<reject xmlns='" + NS_JINGLE_MESSAGE + "' id=" + quoteattr(sid) + ">"
        "<description xmlns='" + NS_RTP + "'/></reject>"
        "<store xmlns='" + NS_HINTS + "'/></message>"
    )


def build_iq_result(*, frm: str, to: str, iq_id: str) -> str:
    return (
        "<iq type='result' from=" + quoteattr(frm) + " to=" + quoteattr(to)
        + " id=" + quoteattr(iq_id) + "/>"
    )


def build_session_terminate(*, frm: str, to: str, sid: str) -> str:
    return (
        "<iq type='set' from=" + quoteattr(frm) + " to=" + quoteattr(to)
        + " id=" + quoteattr("ft-" + uuid4().hex[:12]) + ">"
        "<jingle xmlns='" + NS_JINGLE + "' action='session-terminate' sid="
        + quoteattr(sid) + "><reason><decline/></reason></jingle></iq>"
    )


def build_disco_info(*, frm: str, to: str, iq_id: str, category: str, ctype: str) -> str:
    feats = "".join("<feature var=" + quoteattr(f) + "/>" for f in DISCO_FEATURES)
    return (
        "<iq type='result' from=" + quoteattr(frm) + " to=" + quoteattr(to)
        + " id=" + quoteattr(iq_id) + "><query xmlns='" + NS_DISCO_INFO + "'>"
        "<identity category=" + quoteattr(category) + " type=" + quoteattr(ctype)
        + " name='iMessage'/>" + feats + "</query></iq>"
    )


class RateLimiter:
    """At most `burst` events per `window` seconds (sliding window)."""

    def __init__(self, burst: int = RATE_BURST, window: float = RATE_WINDOW,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.burst = burst
        self.window = window
        self._clock = clock
        self._hits: Deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._hits and now - self._hits[0] >= self.window:
            self._hits.popleft()
        if len(self._hits) >= self.burst:
            return False
        self._hits.append(now)
        return True


class CallService:
    """Owner-only call handling for the component transport.

    `transport` supplies send_message (posts the link) and send_raw (the
    Jingle stanzas); `upstream` supplies facetime_link(). Every method that
    takes a stanza expects the transport to have verified the owner already.
    """

    def __init__(
        self,
        cfg: Config,
        upstream: Any,
        send_message: Callable[..., Awaitable[None]],
        send_raw: Callable[[str], Any],
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        self.cfg = cfg
        self.upstream = upstream
        self._send_message = send_message
        self._send_raw = send_raw
        self._limiter = limiter or RateLimiter()
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._owner = bare_jid(cfg.component_owner)

    # -- helpers -----------------------------------------------------------

    def _is_ours(self, jid: str) -> bool:
        bare = bare_jid(jid)
        return bare == bare_jid(self.cfg.component_jid) or bool(
            jid_to_handle(bare, self.cfg.component_domain)
        )

    def _first_time(self, sid: str) -> bool:
        if not sid:
            return True
        if sid in self._seen:
            return False
        self._seen[sid] = None
        while len(self._seen) > _SEEN_MAX:
            self._seen.popitem(last=False)
        return True

    async def _raw(self, xml: str) -> None:
        result = self._send_raw(xml)
        if hasattr(result, "__await__"):
            await result

    async def post_link(self, frm: str, trigger: str, video: bool = True) -> None:
        """Start the call (or mint a plain link) and post it to the owner from `frm`."""
        handle = jid_to_handle(frm, self.cfg.component_domain)
        if not self._limiter.allow():
            body = "FaceTime not started: too many requests, try again in a minute."
            log.warning("facetime request rate-limited", event="facetime_rate_limited", trigger=trigger)
        else:
            try:
                if handle:
                    url, rang, note = await self.upstream.facetime_call(handle, video)
                else:
                    # Command sent to the component itself: no contact to ring.
                    url, rang, note = await self.upstream.facetime_link(), False, None
            except UpstreamError as exc:
                log.warning(
                    "facetime request failed",
                    event="facetime_link_error",
                    trigger=trigger,
                    error=str(exc),
                )
                body = "FaceTime unavailable: " + str(exc)
            else:
                body = "FaceTime: " + url + "\n" + (
                    "Ringing the contact now."
                    if rang
                    else "The contact was NOT rung"
                    + (" (" + note + ")" if note else "")
                    + "; share the link yourself if you want them to join."
                )
                log.info("facetime link posted", event="facetime_link_sent",
                         trigger=trigger, rang=rang)
        try:
            await self._send_message(to=self._owner, body=body, mtype="chat", frm=frm)
        except Exception:  # noqa: BLE001
            log.exception("could not post the facetime link", event="facetime_post_error")

    # -- stanza entry points ----------------------------------------------

    async def on_message(self, msg: Any) -> bool:
        """Handle a XEP-0353 message or the text command; True when consumed."""
        to_jid = str(msg["to"] or "")
        from_jid = str(msg["from"] or "")
        info = jingle_message_kind(getattr(msg, "xml", None))
        if info is not None:
            name, sid, media = info
            # Debug aid for capturing what a client really sends: kind only.
            log.debug("jingle message stanza", event="jingle_message", kind=name,
                      media=",".join(media))
            if name != "propose":
                return True
            if not self._is_ours(to_jid):
                return True
            # A client retry gets the same decline again but no second link.
            await self._raw(build_reject(frm=to_jid, to=from_jid, sid=sid))
            if self._first_time(sid):
                await self.post_link(bare_jid(to_jid), "propose", video="audio" not in media or "video" in media)
            return True
        if is_command(msg["body"]) and self._is_ours(to_jid):
            log.debug("facetime text command", event="facetime_command")
            await self.post_link(bare_jid(to_jid), "command")
            return True
        return False

    async def on_iq(self, iq: Any) -> bool:
        """Handle a Jingle IQ from the owner (decline it); True when consumed."""
        info = jingle_iq_info(getattr(iq, "xml", None))
        if info is None or str(iq["type"]) != "set":
            return False
        action, sid = info
        to_jid = str(iq["to"] or "")
        from_jid = str(iq["from"] or "")
        log.debug("jingle iq stanza", event="jingle_iq", kind=action)
        await self._raw(build_iq_result(frm=to_jid, to=from_jid, iq_id=str(iq["id"] or "")))
        if action == "session-initiate" and self._is_ours(to_jid):
            await self._raw(build_session_terminate(frm=to_jid, to=from_jid, sid=sid))
            if self._first_time(sid):
                await self.post_link(bare_jid(to_jid), "session-initiate")
        return True

    async def on_disco_info(self, iq: Any) -> bool:
        """Answer disco#info for the component and contact JIDs (XEP-0030)."""
        to_jid = str(iq["to"] or "")
        if str(iq["type"]) != "get" or not self._is_ours(to_jid):
            return False
        xml = getattr(iq, "xml", None)
        query = xml.find("{" + NS_DISCO_INFO + "}query") if xml is not None else None
        if query is None or query.get("node"):
            return False
        is_component = bare_jid(to_jid) == bare_jid(self.cfg.component_jid)
        await self._raw(
            build_disco_info(
                frm=to_jid,
                to=str(iq["from"] or ""),
                iq_id=str(iq["id"] or ""),
                category="gateway" if is_component else "account",
                ctype="imessage" if is_component else "registered",
            )
        )
        return True


__all__ = ["CallService", "RateLimiter", "is_command"]
