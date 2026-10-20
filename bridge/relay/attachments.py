"""Inbound attachments: turn the server's `attachment` event into an XEP-0066 message.

The server has already downloaded the file and minted a short-lived signed path
(`/dl/<id>?exp=&sig=`). This module only joins it to PUBLIC_FILE_BASE_URL and
sends the link to the owner, from the peer's JID in component mode, with the
URL as the body and a `jabber:x:oob` element so clients render a file.

Files are untrusted third-party content; the link is the only thing that ever
crosses to XMPP (the bridge never reads the bytes).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import xep
from .component import peer_jid
from .config import Config
from .logsetup import get_logger
from .models import IMessageEvent, OutgoingXmpp

log = get_logger("relay.attachments")

# Exactly what the server mints; anything else is not forwarded.
_PATH_RE = re.compile(r"^/dl/[0-9a-f]{32}\?exp=[0-9]{1,12}&sig=[0-9a-f]{64}$")


def build_url(base: str, path: str) -> Optional[str]:
    """base + path, or None when the base is unset or the path is not a signed /dl path."""
    if not base or not _PATH_RE.match(path or ""):
        return None
    return base.rstrip("/") + path


def plan_attachment_delivery(cfg: Config, event: dict, url: str) -> list[OutgoingXmpp]:
    """The stanzas (body = url) for one attachment event.

    Component mode sends from the peer JID (the owner sees the file arrive from
    the person); a group or unencodable handle is sent from the component JID,
    since the placeholder text already named the sender and the group.
    Client mode reuses the normal routing, so contacts/MUC/prefix rules apply.
    """
    from .core import plan_delivery  # local: core imports this package's siblings

    sender = str(event.get("from") or "")
    chat_guid = str(event.get("chat_guid") or "")
    is_group = ";+;" in chat_guid
    if cfg.mode != "component":
        msg = IMessageEvent(
            guid=str(event.get("guid") or ""),
            chat_guid=chat_guid,
            sender=sender,
            to=(),
            text=url,
            is_group=is_group,
        )
        return plan_delivery(cfg, msg)
    frm = None if is_group else peer_jid(sender, cfg.component_domain)
    return [OutgoingXmpp(to=t, body=url, mtype="chat", frm=frm) for t in cfg.targets]


def oob_elements(url: str, name: str) -> list[Any]:
    return [xep.oob(url, name or None), xep.store_hint()]


class Attachments:
    def __init__(self, cfg: Config, transport: Any, stats: Any) -> None:
        self.cfg = cfg
        self.transport = transport
        self.stats = stats

    async def on_event(self, event: dict) -> None:
        url = build_url(self.cfg.public_file_base_url, str(event.get("path") or ""))
        if url is None:
            self.stats["attachment_unlinked"] += 1
            log.warning(
                "attachment event dropped: PUBLIC_FILE_BASE_URL unset or the path is not a signed /dl path",
                event="attachment_unlinked",
                guid=str(event.get("guid") or ""),
            )
            return
        name = str(event.get("name") or "")
        for stanza in plan_attachment_delivery(self.cfg, event, url):
            kwargs: dict = {}
            if self.cfg.mode == "component":
                # The client transport ignores payloads; the body URL still works.
                kwargs["elements"] = oob_elements(url, name)
            try:
                await self.transport.send_message(
                    to=stanza.to,
                    body=stanza.body,
                    mtype=stanza.mtype,
                    muc_nick=stanza.muc_nick,
                    frm=stanza.frm,
                    **kwargs,
                )
            except Exception:
                self.stats["delivery_errors"] += 1
                log.exception("failed to deliver attachment link", event="attachment_send_error", to=stanza.to)
                continue
            self.stats["attachments_delivered"] += 1
            log.info(
                "delivered attachment link",
                event="attachment_delivered",
                guid=str(event.get("guid") or ""),
                to=stanza.to,
                size=event.get("size"),
            )
