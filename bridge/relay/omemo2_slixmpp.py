"""The real (slixmpp-backed) PEP store for OMEMO 2.

Only the PEP plumbing lives here: fetching and publishing the
urn:xmpp:omemo:2:devices and urn:xmpp:omemo:2:bundles nodes.  The XML
serialisation is done by python-twomemo in relay.omemo2; this file only moves
pubsub items.  It has no offline test (it needs a live XMPP server) and is
deliberately small so it can be reviewed against XEP-0060.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from xml.etree import ElementTree as ET

from slixmpp.exceptions import IqError
from slixmpp.jid import JID  # pylint: disable=no-name-in-module
from slixmpp.plugins.xep_0004 import Form  # type: ignore[attr-defined]

from .logsetup import get_logger
from .omemo2 import BUNDLE_NODE, DEVICE_LIST_NODE, Omemo2Error

log = get_logger("relay.omemo2.pep")

_PUBSUB = "http://jabber.org/protocol/pubsub"


def _options_form(form_type: str, fields: Dict[str, str]) -> Form:
    form = Form()
    form["type"] = "submit"
    form.add_field(var="FORM_TYPE", ftype="hidden", value=form_type)
    for key, value in fields.items():
        form.add_field(var=key, value=value)
    return form


class SlixmppPepStore:
    """PepStore implemented on slixmpp's XEP-0060 pubsub plugin."""

    def __init__(self, xmpp: Any, own_bare_jid: str) -> None:
        self.xmpp = xmpp
        self.own_bare_jid = own_bare_jid.lower()
        self.xep_0060 = xmpp.plugin["xep_0060"]

    # -- helpers ----------------------------------------------------------

    async def _publish(self, node: str, item: ET.Element, item_id: str, max_items: str) -> None:
        options = {
            "pubsub#access_model": "open",
            "pubsub#persist_items": "true",
            "pubsub#max_items": max_items,
        }
        publish_options = _options_form(_PUBSUB + "#publish-options", options)
        node_config = _options_form(_PUBSUB + "#node_config", options)
        try:
            await self.xep_0060.publish(
                JID(self.own_bare_jid), node, item_id, item, publish_options
            )
            return
        except IqError as exc:
            precondition = exc.iq["error"].xml.find(
                "{" + _PUBSUB + "#errors}precondition-not-met"
            )
            if precondition is None:
                raise
        # The node exists with a different configuration: reconfigure and retry.
        await self.xep_0060.set_node_config(JID(self.own_bare_jid), node, node_config)
        await self.xep_0060.publish(JID(self.own_bare_jid), node, item_id, item, publish_options)

    async def _get_items(self, bare_jid: str, node: str, *, max_items: Optional[int] = None,
                         item_ids: Optional[list] = None) -> Optional[ET.Element]:
        kwargs: Dict[str, Any] = {}
        if max_items is not None:
            kwargs["max_items"] = max_items
        if item_ids is not None:
            kwargs["item_ids"] = item_ids
        try:
            iq = await self.xep_0060.get_items(JID(bare_jid), node, **kwargs)
        except IqError as exc:
            if exc.condition == "item-not-found":
                return None
            # Some servers reject max_items; retry without it.
            if max_items is not None:
                try:
                    iq = await self.xep_0060.get_items(JID(bare_jid), node)
                except IqError as retry:
                    if retry.condition == "item-not-found":
                        return None
                    raise Omemo2Error(
                        "PEP fetch of " + node + " for " + bare_jid + " failed: " + str(retry)
                    ) from retry
            else:
                raise Omemo2Error(
                    "PEP fetch of " + node + " for " + bare_jid + " failed: " + str(exc)
                ) from exc
        items = iq["pubsub"]["items"]
        if len(items) == 0:
            return None
        if len(items) > 1:
            raise Omemo2Error(
                "PEP node " + node + " for " + bare_jid + " returned " + str(len(items))
                + " items, expected one"
            )
        payload = next(iter(items["item"].xml), None)
        if payload is None:
            raise Omemo2Error("PEP item on " + node + " for " + bare_jid + " is empty")
        return payload

    # -- PepStore ---------------------------------------------------------

    async def fetch_device_list(self, bare_jid: str) -> Optional[ET.Element]:
        return await self._get_items(bare_jid, DEVICE_LIST_NODE, max_items=1)

    async def publish_device_list(self, bare_jid: str, element: ET.Element) -> None:
        await self._publish(DEVICE_LIST_NODE, element, "current", "1")

    async def fetch_bundle(self, bare_jid: str, device_id: int) -> Optional[ET.Element]:
        return await self._get_items(bare_jid, BUNDLE_NODE, item_ids=[str(device_id)])

    async def publish_bundle(self, bare_jid: str, device_id: int, element: ET.Element) -> None:
        await self._publish(BUNDLE_NODE, element, str(device_id), "max")

    async def delete_bundle(self, bare_jid: str, device_id: int) -> None:
        try:
            await self.xep_0060.retract(
                JID(self.own_bare_jid), BUNDLE_NODE, [str(device_id)], notify=False
            )
        except IqError as exc:
            if exc.condition == "item-not-found":
                return
            raise Omemo2Error("could not retract the OMEMO bundle: " + str(exc)) from exc
