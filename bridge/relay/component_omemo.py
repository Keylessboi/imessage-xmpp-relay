"""OMEMO for component mode: a bridge-side emulation of the minimum PEP.

An XEP-0114 component has no account, hence no PEP/pubsub service and no
device list.  OMEMO clients nevertheless discover a contact's devices by
fetching `urn:xmpp:omemo:2:devices` (or the legacy
`eu.siacs.conversations.axolotl.devicelist`) from the contact's bare JID.  The
bridge therefore answers those IQs itself, for the component domain and for
every `<handle>@domain` peer JID.

One OMEMO identity per peer JID
-------------------------------
Clients key their sessions by (JID, device id).  The bridge keys its sessions
by (owner JID, owner device id).  If every peer JID shared one bridge device,
a session set up while chatting with peer A would be reused when sending as
peer B, but the owner's client has no session for (B, device) and could not
decrypt.  So each peer JID gets its own python-omemo SessionManager (own
device id, identity key, prekeys), all stored in the single XMPP_OMEMO_STORE
file under a per-peer key prefix and created lazily the first time the owner
asks about that peer or a message to/from it needs one.

Both backends (OMEMO 2 and legacy) live in each manager and share its identity.

The published device list / bundle of each peer manager is kept in the store
too, so a restart keeps serving exactly what the manager last published.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional, Protocol
from xml.etree import ElementTree as ET

from omemo.storage import Storage

from .config import Config
from .logsetup import get_logger
from .models import bare_jid
from .omemo2 import (
    BUNDLE_NODE,
    DEVICE_LIST_NODE,
    LEGACY_BUNDLE_NODE_PREFIX,
    LEGACY_DEVICE_LIST_NODE,
    LEGACY_NAMESPACE,
    NAMESPACE,
    JsonFileStorage,
    NoOmemoDevices,
    Omemo2,
    Omemo2Error,
    PrefixedStorage,
)

log = get_logger("relay.component.omemo")

PUBSUB_NS = "http://jabber.org/protocol/pubsub"
DISCO_INFO_NS = "http://jabber.org/protocol/disco#info"
DISCO_ITEMS_NS = "http://jabber.org/protocol/disco#items"

# What the emulation honestly supports: read-only PEP with persistent items.
# `#publish` is deliberately NOT advertised: every write is refused.
PEP_FEATURES = (
    DISCO_INFO_NS,
    DISCO_ITEMS_NS,
    PUBSUB_NS,
    PUBSUB_NS + "#retrieve-items",
    PUBSUB_NS + "#persistent-items",
)
KNOWN_NODES = (DEVICE_LIST_NODE, BUNDLE_NODE, LEGACY_DEVICE_LIST_NODE)


class PepError(Exception):
    """An IQ error to send back: (condition, error type, text)."""

    def __init__(self, condition: str, etype: str = "cancel", text: str = "") -> None:
        super().__init__(condition + (": " + text if text else ""))
        self.condition = condition
        self.etype = etype
        self.text = text


class ItemFetcher(Protocol):
    """Fetches one pubsub item from a remote PEP service over the component stream."""

    async def fetch_item_payload(
        self, *, frm: str, to: str, node: str, item_id: Optional[str] = None
    ) -> Optional[ET.Element]:
        """The payload of the item, or None when the node/item does not exist.

        Raises Omemo2Error when the answer could not be obtained (timeout, error).
        """


def disco_info_result(node: Optional[str]) -> ET.Element:
    """The disco#info <query/> the bridge answers with (identity pubsub/pep)."""
    query = ET.Element("{" + DISCO_INFO_NS + "}query")
    if node:
        if node not in KNOWN_NODES and not node.startswith(LEGACY_BUNDLE_NODE_PREFIX):
            raise PepError("item-not-found", "cancel", "unknown node")
        query.set("node", node)
        ET.SubElement(query, "{" + DISCO_INFO_NS + "}identity", {"category": "pubsub", "type": "leaf"})
        ET.SubElement(query, "{" + DISCO_INFO_NS + "}feature", {"var": PUBSUB_NS + "#retrieve-items"})
        return query
    ET.SubElement(
        query, "{" + DISCO_INFO_NS + "}identity",
        {"category": "pubsub", "type": "pep", "name": "OMEMO device directory"},
    )
    for feature in PEP_FEATURES:
        ET.SubElement(query, "{" + DISCO_INFO_NS + "}feature", {"var": feature})
    return query


def disco_items_result() -> ET.Element:
    return ET.Element("{" + DISCO_ITEMS_NS + "}query")


def items_result(node: str, item_id: str, payload: ET.Element) -> ET.Element:
    pubsub = ET.Element("{" + PUBSUB_NS + "}pubsub")
    items = ET.SubElement(pubsub, "{" + PUBSUB_NS + "}items", {"node": node})
    item = ET.SubElement(items, "{" + PUBSUB_NS + "}item", {"id": item_id})
    item.append(payload)
    return pubsub


class _KindPep:
    """PepStore for one backend of one peer identity.

    Publishes (what the manager uploads for itself) go to the persistent
    storage, and are what the bridge serves.  Fetches for another JID (the
    owner) go out as IQs from the peer JID over the component stream.
    """

    def __init__(self, *, kind: str, peer: str, storage: Storage, fetcher: ItemFetcher) -> None:
        self.kind = kind  # "omemo2" | "legacy"
        self.peer = peer
        self.storage = storage
        self.fetcher = fetcher

    # persisted keys
    def _devices_key(self) -> str:
        return "/relay/pep/" + self.kind + "/devices"

    def _bundle_key(self, device_id: int) -> str:
        return "/relay/pep/" + self.kind + "/bundle/" + str(device_id)

    async def _read(self, key: str) -> Optional[ET.Element]:
        raw = (await self.storage.load(key)).maybe(None)
        return ET.fromstring(raw) if isinstance(raw, str) else None

    async def _write(self, key: str, element: ET.Element) -> None:
        await self.storage.store(key, ET.tostring(element, encoding="unicode"))

    async def published_device_list(self) -> Optional[ET.Element]:
        return await self._read(self._devices_key())

    async def published_bundle(self, device_id: int) -> Optional[ET.Element]:
        return await self._read(self._bundle_key(device_id))

    @property
    def _device_list_node(self) -> str:
        return DEVICE_LIST_NODE if self.kind == "omemo2" else LEGACY_DEVICE_LIST_NODE

    def _bundle_node(self, device_id: int) -> str:
        return BUNDLE_NODE if self.kind == "omemo2" else LEGACY_BUNDLE_NODE_PREFIX + str(device_id)

    # PepStore ---------------------------------------------------------------

    async def fetch_device_list(self, jid: str) -> Optional[ET.Element]:
        if jid.lower() == self.peer:
            return await self.published_device_list()
        return await self.fetcher.fetch_item_payload(
            frm=self.peer, to=jid, node=self._device_list_node
        )

    async def publish_device_list(self, jid: str, element: ET.Element) -> None:
        await self._write(self._devices_key(), element)

    async def fetch_bundle(self, jid: str, device_id: int) -> Optional[ET.Element]:
        if jid.lower() == self.peer:
            return await self.published_bundle(device_id)
        return await self.fetcher.fetch_item_payload(
            frm=self.peer,
            to=jid,
            node=self._bundle_node(device_id),
            item_id=str(device_id) if self.kind == "omemo2" else None,
        )

    async def publish_bundle(self, jid: str, device_id: int, element: ET.Element) -> None:
        await self._write(self._bundle_key(device_id), element)

    async def delete_bundle(self, jid: str, device_id: int) -> None:
        await self.storage.delete(self._bundle_key(device_id))


class _Peer:
    def __init__(self, omemo: Omemo2, omemo2_pep: _KindPep, legacy_pep: _KindPep) -> None:
        self.omemo = omemo
        self.omemo2_pep = omemo2_pep
        self.legacy_pep = legacy_pep
        self.lock = asyncio.Lock()  # serialises encrypt/decrypt on this identity


class ComponentOmemo:
    """The OMEMO identities of a component and the PEP answers that go with them."""

    NOTICE_INTERVAL = 300.0

    def __init__(self, cfg: Config, fetcher: ItemFetcher) -> None:
        self.cfg = cfg
        self.domain = cfg.component_domain.strip().lower()
        self.component_jid = bare_jid(cfg.component_jid)
        self.fetcher = fetcher
        self._storage: Optional[JsonFileStorage] = None
        self._peers: Dict[str, _Peer] = {}
        self._creating: Dict[str, asyncio.Lock] = {}
        self._last_notice: Dict[str, float] = {}

    # lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Open (or create, mode 0600) the store.  Raises Omemo2Error if unusable."""
        storage = JsonFileStorage(self.cfg.omemo_store)
        try:
            storage.ensure_exists()
        except OSError as exc:
            raise Omemo2Error("cannot create the OMEMO store: " + str(exc)) from exc
        self._storage = storage

    async def close(self) -> None:
        for peer in list(self._peers.values()):
            try:
                await peer.omemo.close()
            except Exception:  # noqa: BLE001
                log.debug("OMEMO peer shutdown raised", exc_info=True, event="omemo_shutdown_error")
        self._peers.clear()

    # which JIDs have an identity ----------------------------------------------

    def is_peer(self, jid: str) -> bool:
        """True for the component domain and for every generated peer JID."""
        from .component import jid_to_handle  # local import: component imports us lazily

        bare = bare_jid(jid)
        if not bare:
            return False
        if bare == self.component_jid or bare == self.domain:
            return True
        return jid_to_handle(bare, self.domain) is not None

    async def _peer(self, jid: str) -> _Peer:
        assert self._storage is not None, "ComponentOmemo.start() was not called"
        bare = bare_jid(jid)
        existing = self._peers.get(bare)
        if existing is not None:
            return existing
        lock = self._creating.setdefault(bare, asyncio.Lock())
        async with lock:
            existing = self._peers.get(bare)
            if existing is not None:
                return existing
            storage = PrefixedStorage(self._storage, "peer/" + bare)
            omemo2_pep = _KindPep(kind="omemo2", peer=bare, storage=storage, fetcher=self.fetcher)
            legacy_pep = _KindPep(kind="legacy", peer=bare, storage=storage, fetcher=self.fetcher)
            manager = Omemo2(
                own_bare_jid=bare,
                store_path=self.cfg.omemo_store,
                pep=omemo2_pep,
                legacy_pep=legacy_pep,
                btbv=self.cfg.omemo_btbv,
                own_label="openbubbles-relay bridge",
                storage=storage,
            )
            await manager.start()
            peer = _Peer(manager, omemo2_pep, legacy_pep)
            self._peers[bare] = peer
            log.info("OMEMO identity ready for a peer JID", event="omemo_peer_ready",
                     peer=bare, device_id=manager.device_id)
            return peer

    # serving IQs -------------------------------------------------------------

    async def serve_items(self, peer_jid: str, node: str, item_ids: list) -> ET.Element:
        """The <pubsub/> result for an items GET, or PepError."""
        if not self.is_peer(peer_jid):
            raise PepError("service-unavailable")
        if node not in KNOWN_NODES and not node.startswith(LEGACY_BUNDLE_NODE_PREFIX):
            raise PepError("item-not-found", "cancel", "no such node")
        try:
            peer = await self._peer(peer_jid)
        except Omemo2Error as exc:
            log.error("OMEMO identity unavailable while answering an IQ", event="omemo_peer_error",
                      peer=peer_jid, error=str(exc))
            raise PepError("internal-server-error", "wait", "OMEMO unavailable") from exc
        device_id = peer.omemo.device_id

        if node in (DEVICE_LIST_NODE, LEGACY_DEVICE_LIST_NODE):
            if item_ids and any(i != "current" for i in item_ids):
                raise PepError("item-not-found", "cancel", "no such item")
            pep = peer.omemo2_pep if node == DEVICE_LIST_NODE else peer.legacy_pep
            payload = await pep.published_device_list()
            if payload is None:
                raise PepError("item-not-found", "cancel", "no device list")
            return items_result(node, "current", payload)

        if node == BUNDLE_NODE:
            wanted = item_ids or [str(device_id)]
            if len(wanted) != 1 or wanted[0] != str(device_id):
                raise PepError("item-not-found", "cancel", "no such device")
            payload = await peer.omemo2_pep.published_bundle(device_id)
            if payload is None:
                raise PepError("item-not-found", "cancel", "no bundle")
            return items_result(node, str(device_id), payload)

        # legacy bundle node: eu.siacs.conversations.axolotl.bundles:<deviceId>
        suffix = node[len(LEGACY_BUNDLE_NODE_PREFIX):]
        if suffix != str(device_id):
            raise PepError("item-not-found", "cancel", "no such device")
        payload = await peer.legacy_pep.published_bundle(device_id)
        if payload is None:
            raise PepError("item-not-found", "cancel", "no bundle")
        return items_result(node, "current", payload)

    # plaintext policy ------------------------------------------------------------
    #
    # `required`      every 1:1 message is encrypted or not delivered.
    # `opportunistic` encrypt whenever the owner advertises an OMEMO device; send
    #                 and accept plaintext only while the owner advertises none
    #                 AND the conversation has never been encrypted.  Once one
    #                 message in a conversation (peer JID <-> owner) was
    #                 encrypted in either direction, it is "sticky": plaintext is
    #                 refused for good, so a stripped device list or a missing
    #                 header cannot downgrade it.  The flag lives in the store.

    @property
    def opportunistic(self) -> bool:
        return self.cfg.omemo_policy == "opportunistic"

    def _sticky_key(self, peer_jid: str) -> str:
        return "relay/encrypted-conversation/" + bare_jid(peer_jid)

    async def is_sticky(self, peer_jid: str) -> bool:
        assert self._storage is not None
        return bool((await self._storage.load(self._sticky_key(peer_jid))).maybe(False))

    async def _mark_sticky(self, peer_jid: str) -> None:
        assert self._storage is not None
        if not await self.is_sticky(peer_jid):
            await self._storage.store(self._sticky_key(peer_jid), True)

    async def plaintext_allowed(self, peer_jid: str) -> bool:
        """May a message of this conversation travel unencrypted right now?"""
        return self.opportunistic and not await self.is_sticky(peer_jid)

    # encrypt / decrypt ---------------------------------------------------------

    async def encrypt(self, body: str, *, peer_jid: str, to_jid: str) -> list:
        """The <encrypted/> elements (one per backend used) for body; Omemo2Error if impossible."""
        """Raises NoOmemoDevices when the recipient has none (callers may then apply the policy)."""
        peer = await self._peer(peer_jid)
        async with peer.lock:
            elements = await peer.omemo.encrypt_multi(body, to_jid=to_jid)
        if self.opportunistic:
            await self._mark_sticky(peer_jid)
        return elements

    async def decrypt(self, element: ET.Element, *, peer_jid: str, from_jid: str) -> Optional[str]:
        peer = await self._peer(peer_jid)
        async with peer.lock:
            plaintext = await peer.omemo.decrypt_any(element, from_jid=from_jid)
        if self.opportunistic:
            await self._mark_sticky(peer_jid)
        return plaintext

    def notice_due(self, peer_jid: str) -> bool:
        """Rate limit for the plaintext 'could not deliver encrypted' notice."""
        now = time.monotonic()
        last = self._last_notice.get(peer_jid)
        if last is not None and now - last < self.NOTICE_INTERVAL:
            return False
        self._last_notice[peer_jid] = now
        return True


__all__ = [
    "ComponentOmemo", "ItemFetcher", "NoOmemoDevices", "PepError", "NAMESPACE", "LEGACY_NAMESPACE",
    "disco_info_result", "disco_items_result", "items_result",
]
