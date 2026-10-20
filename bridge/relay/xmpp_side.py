"""The XMPP side, behind a tiny transport interface.

SlixmppTransport is the real implementation. Everything the rest of the service
needs is expressed by the XmppTransport protocol, so tests inject a fake and no
XMPP server (or even the slixmpp package) is required to run them.
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from .config import Config
from .logsetup import get_logger
from .models import IncomingXmpp, bare_jid
from .upstream import Backoff

log = get_logger("relay.xmpp")

IncomingHandler = Callable[[IncomingXmpp], Awaitable[None]]

# The wire element carrying an OMEMO 2 message (XEP-0384).
OMEMO2_ENCRYPTED_TAG = "{urn:xmpp:omemo:2}encrypted"


@runtime_checkable
class XmppTransport(Protocol):
    """The whole surface the bridge needs from XMPP."""

    def set_incoming_handler(self, handler: IncomingHandler) -> None:
        """Register the coroutine called for every inbound message."""

    async def start(self) -> None:
        """Connect (and, where supported, keep reconnecting in the background)."""

    async def stop(self) -> None:
        """Disconnect and release resources."""

    async def send_message(
        self,
        *,
        to: str,
        body: str,
        mtype: str = "chat",
        muc_nick: Optional[str] = None,
        frm: Optional[str] = None,
    ) -> None:
        """Send one chat or groupchat message.

        `frm` is only meaningful for component mode, where each peer JID is
        the sender; a client transport has a fixed account identity.
        """


class SlixmppTransport:
    """XmppTransport implemented on slixmpp.

    slixmpp already retries the initial connection with exponential backoff; on
    top of that this class watches the 'disconnected' event and reconnects
    itself, also with jittered backoff, so a server restart cannot wedge the
    bridge.

    Group chats use a single MUC occupant (cfg.muc_nick). The per-message sender
    is carried in the body by the routing layer; muc_nick is accepted for API
    compatibility and used only for logging.
    """

    def __init__(self, cfg: Config) -> None:
        import slixmpp  # imported lazily so tests never need the dependency

        self.cfg = cfg
        self._handler: Optional[IncomingHandler] = None
        self._stopping = False
        self._session_started: Optional[asyncio.Future] = None
        self._disconnected: Optional[asyncio.Event] = None
        self._backoff = Backoff(
            initial=cfg.backoff_initial,
            factor=cfg.backoff_factor,
            max_delay=cfg.backoff_max,
            jitter=cfg.backoff_jitter,
        )

        self.xmpp = slixmpp.ClientXMPP(cfg.jid, cfg.password)
        self.xmpp.register_plugin("xep_0199")  # ping
        if cfg.muc:
            self.xmpp.register_plugin("xep_0045")  # MUC
        self._omemo = None
        if cfg.omemo:
            self.xmpp.register_plugin("xep_0060")  # pubsub (OMEMO 2 PEP)
            self.xmpp.register_plugin("xep_0334")  # message processing hints
        self.xmpp.enable_direct_tls = cfg.direct_tls
        self.xmpp.enable_starttls = not cfg.direct_tls
        self.xmpp.enable_plaintext = False
        if cfg.insecure_tls:
            self.xmpp.ssl_context.check_hostname = False
            self.xmpp.ssl_context.verify_mode = ssl.CERT_NONE

        self.xmpp.add_event_handler("session_start", self._on_session_start)
        self.xmpp.add_event_handler("message", self._on_message)
        self.xmpp.add_event_handler("disconnected", self._on_disconnected)
        self.xmpp.add_event_handler("failed_auth", self._on_failed_auth)

    # -- XmppTransport -----------------------------------------------------

    def set_incoming_handler(self, handler: IncomingHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._disconnected = asyncio.Event()
        while not self._stopping:
            self._disconnected.clear()
            self._session_started = loop.create_future()
            log.info(
                "connecting to XMPP",
                event="xmpp_connect",
                jid=self.cfg.jid,
                server=self.cfg.server,
                port=self.cfg.port,
            )
            if self.cfg.server:
                self.xmpp.connect(host=self.cfg.server, port=self.cfg.port)
            else:
                self.xmpp.connect()
            try:
                await self._session_started
                self._backoff.reset()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("XMPP session failed", event="xmpp_session_error", error=str(exc))
                await self._sleep_or_stop(self._backoff.next_delay())
                continue
            await self._disconnected.wait()
            if self._stopping:
                break
            delay = self._backoff.next_delay()
            log.warning("XMPP disconnected; reconnecting", event="xmpp_reconnect", delay=round(delay, 3))
            await self._sleep_or_stop(delay)

    async def stop(self) -> None:
        self._stopping = True
        if self._disconnected is not None:
            self._disconnected.set()
        if self._omemo is not None:
            try:
                await self._omemo.close()
            except Exception:
                log.debug("OMEMO shutdown raised", exc_info=True, event="omemo_shutdown_error")
            self._omemo = None
        try:
            await asyncio.wait_for(self.xmpp.disconnect(wait=1.0), timeout=5)
        except asyncio.TimeoutError:
            log.warning("XMPP disconnect timed out", event="xmpp_disconnect_timeout")
        except Exception:
            log.debug("XMPP disconnect raised", exc_info=True, event="xmpp_disconnect_error")

    async def send_message(
        self,
        *,
        to: str,
        body: str,
        mtype: str = "chat",
        muc_nick: Optional[str] = None,
        frm: Optional[str] = None,
    ) -> None:
        if frm:
            # A client account cannot choose its From; ignore rather than spoof.
            log.debug("ignoring frm on a client transport", event="xmpp_frm_ignored", frm=frm)
        if mtype == "groupchat" and muc_nick:
            log.debug("sending groupchat", event="xmpp_groupchat", to=to, nick=muc_nick)
        if self.cfg.omemo and mtype in ("chat", "normal"):
            # 1:1 traffic is encrypted or not sent at all: never fall back to
            # plaintext.  Group chat is explicitly out of scope (see README).
            await self._send_encrypted(to=to, body=body, mtype=mtype)
            return
        msg = self.xmpp.make_message(mto=to, mbody=body, mtype=mtype)
        result = msg.send()
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result

    async def _send_encrypted(self, *, to: str, body: str, mtype: str) -> None:
        from .omemo2 import Omemo2Error

        if self._omemo is None:
            raise Omemo2Error(
                "OMEMO 2 is enabled but not initialised; refusing to send plaintext to " + to
            )
        element = await self._omemo.encrypt_body(body, to_jid=to)
        msg = self.xmpp.make_message(mto=to, mtype=mtype)
        msg.enable("store")
        msg.append(element)
        result = msg.send()
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result

    async def _start_omemo(self) -> None:
        from .omemo2 import Omemo2, Omemo2Error
        from .omemo2_slixmpp import SlixmppPepStore

        own = bare_jid(self.cfg.jid)
        manager = Omemo2(
            own_bare_jid=own,
            store_path=self.cfg.omemo_store,
            pep=SlixmppPepStore(self.xmpp, own),
            btbv=self.cfg.omemo_btbv,
            own_label="openbubbles-relay bridge",
        )
        try:
            await manager.start()
        except Exception as exc:  # noqa: BLE001 - a broken store must not kill the session
            # Leave _omemo None so every 1:1 send fails closed.
            log.error(
                "OMEMO 2 initialisation failed; 1:1 messages will NOT be sent in plaintext",
                event="omemo_init_error",
                error=str(exc),
            )
            return
        self._omemo = manager
        log.info(
            "OMEMO 2 ready",
            event="omemo_ready",
            jid=own,
            device_id=manager.device_id,
        )

    # -- event handlers ----------------------------------------------------

    async def _on_session_start(self, _event) -> None:
        log.info("XMPP session started", event="xmpp_session_start", jid=self.cfg.jid)
        try:
            self.xmpp.send_presence()
            try:
                await self.xmpp.get_roster()
            except Exception:
                log.warning("could not fetch roster", exc_info=True, event="xmpp_roster_error")
            if self.cfg.omemo:
                await self._start_omemo()
            if self.cfg.muc:
                try:
                    await self.xmpp.plugin["xep_0045"].join_muc_wait(
                        self.cfg.muc, self.cfg.muc_nick
                    )
                    log.info(
                        "joined MUC room",
                        event="xmpp_muc_joined",
                        room=self.cfg.muc,
                        nick=self.cfg.muc_nick,
                    )
                except Exception:
                    log.exception("could not join MUC room", event="xmpp_muc_error", room=self.cfg.muc)
        finally:
            if self._session_started is not None and not self._session_started.done():
                self._session_started.set_result(True)

    async def _on_message(self, msg) -> None:
        mtype = msg["type"]
        if mtype not in ("chat", "normal", "groupchat"):
            return
        sender = str(msg["from"])
        if bare_jid(sender) == bare_jid(self.cfg.jid):
            return  # our own MUC echo
        body = await self._extract_body(msg, mtype=mtype, sender=sender)
        if body is None:
            return
        incoming = IncomingXmpp(
            from_jid=sender,
            body=body,
            mtype=mtype,
            muc_room=bare_jid(sender) if mtype == "groupchat" else None,
        )
        if self._handler is None:
            log.warning("no incoming handler registered; dropping message", event="xmpp_no_handler")
            return
        await self._handler(incoming)

    async def _extract_body(self, msg, *, mtype: str, sender: str) -> Optional[str]:
        """Return the decrypted/plain body, or None when the stanza is dropped."""
        encrypted = msg.xml.find(OMEMO2_ENCRYPTED_TAG)
        if encrypted is not None:
            from .omemo2 import Omemo2Error  # only encrypted traffic needs the crypto stack

            if self._omemo is None:
                log.error(
                    "received an OMEMO 2 message but OMEMO is not initialised",
                    event="omemo_recv_unavailable",
                    from_jid=sender,
                )
                return None
            try:
                return await self._omemo.decrypt_element(encrypted, from_jid=sender)
            except Omemo2Error as exc:
                log.error(
                    "could not decrypt OMEMO 2 message; dropping it",
                    event="omemo_decrypt_error",
                    from_jid=sender,
                    error=str(exc),
                )
                return None
        if self.cfg.omemo and mtype in ("chat", "normal"):
            # Fail closed: an unencrypted 1:1 message is not forwarded to iMessage.
            log.warning(
                "refusing an unencrypted 1:1 message while XMPP_OMEMO is on",
                event="omemo_plaintext_refused",
                from_jid=sender,
            )
            return None
        if not msg["body"]:
            return None
        return str(msg["body"])

    def _on_disconnected(self, reason=None) -> None:
        log.warning("XMPP connection lost", event="xmpp_disconnected", reason=str(reason))
        if self._disconnected is not None:
            self._disconnected.set()

    def _on_failed_auth(self, _event=None) -> None:
        message = "XMPP authentication failed; check XMPP_JID/XMPP_PASSWORD"
        log.error(message, event="xmpp_auth_failed")
        if self._session_started is not None and not self._session_started.done():
            self._session_started.set_exception(RuntimeError(message))

    async def _sleep_or_stop(self, delay: float) -> None:
        assert self._disconnected is not None
        try:
            await asyncio.wait_for(self._disconnected.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            pass
