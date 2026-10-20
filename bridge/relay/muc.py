"""iMessage group chats as XEP-0045 rooms hosted by the component.

Component mode only. Every iMessage group becomes a multi-user chat room in the
component's own domain; the component itself plays the MUC service, so the
ejabberd side needs no `mod_muc` change and no extra domain (see
bridge/README.md "Group chats").

    room       grp-u<32 hex>@imessage.example.org     (uppercase-UUID group id)
               grp-x<hex of the id>@imessage.example.org   (any other group id)
    occupant   room/+15557654321                        (nick = the handle)
    owner      room/<the nick the owner joined with>

The localparts cannot collide with a peer JID: `localpart_to_handle` rejects
them because `-u`/`-x` is not a hex escape.

What maps to what (all XEP-0045 unless noted):

    iMessage                          XMPP
    --------------------------------  ----------------------------------------
    group message from a member       groupchat from room/<member nick>
    group appears / first message     XEP-0249 direct invite from the room
    group rename                      room subject change
    member added / left               occupant presence + a status line
    owner types in the room           POST /im/send {chat_guid}
    owner changes the subject         POST /im/group/rename
    owner invites a contact           POST /im/group/members {add}
    owner kicks / sets affiliation    POST /im/group/members {remove}
    owner destroys the room           POST /im/group/leave  (leaves the group)
    owner joins an unknown room name  a *draft* room; created on first message
    disco#items on the component      the groups, XEP-0059 paged
    join history                      the last N group messages, XEP-0203 delay

Everything stanza-shaped here is built as text and handed to `send_raw`, and
parsed from the stanza's ElementTree, so the whole layer runs in tests with a
fake that records strings.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape, quoteattr

import aiohttp

from .component import jid_to_handle, localpart_to_handle, peer_jid
from .config import Config
from .logsetup import get_logger
from .models import IMessageEvent, bare_jid, normalize_handle
from .outbound import FetchError, fetch_to_tempfile, guess_name
from .upstream import UpstreamClient, UpstreamError
from .xep import parse_oob_url, xml_safe

log = get_logger("relay.muc")

NS_MUC = "http://jabber.org/protocol/muc"
NS_USER = "http://jabber.org/protocol/muc#user"
NS_ADMIN = "http://jabber.org/protocol/muc#admin"
NS_OWNER = "http://jabber.org/protocol/muc#owner"
NS_ROOMINFO = "http://jabber.org/protocol/muc#roominfo"
NS_DISCO_INFO = "http://jabber.org/protocol/disco#info"
NS_DISCO_ITEMS = "http://jabber.org/protocol/disco#items"
NS_RSM = "http://jabber.org/protocol/rsm"
NS_DELAY = "urn:xmpp:delay"
NS_CONFERENCE = "jabber:x:conference"
NS_DATA = "jabber:x:data"
NS_STANZAS = "urn:ietf:params:xml:ns:xmpp-stanzas"

_UUID_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$"
)
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX_RE = re.compile(r"^(?:[0-9a-f]{2})+$")
_MAX_LOCALPART = 900
HISTORY_SIZE = 100
REINVITE_SECS = 600.0   # re-send the invite at most this often while the owner is not in the room
CONFIG_FORM_ROOMNAME = "muc#roomconfig_roomname"


def groups_enabled(env: Optional[dict] = None) -> bool:
    """`XMPP_GROUPS_MUC` (default on): set 0 to keep the legacy `[group] name:` chat lines."""
    value = (os.environ if env is None else env).get("XMPP_GROUPS_MUC", "1")
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


# -- room JID <-> group id -----------------------------------------------------


def room_localpart(chat_guid: str) -> Optional[str]:
    """The room localpart for a group id; lossless and never a peer localpart."""
    guid = chat_guid or ""
    if not guid:
        return None
    if _UUID_RE.fullmatch(guid):  # not .match: `$` would also accept a trailing newline
        return "grp-u" + guid.replace("-", "").lower()
    try:
        encoded = "grp-x" + guid.encode("utf-8").hex()
    except UnicodeEncodeError:  # a lone surrogate (JSON "\ud800") has no UTF-8 form
        return None
    return encoded if len(encoded) <= _MAX_LOCALPART else None


def localpart_to_chat_guid(localpart: str) -> Optional[str]:
    """Reverse of room_localpart; None for anything that is not a room localpart."""
    value = (localpart or "").strip().lower()
    if value.startswith("grp-u") and _HEX32_RE.fullmatch(value[5:]):
        h = value[5:].upper()
        return "-".join((h[:8], h[8:12], h[12:16], h[16:20], h[20:]))
    if value.startswith("grp-x") and _HEX_RE.fullmatch(value[5:]):
        try:
            guid = bytes.fromhex(value[5:]).decode("utf-8")
        except UnicodeDecodeError:
            return None
        # One spelling per room: a UUID spelled grp-x<hex> (or a guid too long to mint) is not ours.
        return guid if room_localpart(guid) == value else None
    return None


def room_jid(chat_guid: str, domain: str) -> Optional[str]:
    local = room_localpart(chat_guid)
    if not local or not (domain or "").strip():
        return None
    return local + "@" + domain.strip().lower()


def jid_to_chat_guid(jid: str, domain: str) -> Optional[str]:
    bare = (jid or "").split("/", 1)[0].strip().lower()
    local, sep, dom = bare.rpartition("@")
    if not sep or dom != (domain or "").strip().lower():
        return None
    return localpart_to_chat_guid(local)


def display_name(name: Optional[str], participants: list[str], mine: set[str]) -> str:
    """The group's name, or a stand-in made of the first few other members."""
    if name and name.strip():
        return name.strip()
    others = [p for p in participants if p not in mine]
    if not others:
        return "iMessage group"
    label = ", ".join(others[:3])
    return label + (" +" + str(len(others) - 3) if len(others) > 3 else "")


_MIN_STAMP = 0.0              # the epoch: earlier stamps mean nothing for chat history
_MAX_STAMP = 253402300799.0   # 9999-12-31T23:59:59Z


def format_stamp(ts: float) -> str:
    """XEP-0082 UTC datetime for a XEP-0203 delay stamp."""
    # A timestamp outside 1970..9999 (or NaN) would raise and abort every later history replay.
    if not (_MIN_STAMP <= ts <= _MAX_STAMP):
        ts = _MIN_STAMP if ts < 0 else _MAX_STAMP if ts == ts else 0.0
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: str) -> Optional[float]:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


# -- state ----------------------------------------------------------------------


@dataclass
class HistoryItem:
    handle: str            # sender handle; "" = the owner (sent from XMPP)
    text: str
    stamp: float
    msg_id: str = ""


@dataclass
class Room:
    """One room, real (an iMessage group) or a draft (no iMessage group yet)."""

    key: str                                   # chat_guid, or draft localpart
    name: Optional[str] = None
    participants: list[str] = field(default_factory=list)   # bare handles
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))
    sessions: dict[str, str] = field(default_factory=dict)  # owner full JID -> nick
    invited_at: float = 0.0                    # when the owner was last sent a direct invite
    refreshed: bool = False                    # name/members were pulled from imessage-server
    draft: bool = False


def select_history(items: list[HistoryItem], request: Optional[dict], now: float) -> list[HistoryItem]:
    """Apply a XEP-0045 `<history/>` request (maxstanzas, seconds, since, maxchars)."""
    selected = list(items)
    if request is None:
        return selected
    since = request.get("since")
    if since is not None:
        selected = [i for i in selected if i.stamp > since]
    seconds = request.get("seconds")
    if seconds is not None:
        selected = [i for i in selected if i.stamp >= now - seconds]
    maxchars = request.get("maxchars")
    if maxchars is not None:
        kept: list[HistoryItem] = []
        total = 0
        for item in reversed(selected):
            total += len(item.text)
            if total > maxchars:
                break
            kept.append(item)
        selected = list(reversed(kept))
    maxstanzas = request.get("maxstanzas")
    if maxstanzas is not None:
        selected = selected[-maxstanzas:] if maxstanzas > 0 else []
    return selected


def parse_history_request(x: Optional[ET.Element]) -> Optional[dict]:
    if x is None:
        return None
    hist = _child(x, "history")
    if hist is None:
        return None
    out: dict[str, Any] = {}
    for key in ("maxstanzas", "maxchars", "seconds"):
        raw = hist.get(key)
        if raw is not None:
            try:
                out[key] = max(0, int(raw))
            except ValueError:
                pass
    since = hist.get("since")
    if since is not None:
        stamp = parse_stamp(since)
        if stamp is not None:
            out["since"] = stamp
    return out


def page(items: list[str], max_: Optional[int], after: Optional[str]) -> tuple[list[str], int]:
    """XEP-0059 forward paging over an already sorted list. Returns (page, total)."""
    total = len(items)
    start = 0
    if after is not None:
        start = next((i + 1 for i, v in enumerate(items) if v == after), total)
    chunk = items[start:]
    if max_ is not None:
        chunk = chunk[: max(0, max_)]
    return chunk, total


# -- XML helpers ----------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _child(el: Optional[ET.Element], local: str, ns: Optional[str] = None) -> Optional[ET.Element]:
    if el is None:
        return None
    for ch in el:
        if _local(ch.tag) == local and (ns is None or _ns(ch.tag) == ns):
            return ch
    return None


def _children(el: Optional[ET.Element], local: str, ns: Optional[str] = None) -> list[ET.Element]:
    if el is None:
        return []
    return [ch for ch in el if _local(ch.tag) == local and (ns is None or _ns(ch.tag) == ns)]


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "") if el is not None else ""


def _a(value: Any) -> str:
    return quoteattr(str(value))


def _error_xml(condition: str, etype: str = "cancel", text: Optional[str] = None) -> str:
    body = "<" + condition + " xmlns=" + _a(NS_STANZAS) + "/>"
    if text:
        body += "<text xmlns=" + _a(NS_STANZAS) + ">" + escape(text) + "</text>"
    return "<error type=" + _a(etype) + ">" + body + "</error>"


class UpstreamGroups:
    """The `/im/group*` calls, over the same HTTP session as UpstreamClient."""

    def __init__(self, upstream: UpstreamClient) -> None:
        self.upstream = upstream

    async def _call(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        up = self.upstream
        try:
            async with up.session.request(
                method,
                up.base_url + path,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=up.timeout),
            ) as resp:
                body = await resp.text()
                if resp.status // 100 != 2:
                    detail = body[:300]
                    try:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict) and parsed.get("error"):
                            detail = str(parsed["error"])
                    except ValueError:
                        pass
                    raise UpstreamError(method + " " + path + " -> " + str(resp.status) + ": " + detail)
                try:
                    return json.loads(body) if body else {}
                except ValueError:
                    return {"ok": True}
        except aiohttp.ClientError as exc:
            raise UpstreamError(method + " " + path + " failed: " + str(exc)) from exc

    async def groups(self) -> list[dict]:
        return list((await self._call("GET", "/im/groups")).get("groups") or [])

    async def create(self, participants: list[str], name: Optional[str], text: str) -> dict:
        return await self._call(
            "POST", "/im/group", {"participants": participants, "name": name, "text": text}
        )

    async def rename(self, chat_guid: str, name: str) -> dict:
        return await self._call("POST", "/im/group/rename", {"chat_guid": chat_guid, "name": name})

    async def members(self, chat_guid: str, add: list[str], remove: list[str]) -> dict:
        return await self._call(
            "POST", "/im/group/members", {"chat_guid": chat_guid, "add": add, "remove": remove}
        )

    async def leave(self, chat_guid: str) -> dict:
        return await self._call("POST", "/im/group/leave", {"chat_guid": chat_guid})

    async def send(self, chat_guid: str, text: str, reply_to: Optional[str] = None) -> dict:
        return await self._call(
            "POST", "/im/send", {"chat_guid": chat_guid, "text": text, "reply_to": reply_to}
        )


class GroupService:
    """The MUC service for iMessage groups. One instance per component."""

    def __init__(
        self,
        cfg: Config,
        xmpp: Any,
        api: UpstreamGroups,
        *,
        status: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.xmpp = xmpp
        self.api = api
        self._status = status
        self.clock = clock
        self.domain = cfg.component_domain.strip().lower()
        self.owner = bare_jid(cfg.component_owner)
        self.rooms: dict[str, Room] = {}      # chat_guid -> real room
        self.drafts: dict[str, Room] = {}     # localpart -> draft room
        self.mine: set[str] = set()           # our own handles (bare)
        self._tasks: set = set()

    # -- wiring ---------------------------------------------------------------

    def attach(self) -> None:
        """Register on the slixmpp component. Safe to skip in tests."""
        from slixmpp.xmlstream.handler import Callback
        from slixmpp.xmlstream.matcher import MatchXPath

        self.xmpp.add_event_handler("message", self._on_message)
        self.xmpp.add_event_handler("presence", self._on_presence)
        self.xmpp.register_handler(
            Callback(
                "imessage-groups-iq",
                MatchXPath("{%s}iq" % self.xmpp.default_ns),
                self._on_iq,
            )
        )

    async def _on_message(self, msg) -> None:
        await self._guard(self.handle_message, msg.xml, "message")

    async def _on_presence(self, pres) -> None:
        await self._guard(self.handle_presence, pres.xml, "presence")

    def _on_iq(self, iq) -> None:
        # slixmpp calls Callback handlers synchronously and does not await them.
        task = asyncio.ensure_future(self._guard(self.handle_iq, iq.xml, "iq"))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard(self, fn, el, what: str) -> None:
        try:
            await fn(el)
        except Exception:  # noqa: BLE001 - one bad stanza must not kill the stream
            log.exception("group stanza handler failed", event="muc_error", stanza=what)

    async def _send(self, xml: str) -> None:
        # Message text, names and handles come from iMessage; one XML-illegal character
        # (NUL, ESC, a lone surrogate...) would make the stanza ill-formed and the server
        # would drop the component stream. Every structural character is legal, so filter all.
        result = self.xmpp.send_raw(xml_safe(xml))
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result

    # -- state ----------------------------------------------------------------

    async def hydrate(self) -> None:
        """Load our handles and the saved groups from imessage-server (best effort)."""
        if self._status is not None:
            try:
                status = await self._status()
                for handle in status.get("handles") or []:
                    self.mine.add(normalize_handle(str(handle).split(":", 1)[-1]))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read our handles", event="muc_status_error", error=str(exc))
        try:
            groups = await self.api.groups()
        except UpstreamError as exc:
            log.warning("could not list groups", event="muc_hydrate_error", error=str(exc))
            return
        for info in groups:
            room = self._room(str(info.get("chat_guid") or ""))
            if room is None:
                continue
            room.name = info.get("name") or room.name
            room.participants = [normalize_handle(p) for p in info.get("participants") or []]
        log.info("hydrated groups", event="muc_hydrated", count=len(self.rooms))

    def _room(self, chat_guid: str, create: bool = True) -> Optional[Room]:
        if not chat_guid:
            return None
        room = self.rooms.get(chat_guid)
        if room is None and create:
            if room_localpart(chat_guid) is None:
                return None
            room = self.rooms[chat_guid] = Room(key=chat_guid)
        return room

    def occupants(self, room: Room) -> list[str]:
        return [p for p in room.participants if p not in self.mine]

    def label(self, room: Room) -> str:
        return display_name(room.name, room.participants, self.mine)

    def room_address(self, room: Room) -> str:
        if room.draft:
            return room.key + "@" + self.domain
        return room_jid(room.key, self.domain) or ""

    def _nick_of(self, room: Room, handle: str) -> str:
        """The occupant nick a sender shows under (the owner's own nick if it is us)."""
        if not handle or handle in self.mine:
            return next(iter(room.sessions.values()), None) or "me"
        return handle

    def _status_stanza(self, room: Room, to: str, text: str) -> str:
        return (
            "<message type='groupchat' from=" + _a(self.room_address(room)) + " to=" + _a(to)
            + "><body>" + escape(text) + "</body></message>"
        )

    # -- iMessage -> XMPP -----------------------------------------------------

    async def on_message(self, message: IMessageEvent) -> bool:
        """An inbound iMessage in a group. True when it was routed to the room."""
        room = self._room(message.chat_guid)
        if room is None:
            return False
        members = [normalize_handle(h) for h in ([message.sender] + list(message.to)) if h]
        if not room.refreshed:
            await self._refresh(room)
        if members:
            await self._set_members(room, sorted(set(members) | set(room.participants)))
        stamp = (message.timestamp / 1000.0) if message.timestamp else self.clock()
        sender = normalize_handle(message.sender) if message.sender else ""
        room.history.append(HistoryItem(sender, message.text, stamp, message.guid))
        if room.sessions:
            for full, _ in list(room.sessions.items()):
                await self._send(self._group_message(room, sender, message.text, full, message.guid))
        elif self.clock() - room.invited_at > REINVITE_SECS:
            await self._invite(room)
        return True

    async def on_attachment(self, event: dict, url: str, name: str) -> bool:
        """An inbound group attachment as an OOB groupchat message from the sender's occupant.

        The room is plaintext (no OMEMO in rooms), so the link is as visible as any
        room text. History keeps the URL as the body.
        """
        room = self._room(str(event.get("chat_guid") or ""))
        if room is None:
            return False
        sender = normalize_handle(str(event.get("from") or "")) if event.get("from") else ""
        if sender and sender not in room.participants:
            await self._set_members(room, sorted(set(room.participants) | {sender}))
        guid = str(event.get("guid") or "")
        room.history.append(HistoryItem(sender, url, self.clock(), guid))
        if room.sessions:
            for full in list(room.sessions):
                await self._send(self._group_message(room, sender, url, full, guid, oob=(url, name)))
        elif self.clock() - room.invited_at > REINVITE_SECS:
            await self._invite(room)
        return True

    def _group_message(self, room: Room, handle: str, text: str, to: str, msg_id: str = "",
                       stamp: Optional[float] = None, oob: Optional[tuple[str, str]] = None) -> str:
        frm = self.room_address(room) + "/" + self._nick_of(room, handle)
        out = "<message type='groupchat' from=" + _a(frm) + " to=" + _a(to)
        if msg_id:
            out += " id=" + _a(msg_id)
        out += "><body>" + escape(text) + "</body>"
        if oob is not None:
            out += ("<x xmlns='jabber:x:oob'><url>" + escape(oob[0]) + "</url>"
                    + ("<desc>" + escape(oob[1]) + "</desc>" if oob[1] else "") + "</x>")
        if stamp is not None:
            out += ("<delay xmlns=" + _a(NS_DELAY) + " from=" + _a(self.room_address(room))
                    + " stamp=" + _a(format_stamp(stamp)) + "/>")
        return out + "</message>"

    async def _invite(self, room: Room) -> None:
        """XEP-0249 direct invitation so the group shows up in the owner's client."""
        room.invited_at = self.clock()
        address = self.room_address(room)
        reason = self.label(room)
        await self._send(
            "<message from=" + _a(address) + " to=" + _a(self.owner) + ">"
            "<x xmlns=" + _a(NS_CONFERENCE) + " jid=" + _a(address) + " reason=" + _a(reason) + "/>"
            "<body>" + escape("iMessage group \"" + reason + "\": join " + address) + "</body>"
            "</message>"
        )
        log.info("invited the owner to a group room", event="muc_invited", room=address)

    async def _refresh(self, room: Room) -> None:
        """Pull this group's name and members from imessage-server (best effort)."""
        room.refreshed = True
        try:
            for info in await self.api.groups():
                if info.get("chat_guid") == room.key:
                    room.name = info.get("name") or room.name
                    room.participants = [normalize_handle(p) for p in info.get("participants") or []]
        except UpstreamError:
            pass

    async def on_group_event(self, event: dict) -> None:
        """A `group` SSE event: rename, member change or icon change."""
        room = self._room(str(event.get("chat_guid") or ""))
        if room is None:
            return
        kind = str(event.get("kind") or "")
        who = normalize_handle(str(event.get("from") or "")) if event.get("from") else ""
        if kind == "rename":
            room.name = event.get("name") or room.name
            await self._broadcast_subject(room, who)
        elif kind == "participants":
            members = [normalize_handle(p) for p in event.get("participants") or []]
            added = [normalize_handle(p) for p in event.get("added") or []]
            removed = [normalize_handle(p) for p in event.get("removed") or []]
            await self._set_members(room, members)
            for line in self._change_lines(who, added, removed):
                await self._broadcast(room, self._status_stanza, line)
        elif kind == "icon":
            await self._broadcast(
                room, self._status_stanza,
                (who or "someone") + " changed the group icon (icons are not mirrored)",
            )
        else:
            log.debug("ignoring group event", event="muc_event_ignored", kind=kind)

    def _change_lines(self, who: str, added: list[str], removed: list[str]) -> list[str]:
        actor = who or "someone"
        lines = []
        if added:
            lines.append(actor + " added " + ", ".join(added))
        if removed:
            lines.append(actor + (" left" if removed == [who] else " removed " + ", ".join(removed)))
        return lines

    async def _broadcast(self, room: Room, build: Callable[..., str], *args) -> None:
        for full in list(room.sessions):
            await self._send(build(room, full, *args))

    async def _broadcast_subject(self, room: Room, who: str) -> None:
        for full in list(room.sessions):
            await self._send(self._subject_stanza(room, full, who))

    def _subject_stanza(self, room: Room, to: str, who: str = "") -> str:
        frm = self.room_address(room) + ("/" + self._nick_of(room, who) if who else "")
        return ("<message type='groupchat' from=" + _a(frm) + " to=" + _a(to) + "><subject>"
                + escape(self.label(room)) + "</subject></message>")

    async def _set_members(self, room: Room, members: list[str]) -> None:
        """Replace the member list, announcing joins and leaves to joined sessions."""
        old = set(self.occupants(room))
        room.participants = sorted(set(members))
        new = set(self.occupants(room))
        for full in list(room.sessions):
            for handle in sorted(new - old):
                await self._send(self._occupant_presence(room, handle, full))
            for handle in sorted(old - new):
                await self._send(self._occupant_presence(room, handle, full, gone=True))

    # -- presence -------------------------------------------------------------

    def _occupant_presence(self, room: Room, handle: str, to: str, gone: bool = False) -> str:
        jid = peer_jid(handle, self.domain)
        item = "<item affiliation=" + _a("none" if gone else "member") + " role=" + _a("none" if gone else "participant")
        if jid:
            item += " jid=" + _a(jid)
        item += "/>"
        out = "<presence from=" + _a(self.room_address(room) + "/" + handle) + " to=" + _a(to)
        if gone:
            out += " type='unavailable'"
        return out + "><x xmlns=" + _a(NS_USER) + ">" + item + "</x></presence>"

    def _self_presence(self, room: Room, full: str, nick: str, extra: str = "") -> str:
        return (
            "<presence from=" + _a(self.room_address(room) + "/" + nick) + " to=" + _a(full) + ">"
            "<x xmlns=" + _a(NS_USER) + "><item affiliation='owner' role='moderator' jid=" + _a(full) + "/>"
            "<status code='100'/>" + extra + "<status code='110'/></x></presence>"
        )

    def _presence_error(self, el: ET.Element, condition: str, text: Optional[str] = None) -> str:
        return (
            "<presence type='error' from=" + _a(el.get("to", "")) + " to=" + _a(el.get("from", ""))
            + ">" + _error_xml(condition, "cancel", text) + "</presence>"
        )

    async def handle_presence(self, el: ET.Element) -> None:
        to = el.get("to", "")
        frm = el.get("from", "")
        local, _, rest = to.partition("@")
        domain, _, nick = rest.partition("/")
        if domain.lower() != self.domain or not local:
            return
        ptype = el.get("type") or "available"
        if ptype not in ("available", "unavailable"):
            return
        x = _child(el, "x", NS_MUC)
        if bare_jid(frm) != self.owner:
            if x is not None or ptype == "unavailable":
                log.warning("DENYING room presence from a non-owner JID", event="muc_denied", from_jid=frm)
            return
        chat_guid = localpart_to_chat_guid(local)
        if chat_guid is None and localpart_to_handle(local) is not None:
            return  # a peer JID, handled by the component transport
        if ptype == "unavailable":
            await self._leave_session(local.lower(), chat_guid, frm)
            return
        if x is None:
            return  # plain presence to a room outside a join: ignore
        if not nick:
            await self._send(self._presence_error(el, "jid-malformed", "a room nickname is required"))
            return
        room = self.rooms.get(chat_guid) if chat_guid else self.drafts.get(local.lower())
        created = False
        if room is None:
            if chat_guid is not None:
                # Opportunistically learn a group we have not heard from yet.
                await self.hydrate()
                room = self.rooms.get(chat_guid)
                if room is None:
                    await self._send(self._presence_error(el, "item-not-found", "no such iMessage group"))
                    return
            else:
                room = self.drafts[local.lower()] = Room(key=local.lower(), name=local, draft=True)
                created = True
        others = {n for f, n in room.sessions.items() if f != frm}
        # One owner, one nick: every session of the owner shares it, and it cannot be an occupant's.
        if nick in self.occupants(room) or (others and nick not in others):
            await self._send(self._presence_error(el, "conflict", "nickname already in use"))
            return
        room.sessions[frm] = nick
        room.invited_at = self.clock()
        await self._send_join(room, frm, nick, parse_history_request(x), created)

    async def _send_join(self, room: Room, full: str, nick: str, history: Optional[dict], created: bool) -> None:
        for handle in self.occupants(room):
            await self._send(self._occupant_presence(room, handle, full))
        await self._send(self._self_presence(room, full, nick, "<status code='201'/>" if created else ""))
        for item in select_history(list(room.history), history, self.clock()):
            await self._send(self._group_message(room, item.handle, item.text, full, item.msg_id, item.stamp))
        await self._send(self._subject_stanza(room, full))
        log.info("owner joined a group room", event="muc_joined", room=self.room_address(room),
                 draft=room.draft, history=len(room.history))

    async def _leave_session(self, local: str, chat_guid: Optional[str], full: str) -> None:
        room = self.rooms.get(chat_guid) if chat_guid else self.drafts.get(local)
        if room is None or full not in room.sessions:
            return
        nick = room.sessions.pop(full)
        await self._send(
            "<presence type='unavailable' from=" + _a(self.room_address(room) + "/" + nick)
            + " to=" + _a(full) + "><x xmlns=" + _a(NS_USER)
            + "><item affiliation='owner' role='none'/><status code='110'/></x></presence>"
        )
        if room.draft and not room.sessions:
            self.drafts.pop(room.key, None)

    # -- messages -------------------------------------------------------------

    async def handle_message(self, el: ET.Element) -> None:
        mtype = el.get("type") or "normal"
        to = el.get("to", "")
        frm = el.get("from", "")
        local, _, rest = to.partition("@")
        domain, _, resource = rest.partition("/")
        if domain.lower() != self.domain or not local:
            return
        chat_guid = localpart_to_chat_guid(local)
        room = self.rooms.get(chat_guid) if chat_guid else self.drafts.get(local.lower())
        if room is None:
            return
        if bare_jid(frm) != self.owner:
            log.warning("DENYING room message from a non-owner JID", event="muc_denied", from_jid=frm)
            return
        if mtype == "chat" or resource:
            await self._send(self._message_error(el, "feature-not-implemented", "private messages to occupants are not supported"))
            return
        # Mediated invitations: <x muc#user><invite to='contact'/></x>
        x = _child(el, "x", NS_USER)
        invites = _children(x, "invite")
        if invites:
            handles = []
            for invite in invites:
                handle = jid_to_handle(invite.get("to", ""), self.domain)
                if handle is None:
                    await self._send(self._message_error(el, "item-not-found", "can only invite contacts of this gateway"))
                    return
                handles.append(handle)
            error = await self._add_members(room, handles)
            if error:
                await self._send(self._message_error(el, "recipient-unavailable", error))
            return
        if mtype != "groupchat":
            return
        if frm not in room.sessions:
            await self._send(self._message_error(el, "not-acceptable", "join the room first"))
            return
        subject = _child(el, "subject")
        body = _child(el, "body")
        if subject is not None and body is None:
            await self._set_subject(el, room, _text(subject).strip())
        elif body is not None and _text(body).strip():
            url = parse_oob_url(el)
            if url:
                await self._owner_sends_file(el, room, url, _text(body))
            else:
                await self._owner_says(el, room, _text(body))

    async def _owner_sends_file(self, el: ET.Element, room: Room, url: str, text: str) -> None:
        """XEP-0066 in a room: fetch the (SSRF-guarded) URL and send it to the group as a file."""
        if room.draft:
            await self._send(self._message_error(el, "not-acceptable", "send a text message first to create the group"))
            return
        try:
            path, ctype, size = await fetch_to_tempfile(
                url, max_bytes=self.cfg.outbound_max_bytes, timeout=self.cfg.outbound_fetch_timeout
            )
        except FetchError as exc:
            await self._send(self._message_error(el, "not-acceptable", "Not sent: " + str(exc)))
            return
        name = guess_name(url)
        try:
            await self.api.upstream.send_attachment("", path, name, ctype, chat_guid=room.key)
        except UpstreamError as exc:
            log.error("group attachment send failed", event="muc_attachment_error", room=room.key, error=str(exc))
            await self._send(self._message_error(el, "recipient-unavailable", "iMessage attachment failed: " + str(exc)))
            return
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        msg_id = el.get("id") or ""
        room.history.append(HistoryItem("", url, self.clock(), msg_id))
        for full in list(room.sessions):
            await self._send(self._group_message(room, "", url, full, msg_id, oob=(url, name)))
        log.info("sent a room attachment to the iMessage group", event="muc_attachment_sent", room=room.key, size=size)
        caption = text.strip()
        if caption and caption != url.strip():
            await self._owner_says(el, room, caption)

    def _message_error(self, el: ET.Element, condition: str, text: Optional[str] = None) -> str:
        return (
            "<message type='error' from=" + _a(el.get("to", "")) + " to=" + _a(el.get("from", ""))
            + (" id=" + _a(el.get("id")) if el.get("id") else "") + ">"
            + _error_xml(condition, "cancel", text) + "</message>"
        )

    async def _owner_says(self, el: ET.Element, room: Room, text: str) -> None:
        msg_id = el.get("id") or ""
        if room.draft:
            invited = self.occupants(room)
            if len(invited) < 2:
                await self._send(self._message_error(
                    el, "not-acceptable",
                    "invite at least two contacts before the first message: an iMessage group needs three people",
                ))
                return
            await self._materialise(el, room, text, msg_id)
            return
        try:
            await self.api.send(room.key, text)
        except UpstreamError as exc:
            log.error("group send failed", event="muc_send_error", room=room.key, error=str(exc))
            await self._send(self._message_error(el, "recipient-unavailable", "iMessage send failed: " + str(exc)))
            return
        room.history.append(HistoryItem("", text, self.clock(), msg_id))
        # Reflect to every joined session, as a MUC must (clients use it as the delivery ack).
        for full in list(room.sessions):
            await self._send(self._group_message(room, "", text, full, msg_id))
        log.info("sent a room message to the iMessage group", event="muc_sent", room=room.key)

    async def _materialise(self, el: ET.Element, draft: Room, text: str, msg_id: str) -> None:
        """First message of a draft room: create the iMessage group and move the owner to it."""
        try:
            result = await self.api.create(self.occupants(draft), draft.name, text)
        except UpstreamError as exc:
            log.error("group create failed", event="muc_create_error", error=str(exc))
            await self._send(self._message_error(el, "recipient-unavailable", "could not create the group: " + str(exc)))
            return
        chat_guid = str(result.get("chat_guid") or "")
        room = self._room(chat_guid)
        if room is None:
            await self._send(self._message_error(el, "internal-server-error", "imessage-server returned no group id"))
            return
        info = result.get("group") or {}
        room.name = info.get("name") or draft.name
        room.participants = [normalize_handle(p) for p in info.get("participants") or []] or sorted(
            set(draft.participants) | self.mine)
        room.history.append(HistoryItem("", text, self.clock(), msg_id))
        room.invited_at = self.clock()
        room.refreshed = True
        new_address = self.room_address(room)
        # XEP-0045 10.9: the draft is destroyed with an alternate venue, then XEP-0249 invites.
        for full, nick in list(draft.sessions.items()):
            await self._send(
                "<presence type='unavailable' from=" + _a(self.room_address(draft) + "/" + nick)
                + " to=" + _a(full) + "><x xmlns=" + _a(NS_USER)
                + "><item affiliation='none' role='none'/><destroy jid=" + _a(new_address)
                + "><reason>iMessage group created</reason></destroy></x></presence>"
            )
        self.drafts.pop(draft.key, None)
        await self._send(
            "<message from=" + _a(new_address) + " to=" + _a(self.owner) + ">"
            "<x xmlns=" + _a(NS_CONFERENCE) + " jid=" + _a(new_address) + " reason=" + _a(self.label(room)) + "/>"
            "<body>" + escape("iMessage group \"" + self.label(room) + "\" created: join " + new_address) + "</body></message>"
        )
        log.info("created an iMessage group from a draft room", event="muc_created", room=new_address,
                 members=len(room.participants))

    async def _set_subject(self, el: ET.Element, room: Room, name: str) -> None:
        if not name:
            await self._send(self._message_error(el, "bad-request", "an empty subject cannot rename a group"))
            return
        if not room.draft:
            try:
                await self.api.rename(room.key, name)
            except UpstreamError as exc:
                await self._send(self._message_error(el, "recipient-unavailable", "rename failed: " + str(exc)))
                return
        room.name = name
        await self._broadcast_subject(room, "")

    async def _add_members(self, room: Room, handles: list[str]) -> Optional[str]:
        """Returns an error text, or None on success."""
        handles = [h for h in handles if h not in room.participants]
        if not handles:
            return None
        if room.draft:
            await self._set_members(room, sorted(set(room.participants) | set(handles)))
            return None
        try:
            result = await self.api.members(room.key, handles, [])
        except UpstreamError as exc:
            return "could not add members: " + str(exc)
        await self._apply_group_result(room, result, "you added " + ", ".join(handles))
        return None

    async def _remove_members(self, room: Room, handles: list[str]) -> Optional[str]:
        """Returns an error text, or None on success."""
        if room.draft:
            await self._set_members(room, [p for p in room.participants if p not in handles])
            return None
        try:
            result = await self.api.members(room.key, [], handles)
        except UpstreamError as exc:
            return str(exc)
        await self._apply_group_result(room, result, "you removed " + ", ".join(handles))
        return None

    async def _apply_group_result(self, room: Room, result: dict, line: str) -> None:
        info = result.get("group") or {}
        if info.get("participants"):
            await self._set_members(room, [normalize_handle(p) for p in info["participants"]])
        await self._broadcast(room, self._status_stanza, line)

    # -- IQ -------------------------------------------------------------------

    def _iq(self, el: ET.Element, payload: str = "", etype: str = "result") -> str:
        return (
            "<iq type=" + _a(etype) + " id=" + _a(el.get("id", "")) + " from=" + _a(el.get("to", ""))
            + " to=" + _a(el.get("from", "")) + ">" + payload + "</iq>"
        )

    def _iq_error(self, el: ET.Element, condition: str, text: Optional[str] = None, etype: str = "cancel") -> str:
        return self._iq(el, _error_xml(condition, etype, text), "error")

    async def handle_iq(self, el: ET.Element) -> None:
        if el.get("type") not in ("get", "set"):
            return
        frm = el.get("from", "")
        to = el.get("to", "")
        local, _, rest = to.partition("@") if "@" in to else ("", "", to)
        domain, _, resource = rest.partition("/")
        if domain.lower() != self.domain:
            return
        if bare_jid(frm) != self.owner:
            log.warning("DENYING iq from a non-owner JID", event="muc_denied", from_jid=frm)
            return
        payload = next((ch for ch in el if _ns(ch.tag)), None)
        ns = _ns(payload.tag) if payload is not None else ""
        if not local:
            await self._service_iq(el, payload, ns)
            return
        chat_guid = localpart_to_chat_guid(local)
        room = self.rooms.get(chat_guid) if chat_guid else self.drafts.get(local.lower())
        if room is None or resource:
            await self._send(self._iq_error(el, "item-not-found" if room is None else "service-unavailable"))
            return
        if ns == NS_DISCO_INFO:
            await self._send(self._iq(el, self._room_info(room)))
        elif ns == NS_DISCO_ITEMS:
            await self._send(self._iq(el, self._room_items(room)))
        elif ns == NS_ADMIN:
            await self._admin_iq(el, room, payload)
        elif ns == NS_OWNER:
            await self._owner_iq(el, room, payload)
        else:
            await self._send(self._iq_error(el, "service-unavailable"))

    async def _service_iq(self, el: ET.Element, payload: Optional[ET.Element], ns: str) -> None:
        if ns == NS_DISCO_INFO:
            await self._send(self._iq(el, (
                "<query xmlns=" + _a(NS_DISCO_INFO) + ">"
                "<identity category='conference' type='text' name='iMessage groups'/>"
                "<identity category='gateway' type='imessage' name='iMessage'/>"
                "<feature var=" + _a(NS_DISCO_INFO) + "/><feature var=" + _a(NS_DISCO_ITEMS) + "/>"
                "<feature var=" + _a(NS_MUC) + "/><feature var=" + _a(NS_RSM) + "/>"
                "<feature var=" + _a(NS_CONFERENCE) + "/><feature var=" + _a(NS_DELAY) + "/>"
                "</query>")))
        elif ns == NS_DISCO_ITEMS:
            await self._send(self._iq(el, self._service_items(payload)))
        else:
            await self._send(self._iq_error(el, "service-unavailable"))

    def _service_items(self, query: Optional[ET.Element]) -> str:
        rooms = {self.room_address(r): r for r in [*self.rooms.values(), *self.drafts.values()]
                 if r.draft or len(r.participants) > 2}
        jids = sorted(rooms)
        rsm = _child(query, "set", NS_RSM)
        max_ = after = None
        if rsm is not None:
            try:
                max_ = int(_text(_child(rsm, "max")))
            except ValueError:
                max_ = None
            after = _text(_child(rsm, "after")) or None
        chunk, total = page(jids, max_, after)
        out = "<query xmlns=" + _a(NS_DISCO_ITEMS) + ">"
        for jid in chunk:
            out += "<item jid=" + _a(jid) + " name=" + _a(self.label(rooms[jid])) + "/>"
        if rsm is not None:
            out += "<set xmlns=" + _a(NS_RSM) + ">"
            if chunk:
                out += ("<first index=" + _a(jids.index(chunk[0])) + ">" + escape(chunk[0]) + "</first>"
                        "<last>" + escape(chunk[-1]) + "</last>")
            out += "<count>" + str(total) + "</count></set>"
        return out + "</query>"

    def _room_info(self, room: Room) -> str:
        count = len(room.participants) or 1
        return (
            "<query xmlns=" + _a(NS_DISCO_INFO) + ">"
            "<identity category='conference' type='text' name=" + _a(self.label(room)) + "/>"
            "<feature var=" + _a(NS_MUC) + "/><feature var=" + _a(NS_DISCO_INFO) + "/>"
            "<feature var=" + _a(NS_DISCO_ITEMS) + "/><feature var='muc_persistent'/>"
            "<feature var='muc_membersonly'/><feature var='muc_nonanonymous'/>"
            "<feature var='muc_open'/><feature var='muc_unsecured'/><feature var='muc_unmoderated'/>"
            "<feature var='muc_public'/><feature var=" + _a(NS_DELAY) + "/>"
            "<x xmlns=" + _a(NS_DATA) + " type='result'>"
            "<field var='FORM_TYPE' type='hidden'><value>" + NS_ROOMINFO + "</value></field>"
            "<field var='muc#roominfo_description'><value>iMessage group</value></field>"
            "<field var='muc#roominfo_subject'><value>" + escape(self.label(room)) + "</value></field>"
            "<field var='muc#roominfo_occupants'><value>" + str(count) + "</value></field>"
            "</x></query>"
        )

    def _room_items(self, room: Room) -> str:
        out = "<query xmlns=" + _a(NS_DISCO_ITEMS) + ">"
        base = self.room_address(room)
        for handle in self.occupants(room):
            out += "<item jid=" + _a(base + "/" + handle) + " name=" + _a(handle) + "/>"
        for nick in room.sessions.values():
            out += "<item jid=" + _a(base + "/" + nick) + " name=" + _a(nick) + "/>"
        return out + "</query>"

    async def _admin_iq(self, el: ET.Element, room: Room, query: ET.Element) -> None:
        items = _children(query, "item")
        if el.get("type") == "get":
            wanted = items[0].get("affiliation") if items else None
            out = "<query xmlns=" + _a(NS_ADMIN) + ">"
            if wanted in (None, "member"):
                for handle in self.occupants(room):
                    jid = peer_jid(handle, self.domain)
                    out += ("<item affiliation='member' nick=" + _a(handle)
                            + (" jid=" + _a(jid) if jid else "") + "/>")
            if wanted in (None, "owner"):
                out += "<item affiliation='owner' jid=" + _a(self.owner) + "/>"
            await self._send(self._iq(el, out + "</query>"))
            return
        add: list[str] = []
        remove: list[str] = []
        for item in items:
            jid = item.get("jid")
            nick = item.get("nick")
            handle = jid_to_handle(jid, self.domain) if jid else (nick if nick in self.occupants(room) else None)
            if handle is None:
                await self._send(self._iq_error(el, "item-not-found", "unknown member", "cancel"))
                return
            affiliation = item.get("affiliation")
            role = item.get("role")
            if affiliation == "member":
                add.append(handle)
            elif affiliation in ("none", "outcast") or role == "none":
                remove.append(handle)
            else:
                await self._send(self._iq_error(el, "bad-request", "only member and none affiliations map to iMessage"))
                return
        error = await self._add_members(room, add) if add else None
        if error is None and remove:
            error = await self._remove_members(room, remove)
        if error is not None:
            await self._send(self._iq_error(el, "not-allowed", error))
            return
        await self._send(self._iq(el))

    async def _owner_iq(self, el: ET.Element, room: Room, query: ET.Element) -> None:
        if el.get("type") == "get":
            await self._send(self._iq(el, (
                "<query xmlns=" + _a(NS_OWNER) + "><x xmlns=" + _a(NS_DATA) + " type='form'>"
                "<title>iMessage group</title>"
                "<field var='FORM_TYPE' type='hidden'><value>http://jabber.org/protocol/muc#roomconfig</value></field>"
                "<field var=" + _a(CONFIG_FORM_ROOMNAME) + " type='text-single' label='Group name'><value>"
                + escape(room.name or "") + "</value></field></x></query>")))
            return
        destroy = _child(query, "destroy")
        if destroy is not None:
            await self._destroy(el, room, _text(_child(destroy, "reason")))
            return
        form = _child(query, "x", NS_DATA)
        if form is not None and form.get("type") == "submit":
            for fld in _children(form, "field"):
                if fld.get("var") == CONFIG_FORM_ROOMNAME:
                    name = _text(_child(fld, "value")).strip()
                    if name and name != (room.name or ""):
                        if not room.draft:
                            try:
                                await self.api.rename(room.key, name)
                            except UpstreamError as exc:
                                await self._send(self._iq_error(el, "not-allowed", "rename failed: " + str(exc)))
                                return
                        room.name = name
                        await self._broadcast_subject(room, "")
        await self._send(self._iq(el))

    async def _destroy(self, el: ET.Element, room: Room, reason: str) -> None:
        """Destroying the room as owner leaves the iMessage group (that is the only exit there is)."""
        if not room.draft:
            try:
                await self.api.leave(room.key)
            except UpstreamError as exc:
                await self._send(self._iq_error(el, "not-allowed", "could not leave the group: " + str(exc)))
                return
        for full, nick in list(room.sessions.items()):
            await self._send(
                "<presence type='unavailable' from=" + _a(self.room_address(room) + "/" + nick)
                + " to=" + _a(full) + "><x xmlns=" + _a(NS_USER)
                + "><item affiliation='none' role='none'/><destroy><reason>"
                + escape(reason or "left the iMessage group") + "</reason></destroy></x></presence>"
            )
        if room.draft:
            self.drafts.pop(room.key, None)
        else:
            self.rooms.pop(room.key, None)
        await self._send(self._iq(el))
        log.info("owner destroyed a group room", event="muc_destroyed", room=self.room_address(room),
                 left_group=not room.draft)


def maybe_make_group_service(cfg: Config, transport: Any, upstream: UpstreamClient) -> Optional[GroupService]:
    """A GroupService for a real component transport, else None (client mode, disabled, fakes)."""
    if cfg.mode != "component" or not groups_enabled():
        return None
    if cfg.omemo:
        # Rooms are plaintext XEP-0045 stanzas (no OMEMO for MUC here). With OMEMO on, group
        # traffic stays on the legacy per-peer 1:1 lines, which are encrypted: fail closed.
        log.warning(
            "XMPP_OMEMO is on: group chats are NOT exposed as MUC rooms (rooms would be plaintext); "
            "using encrypted 1:1 '[group] name' lines instead",
            event="muc_disabled_by_omemo",
        )
        return None
    xmpp = getattr(transport, "xmpp", None)
    if xmpp is None or not hasattr(xmpp, "register_handler"):
        return None
    service = GroupService(cfg, xmpp, UpstreamGroups(upstream), status=upstream.status)
    service.attach()
    return service
