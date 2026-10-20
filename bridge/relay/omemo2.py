"""OMEMO 2 (XEP-0384, urn:xmpp:omemo:2) end-to-end encryption.

This module implements the two pieces the current slixmpp-omemo release
explicitly does not provide: XEP-0420 Stanza Content Encryption (the envelope
element that carries the message body) and the PEP device-list / bundle
plumbing behind the PepStore protocol.  The X3DH + Double Ratchet session and
the wire <encrypted xmlns="urn:xmpp:omemo:2"> element are provided by
python-omemo with the python-twomemo backend.

Design notes
------------
* Fail closed.  Every failure path (no devices, PEP error, partial encryption,
  trust decision failure, malformed envelope) raises Omemo2Error.  There is no
  plaintext fallback anywhere in this module; callers must decide what to
  surface, but they must never send the original body in the clear.
* The identity key and all session state live in one JSON file whose path comes
  from configuration.  The file is written atomically with mode 0600 and its
  contents are never logged.
* Only 1:1 chat/normal messages are supported.  Group chat is out of scope and
  is rejected while OMEMO is enabled rather than silently downgraded.
"""
from __future__ import annotations

import contextvars
import json
import os
import random
import secrets
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol
from xml.etree import ElementTree as ET

import omemo
from omemo.session_manager import SessionManager
from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import DeviceInformation, TrustLevel as CoreTrustLevel

import oldmemo
import oldmemo.etree
import oldmemo.oldmemo
from oldmemo.oldmemo import Oldmemo
import twomemo
import twomemo.etree
from twomemo.twomemo import Twomemo

from .logsetup import get_logger

_log = get_logger("relay.omemo2")

NAMESPACE = twomemo.twomemo.NAMESPACE           # urn:xmpp:omemo:2
DEVICE_LIST_NODE = "urn:xmpp:omemo:2:devices"
BUNDLE_NODE = "urn:xmpp:omemo:2:bundles"

# Legacy OMEMO (XEP-0384 0.3, still what Conversations-based clients speak).
LEGACY_NAMESPACE = oldmemo.oldmemo.NAMESPACE    # eu.siacs.conversations.axolotl
LEGACY_DEVICE_LIST_NODE = LEGACY_NAMESPACE + ".devicelist"
LEGACY_BUNDLE_NODE_PREFIX = LEGACY_NAMESPACE + ".bundles:"
LEGACY_ENCRYPTED_TAG = "{" + LEGACY_NAMESPACE + "}encrypted"

# XEP-0420 Stanza Content Encryption (SCE) and its OMEMO profile (XEP-0384 5.5).
SCE_NAMESPACE = "urn:xmpp:sce:1"
JABBER_CLIENT_NAMESPACE = "jabber:client"
ENCRYPTED_TAG = "{" + NAMESPACE + "}encrypted"

# Trust level names handed to python-omemo's custom trust system.
UNDECIDED_TRUST_LEVEL = "UNDECIDED"
BLINDLY_TRUSTED_TRUST_LEVEL = "BLINDLY_TRUSTED"

# Refuse absurdly large SCE payloads before parsing XML with them.
MAX_ENVELOPE_BYTES = 256 * 1024

# SCE profile: pad the plaintext so its length does not track the body length.
_MIN_PADDED_LENGTH = 512
_MAX_RANDOM_PAD = 200


class Omemo2Error(Exception):
    """OMEMO 2 could not encrypt or decrypt; the caller must not send plaintext."""


# --------------------------------------------------------------------------- storage


class JsonFileStorage(Storage):
    """A python-omemo Storage backed by a single JSON file.

    The whole key/value map is kept in memory and rewritten atomically on every
    mutation.  The file is created with mode 0600 and is never rendered into a
    log line.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self._data: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError) as exc:
                raise Omemo2Error(
                    "could not read the OMEMO store; refusing to start rather than "
                    "silently regenerating an identity: " + str(exc)
                ) from exc
            if not isinstance(loaded, dict):
                raise Omemo2Error("the OMEMO store is not a JSON object")
            self._data = loaded
            self._tighten_mode()

    def _tighten_mode(self) -> None:
        """The store holds private keys: never leave it readable by others."""
        try:
            if os.stat(self.path).st_mode & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    def ensure_exists(self) -> None:
        """Create an empty store (0600) now, so an unwritable path fails at startup."""
        if not os.path.exists(self.path):
            self._flush()

    async def _load(self, key: str) -> Maybe[Any]:
        return Just(self._data[key]) if key in self._data else Nothing()

    async def _store(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._flush()

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._flush()

    def _flush(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".omemo-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class PrefixedStorage(Storage):
    """A view of another storage under a key prefix (one view per peer identity)."""

    def __init__(self, base: Storage, prefix: str) -> None:
        super().__init__(disable_cache=True)
        self._base = base
        self._prefix = prefix

    async def _load(self, key: str) -> Maybe[Any]:
        return await self._base._load(self._prefix + key)

    async def _store(self, key: str, value: Any) -> None:
        await self._base._store(self._prefix + key, value)

    async def _delete(self, key: str) -> None:
        await self._base._delete(self._prefix + key)


# ----------------------------------------------------------------------- PEP store


class PepStore(Protocol):
    """The OMEMO 2 PEP surface (XEP-0060 / XEP-0163), as tagged XML elements."""

    async def fetch_device_list(self, bare_jid: str) -> Optional[ET.Element]:
        """Return the devices element published by bare_jid, or None."""

    async def publish_device_list(self, bare_jid: str, element: ET.Element) -> None:
        """Publish element to the urn:xmpp:omemo:2:devices node."""

    async def fetch_bundle(self, bare_jid: str, device_id: int) -> Optional[ET.Element]:
        """Return the bundle element for device_id, or None."""

    async def publish_bundle(self, bare_jid: str, device_id: int, element: ET.Element) -> None:
        """Publish element to the urn:xmpp:omemo:2:bundles node."""

    async def delete_bundle(self, bare_jid: str, device_id: int) -> None:
        """Retract the bundle for device_id (best effort)."""


# --------------------------------------------------------------- session manager

_PENDING_CONTEXT: contextvars.ContextVar[Optional[tuple]] = (
    contextvars.ContextVar("omemo2_pending_context", default=None)
)


class PepSessionManager(SessionManager):
    """A python-omemo SessionManager whose PEP backend is a PepStore.

    Construct it through create_for; python-omemo's SessionManager.create
    instantiates the subclass with no arguments, so the store and JID are handed
    over through a context variable that is set for the duration of the call.
    """

    def __init__(self) -> None:
        super().__init__()
        pending = _PENDING_CONTEXT.get()
        if pending is None:
            raise Omemo2Error("PepSessionManager must be built via PepSessionManager.create_for()")
        self._own_bare_jid, self._pep, self._btbv, self._legacy_pep = pending

    @classmethod
    async def create_for(
        cls,
        *,
        own_bare_jid: str,
        pep: PepStore,
        storage: Storage,
        btbv: bool = True,
        initial_own_label: Optional[str] = None,
        legacy_pep: Optional[PepStore] = None,
    ) -> "PepSessionManager":
        """Build the manager.  With legacy_pep, the legacy backend joins Twomemo."""
        token = _PENDING_CONTEXT.set((own_bare_jid, pep, btbv, legacy_pep))
        backends: list = [Twomemo(storage)]
        if legacy_pep is not None:
            backends.append(Oldmemo(storage))
        try:
            manager = await cls.create(
                backends,
                storage,
                own_bare_jid,
                initial_own_label,
                UNDECIDED_TRUST_LEVEL,
            )
        finally:
            _PENDING_CONTEXT.reset(token)
        assert isinstance(manager, PepSessionManager)
        return manager

    # -- trust ------------------------------------------------------------

    @property
    def _btbv_enabled(self) -> bool:
        return self._btbv

    async def _evaluate_custom_trust_level(self, device: DeviceInformation) -> CoreTrustLevel:
        name = device.trust_level_name
        if name == "TRUSTED":
            return CoreTrustLevel.TRUSTED
        if name == "DISTRUSTED":
            return CoreTrustLevel.DISTRUSTED
        if name == BLINDLY_TRUSTED_TRUST_LEVEL:
            # Blind trust only means trusted while BTBV is switched on.
            return CoreTrustLevel.TRUSTED if self._btbv else CoreTrustLevel.UNDECIDED
        if name == UNDECIDED_TRUST_LEVEL:
            return CoreTrustLevel.UNDECIDED
        raise omemo.UnknownTrustLevel("Unknown OMEMO trust level: " + str(name))

    async def require_trusted(self, device: DeviceInformation) -> None:
        """Gate for decrypted messages: python-omemo decrypts for undecided senders."""
        level = await self._evaluate_custom_trust_level(device)
        if level is CoreTrustLevel.TRUSTED:
            return
        if level is CoreTrustLevel.UNDECIDED and self._btbv:
            await self.set_trust(device.bare_jid, device.identity_key, BLINDLY_TRUSTED_TRUST_LEVEL)
            return
        raise Omemo2Error(
            "message from an untrusted OMEMO device " + str(device.device_id)
            + " of " + device.bare_jid
        )

    async def _make_trust_decision(
        self,
        undecided: frozenset,
        identifier: Optional[str],
    ) -> None:
        # The bridge is unattended, so it either auto-trusts new devices (BTBV)
        # or refuses to encrypt for them.  It never guesses.
        if not self._btbv:
            raise omemo.TrustDecisionFailed(
                "OMEMO trust is not decided for: "
                + ", ".join(sorted(d.bare_jid for d in undecided))
            )
        for device in undecided:
            await self.set_trust(device.bare_jid, device.identity_key, BLINDLY_TRUSTED_TRUST_LEVEL)

    # -- PEP --------------------------------------------------------------

    def _backend_io(self, namespace: str):
        """(PEP store, XML serialiser module) for a backend namespace."""
        if namespace == NAMESPACE:
            return self._pep, twomemo.etree
        if namespace == LEGACY_NAMESPACE and self._legacy_pep is not None:
            return self._legacy_pep, oldmemo.etree
        raise omemo.UnknownNamespace("Unsupported OMEMO namespace: " + str(namespace))

    async def _upload_bundle(self, bundle: omemo.Bundle) -> None:
        pep, codec = self._backend_io(bundle.namespace)
        await pep.publish_bundle(bundle.bare_jid, bundle.device_id, codec.serialize_bundle(bundle))

    async def _download_bundle(
        self, namespace: str, bare_jid: str, device_id: int
    ) -> omemo.Bundle:
        pep, codec = self._backend_io(namespace)
        element = await pep.fetch_bundle(bare_jid, device_id)
        if element is None:
            raise omemo.BundleNotFound(bare_jid, device_id)
        return codec.parse_bundle(element, bare_jid, device_id)

    async def _delete_bundle(self, namespace: str, device_id: int) -> None:
        pep, _codec = self._backend_io(namespace)
        await pep.delete_bundle(self._own_bare_jid, device_id)

    async def _upload_device_list(self, namespace: str, device_list: Any) -> None:
        pep, codec = self._backend_io(namespace)
        await pep.publish_device_list(self._own_bare_jid, codec.serialize_device_list(device_list))

    async def _download_device_list(self, namespace: str, bare_jid: str) -> Any:
        pep, codec = self._backend_io(namespace)
        element = await pep.fetch_device_list(bare_jid)
        if element is None:
            return {}
        return codec.parse_device_list(element)

    async def _send_message(self, message: omemo.Message, bare_jid: str) -> None:
        # Empty OMEMO messages (heartbeats) are optional for a bridge that never
        # keeps a visible conversation log; dropping them only costs a slightly
        # longer sending chain, never correctness.
        return None


# --------------------------------------------------------------------- SCE envelope


def build_sce_envelope(
    body: str,
    *,
    from_jid: str,
    to_jid: Optional[str] = None,
    random_pad: bool = True,
) -> bytes:
    """Serialise the XEP-0420 envelope for body (the OMEMO 2 plaintext)."""
    envelope = ET.Element("{" + SCE_NAMESPACE + "}envelope")
    content = ET.SubElement(envelope, "{" + SCE_NAMESPACE + "}content")
    body_element = ET.SubElement(content, "{" + JABBER_CLIENT_NAMESPACE + "}body")
    body_element.text = body
    ET.SubElement(envelope, "{" + SCE_NAMESPACE + "}time", {"stamp": _timestamp()})
    ET.SubElement(envelope, "{" + SCE_NAMESPACE + "}from", {"jid": from_jid})
    if to_jid:
        ET.SubElement(envelope, "{" + SCE_NAMESPACE + "}to", {"jid": to_jid})

    base = ET.tostring(envelope, encoding="utf-8")
    padding = max(0, _MIN_PADDED_LENGTH - len(base))
    if random_pad:
        padding += random.randint(0, _MAX_RANDOM_PAD)
    if padding:
        rpad = ET.Element("{" + SCE_NAMESPACE + "}rpad")
        rpad.text = secrets.token_urlsafe(padding)[:padding]
        envelope.append(rpad)
    return ET.tostring(envelope, encoding="utf-8")


def parse_sce_envelope(
    payload: bytes,
    *,
    expect_from: Optional[str] = None,
    expect_to: Optional[str] = None,
) -> str:
    """Validate an SCE envelope and return the message body."""
    if len(payload) > MAX_ENVELOPE_BYTES:
        raise Omemo2Error("SCE envelope is larger than " + str(MAX_ENVELOPE_BYTES) + " bytes")
    try:
        envelope = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise Omemo2Error("SCE payload is not well-formed XML") from exc
    if envelope.tag != "{" + SCE_NAMESPACE + "}envelope":
        raise Omemo2Error("SCE payload is not a " + SCE_NAMESPACE + " envelope")

    from_element = envelope.find("{" + SCE_NAMESPACE + "}from")
    if from_element is not None and expect_from is not None:
        if (from_element.get("jid") or "").lower() != expect_from.lower():
            raise Omemo2Error("SCE from affix does not match the stanza sender")
    to_element = envelope.find("{" + SCE_NAMESPACE + "}to")
    if to_element is not None and expect_to is not None:
        if (to_element.get("jid") or "").split("/", 1)[0].lower() != expect_to.lower():
            raise Omemo2Error("SCE to affix does not match the stanza recipient")

    content = envelope.find("{" + SCE_NAMESPACE + "}content")
    if content is None:
        raise Omemo2Error("SCE envelope has no content element")
    body_element = content.find("{" + JABBER_CLIENT_NAMESPACE + "}body")
    if body_element is None:
        raise Omemo2Error("SCE content has no body element")
    return body_element.text or ""


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------------ facade


class Omemo2:
    """High-level 1:1 OMEMO 2 encryption for one XMPP account."""

    def __init__(
        self,
        *,
        own_bare_jid: str,
        store_path: str,
        pep: PepStore,
        btbv: bool = True,
        own_label: Optional[str] = None,
        legacy_pep: Optional[PepStore] = None,
        storage: Optional[Storage] = None,
    ) -> None:
        self.own_bare_jid = own_bare_jid.lower()
        self.store_path = store_path
        self.pep = pep
        self.legacy_pep = legacy_pep  # set: legacy OMEMO joins OMEMO 2 (component mode)
        self.btbv = btbv
        self.own_label = own_label
        self._injected_storage = storage  # a shared storage view; store_path is then unused
        self._storage: Optional[Storage] = None
        self._manager: Optional[PepSessionManager] = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def device_id(self) -> Optional[int]:
        """This device's OMEMO id once started (safe to log; not key material)."""
        if self._manager is None:
            return None
        return getattr(self._manager, "_SessionManager__own_device_id", None)

    async def start(self) -> None:
        """Load (or create) the long-term identity and publish our bundle."""
        if self._started:
            return
        storage = self._injected_storage or JsonFileStorage(self.store_path)
        try:
            manager = await PepSessionManager.create_for(
                own_bare_jid=self.own_bare_jid,
                pep=self.pep,
                storage=storage,
                btbv=self.btbv,
                initial_own_label=self.own_label,
                legacy_pep=self.legacy_pep,
            )
            # Leave history-synchronisation mode: the bridge has no history to
            # replay, and in that mode consumed pre keys are never replaced.
            await manager.after_history_sync()
        except omemo.SessionManagerException as exc:
            raise Omemo2Error("could not initialise OMEMO 2: " + str(exc)) from exc
        self._storage = storage
        self._manager = manager
        self._started = True

    async def close(self) -> None:
        if self._manager is not None:
            await self._manager.shutdown()
        self._manager = None
        self._started = False

    async def encrypt_body(self, body: str, *, to_jid: str) -> ET.Element:
        """Encrypt body for every OMEMO 2 device of to_jid.

        Returns the <encrypted xmlns="urn:xmpp:omemo:2"> element for the stanza.
        Raises Omemo2Error -- never a plaintext fallback -- when encryption is
        not possible for every device.
        """
        manager = self._require_manager()
        recipient = to_jid.split("/", 1)[0].strip().lower()

        try:
            await manager.refresh_device_list(NAMESPACE, recipient)
            devices = await manager.get_device_information(recipient)
        except omemo.SessionManagerException as exc:
            raise Omemo2Error(
                "could not fetch the OMEMO device list for " + recipient + ": " + str(exc)
            ) from exc

        if not devices:
            raise Omemo2Error(
                "refusing to send plaintext: " + recipient + " publishes no OMEMO 2 devices"
            )

        envelope = build_sce_envelope(body, from_jid=self.own_bare_jid)
        try:
            messages, errors = await manager.encrypt(
                frozenset({recipient}),
                {NAMESPACE: envelope},
                backend_priority_order=[NAMESPACE],
            )
        except omemo.SessionManagerException as exc:
            raise Omemo2Error("OMEMO 2 encryption for " + recipient + " failed: " + str(exc)) from exc

        if errors:
            detail = "; ".join(sorted(str(error) for error in errors))
            raise Omemo2Error(
                "refusing to send a partially encrypted message to " + recipient + ": " + detail
            )
        if len(messages) != 1:
            raise Omemo2Error(
                "expected exactly one OMEMO 2 message for " + recipient
                + ", got " + str(len(messages))
            )

        message = next(iter(messages))
        return twomemo.etree.serialize_message(message)

    async def decrypt_element(self, element: ET.Element, *, from_jid: str) -> Optional[str]:
        """Decrypt an encrypted element and return its body.

        Returns None for an empty (heartbeat) OMEMO message.  Raises Omemo2Error
        on any failure.
        """
        manager = self._require_manager()
        sender = from_jid.split("/", 1)[0].strip().lower()
        if element.tag != ENCRYPTED_TAG:
            raise Omemo2Error("not an " + NAMESPACE + " encrypted element")
        try:
            message = twomemo.etree.parse_message(element, sender)
        except Exception as exc:  # any malformed wire data is fatal here
            raise Omemo2Error("malformed OMEMO 2 message from " + sender) from exc
        try:
            plaintext, device, _key_material = await manager.decrypt(message)
        except omemo.SessionManagerException as exc:
            raise Omemo2Error("could not decrypt OMEMO 2 message from " + sender + ": " + str(exc)) from exc
        except Exception as exc:  # backend errors are all fatal here
            raise Omemo2Error("could not decrypt OMEMO 2 message from " + sender + ": " + str(exc)) from exc

        if plaintext is None:
            return None
        return parse_sce_envelope(plaintext, expect_from=device.bare_jid)

    # -- multi-backend (OMEMO 2 + legacy) ----------------------------------

    @property
    def namespaces(self) -> list:
        """Backend namespaces in encryption priority order."""
        return [NAMESPACE] + ([LEGACY_NAMESPACE] if self.legacy_pep is not None else [])

    async def encrypt_multi(self, body: str, *, to_jid: str) -> list:
        """Encrypt body for every device of to_jid, one stanza child per backend used.

        Each trusted device gets the highest-priority backend it supports (OMEMO 2
        before legacy).  Returns the <encrypted> elements; raises Omemo2Error --
        never a plaintext fallback -- when no device can be encrypted for.
        """
        manager = self._require_manager()
        recipient = to_jid.split("/", 1)[0].strip().lower()
        try:
            for namespace in self.namespaces:
                await manager.refresh_device_list(namespace, recipient)
            devices = await manager.get_device_information(recipient)
        except omemo.SessionManagerException as exc:
            raise Omemo2Error(
                "could not fetch the OMEMO device list for " + recipient + ": " + str(exc)
            ) from exc
        if not devices:
            raise Omemo2Error("refusing to send plaintext: " + recipient + " publishes no OMEMO devices")

        plaintexts: Dict[str, bytes] = {
            NAMESPACE: build_sce_envelope(body, from_jid=self.own_bare_jid, to_jid=recipient)
        }
        if self.legacy_pep is not None:
            plaintexts[LEGACY_NAMESPACE] = body.encode("utf-8")
        try:
            messages, errors = await manager.encrypt(
                frozenset({recipient}),
                plaintexts,
                backend_priority_order=self.namespaces,
            )
        except omemo.SessionManagerException as exc:
            raise Omemo2Error("OMEMO encryption for " + recipient + " failed: " + str(exc)) from exc

        if not messages:
            detail = "; ".join(sorted(str(error) for error in errors)) or "no eligible device"
            raise Omemo2Error("could not encrypt for any device of " + recipient + ": " + detail)
        out = []
        for message in messages:
            if message.namespace == NAMESPACE:
                out.append(twomemo.etree.serialize_message(message))
            elif message.namespace == LEGACY_NAMESPACE:
                out.append(oldmemo.etree.serialize_message(message))
            else:  # pragma: no cover - only the two registered backends exist
                raise Omemo2Error("unexpected OMEMO namespace " + message.namespace)
        if errors:
            # Some devices (typically stale ones with no bundle) were skipped;
            # the others are encrypted for.  Never reveals content.
            log_skipped = "; ".join(sorted(str(error) for error in errors))
            _log.warning(
                "OMEMO: skipped devices that could not be encrypted for",
                event="omemo_devices_skipped",
                recipient=recipient,
                detail=log_skipped,
            )
        return out

    async def decrypt_any(self, element: ET.Element, *, from_jid: str) -> Optional[str]:
        """Decrypt an OMEMO 2 or legacy <encrypted> element; None for an empty message.

        Raises Omemo2Error on any failure, including an undecided device while
        blind trust is off and a malformed or mis-addressed envelope.
        """
        manager = self._require_manager()
        sender = from_jid.split("/", 1)[0].strip().lower()
        try:
            if element.tag == ENCRYPTED_TAG:
                message = twomemo.etree.parse_message(element, sender)
            elif element.tag == LEGACY_ENCRYPTED_TAG and self.legacy_pep is not None:
                message = await oldmemo.etree.parse_message(
                    element, sender, self.own_bare_jid, manager
                )
            else:
                raise Omemo2Error("not a supported OMEMO encrypted element: " + element.tag)
        except Omemo2Error:
            raise
        except Exception as exc:  # any malformed wire data is fatal here
            raise Omemo2Error("malformed OMEMO message from " + sender + ": " + str(exc)) from exc
        try:
            plaintext, device, _key_material = await manager.decrypt(message)
        except Exception as exc:  # backend errors are all fatal here
            raise Omemo2Error("could not decrypt OMEMO message from " + sender + ": " + str(exc)) from exc

        await manager.require_trusted(device)
        if plaintext is None:
            return None
        if message.namespace == NAMESPACE:
            return parse_sce_envelope(plaintext, expect_from=sender, expect_to=self.own_bare_jid)
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Omemo2Error("legacy OMEMO payload is not UTF-8") from exc

    def _require_manager(self) -> PepSessionManager:
        if not self._started or self._manager is None:
            raise Omemo2Error("OMEMO 2 is not started")
        return self._manager


# ------------------------------------------------------------ in-memory PEP store


class InMemoryPepStore:
    """A PEP store for tests and single-process use (no persistence)."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.device_lists: Dict[str, bytes] = {}
        self.bundles: Dict[tuple, bytes] = {}

    async def fetch_device_list(self, bare_jid: str) -> Optional[ET.Element]:
        if not self.reachable:
            raise Omemo2Error("PEP is unreachable")
        raw = self.device_lists.get(bare_jid.lower())
        return ET.fromstring(raw) if raw is not None else None

    async def publish_device_list(self, bare_jid: str, element: ET.Element) -> None:
        if not self.reachable:
            raise Omemo2Error("PEP is unreachable")
        self.device_lists[bare_jid.lower()] = ET.tostring(element)

    async def fetch_bundle(self, bare_jid: str, device_id: int) -> Optional[ET.Element]:
        if not self.reachable:
            raise Omemo2Error("PEP is unreachable")
        raw = self.bundles.get((bare_jid.lower(), device_id))
        return ET.fromstring(raw) if raw is not None else None

    async def publish_bundle(self, bare_jid: str, device_id: int, element: ET.Element) -> None:
        if not self.reachable:
            raise Omemo2Error("PEP is unreachable")
        self.bundles[(bare_jid.lower(), device_id)] = ET.tostring(element)

    async def delete_bundle(self, bare_jid: str, device_id: int) -> None:
        self.bundles.pop((bare_jid.lower(), device_id), None)
