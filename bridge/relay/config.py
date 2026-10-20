"""Configuration from environment variables.

The password is held in a field with repr=False and is never included in
Config.describe(), which is the only rendering of the config the service logs.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .logsetup import get_logger

log = get_logger("relay.config")

DEFAULT_API_URL = "http://imessage-server:8790"


class ConfigError(Exception):
    """Raised for missing or contradictory environment variables."""


# Secrets that may be supplied as a file instead of an environment variable. Environment
# variables show up in process listings, crash reports and `kubectl describe`; a mounted file
# (Kubernetes Secret volume, Docker secret) does not. Set NAME_FILE=/path instead of NAME.
_FILE_SECRETS = ("XMPP_PASSWORD", "COMPONENT_SECRET")


def _resolve_secret_files(e: dict[str, str]) -> None:
    """Fill ``e[NAME]`` from the file named by ``e[NAME_FILE]``, in place.

    Fails closed: a missing/unreadable file or setting both forms is a ConfigError, and the
    message never contains the file's contents. One trailing newline is stripped, because
    that is how editors and ``echo`` write the file, never part of the secret.
    """
    for name in _FILE_SECRETS:
        path = (e.get(name + "_FILE") or "").strip()
        if not path:
            continue
        if (e.get(name) or "").strip():
            raise ConfigError(f"set either {name} or {name}_FILE, not both")
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{name}_FILE={path} is not readable ({type(exc).__name__})") from None
        if value.endswith("\n"):
            value = value[:-1]
        if not value.strip():
            raise ConfigError(f"{name}_FILE={path} is empty")
        e[name] = value


def _bool(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def split_list(raw: Optional[str]) -> list[str]:
    """Comma and/or whitespace separated list."""
    return [item for item in (raw or "").replace(",", " ").split() if item]


def parse_contacts(raw: Optional[str]) -> dict[str, str]:
    """Parse comma/semicolon separated 'handle=jid' pairs.

    Entries may contain spaces (a phone handle often does), so only the
    separators split them. Phone handles are normalised to +digits and JIDs
    are lowercased.
    """
    contacts: dict[str, str] = {}
    for chunk in re.split(r"[,;]", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ConfigError("XMPP_CONTACTS entry is not handle=jid: " + chunk)
        handle, jid = chunk.split("=", 1)
        handle = handle.strip()
        jid = jid.strip().lower()
        if not handle or not jid:
            raise ConfigError("XMPP_CONTACTS entry is not handle=jid: " + chunk)
        from .models import normalize_handle
        contacts[normalize_handle(handle)] = jid
    return contacts


@dataclass
class Config:
    # Transport mode: "client" (a logged-in bot account) or "component"
    # (a XEP-0114 external component that owns a domain). Client is the default
    # so an existing deployment is unaffected.
    mode: str = "client"           # XMPP_MODE

    # Required in client mode
    jid: str = ""
    password: str = field(default="", repr=False)

    # Component mode (XEP-0114). The secret is never rendered by describe()
    # and never appears in repr().
    component_jid: str = ""              # COMPONENT_JID: the bare domain the component owns
    component_domain: str = ""           # COMPONENT_DOMAIN: domain of the peer JIDs (defaults to component_jid)
    component_host: str = "127.0.0.1"    # COMPONENT_HOST: server component listener
    component_port: int = 5347           # COMPONENT_PORT
    component_secret: str = field(default="", repr=False)  # COMPONENT_SECRET
    component_owner: str = ""            # COMPONENT_OWNER: the only JID allowed to send

    # XMPP connection
    server: Optional[str] = None   # "host" or "host:5223"
    port: int = 5222
    insecure_tls: bool = False
    direct_tls: bool = False       # implicit TLS (xmpps) instead of STARTTLS

    # Delivery targets
    targets: tuple[str, ...] = ()          # XMPP_TARGET, the pipe JIDs
    muc: Optional[str] = None              # XMPP_MUC room (bare JID)
    muc_nick: str = "imessage"             # XMPP_MUC_NICK
    contacts: dict[str, str] = field(default_factory=dict)  # handle -> JID
    prefix_sender: bool = True             # prefix 1:1 pipe bodies with the sender
    default_to: Optional[str] = None       # fallback destination for XMPP -> iMessage

    # Access control
    allowed: frozenset[str] = frozenset()  # bare JIDs allowed to talk to the bot

    # Upstream
    api_url: str = DEFAULT_API_URL
    request_timeout: float = 15.0

    # OMEMO 2 (end-to-end encryption); off by default
    omemo: bool = False                    # XMPP_OMEMO: encrypt 1:1 chat
    omemo_store: str = ""                  # XMPP_OMEMO_STORE: identity/session file (required with XMPP_OMEMO)
    omemo_btbv: bool = True                # XMPP_OMEMO_BTBV: auto-trust new devices

    # Behaviour
    dedup_size: int = 4096
    backoff_initial: float = 1.0
    backoff_factor: float = 2.0
    backoff_max: float = 60.0
    backoff_jitter: float = 0.5
    healthy_stream_after: float = 30.0
    log_level: str = "INFO"

    @staticmethod
    def _parse_component(e, *, targets, allowed, bare_jid):
        """Validate and derive the component-mode settings.

        Returns (component_jid, component_domain, host, port, secret, owner,
        targets, allowed). The owner is the single JID allowed to send, and is
        added to the allowlist so the deny-by-default core accepts it.
        """
        component_jid = (e.get("COMPONENT_JID") or "").strip().lower()
        component_domain = (e.get("COMPONENT_DOMAIN") or "").strip().lower()
        if not component_jid and component_domain:
            component_jid = component_domain
        if not component_jid:
            raise ConfigError(
                "XMPP_MODE=component requires COMPONENT_JID (or COMPONENT_DOMAIN), "
                "e.g. COMPONENT_JID=imessage.example.org"
            )
        if "@" in component_jid or "/" in component_jid:
            raise ConfigError(
                "COMPONENT_JID must be a bare domain (e.g. imessage.example.org), not a full JID"
            )
        if not component_domain:
            component_domain = component_jid
        elif component_domain != component_jid:
            log.warning(
                "COMPONENT_DOMAIN differs from COMPONENT_JID: peer JIDs are built in "
                "COMPONENT_DOMAIN, so the server must route that domain to this component",
                event="component_domain_mismatch",
                component_jid=component_jid,
                component_domain=component_domain,
            )

        component_secret = e.get("COMPONENT_SECRET") or ""
        if not component_secret.strip():
            raise ConfigError(
                "XMPP_MODE=component requires COMPONENT_SECRET (the XEP-0114 shared secret)"
            )

        component_host = (e.get("COMPONENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            component_port = int((e.get("COMPONENT_PORT") or "5347").strip())
        except ValueError:
            raise ConfigError("COMPONENT_PORT must be an integer") from None
        if not 1 <= component_port <= 65535:
            raise ConfigError("COMPONENT_PORT must be between 1 and 65535")

        owner = (e.get("COMPONENT_OWNER") or "").strip().lower()
        if not owner and targets:
            owner = targets[0].lower()
        if not owner and allowed:
            owner = sorted(allowed)[0]
        if not owner:
            raise ConfigError(
                "XMPP_MODE=component needs an owner JID: set COMPONENT_OWNER "
                "(or XMPP_TARGET / XMPP_ALLOWED)"
            )
        if "@" not in owner:
            raise ConfigError("COMPONENT_OWNER must be a full JID, e.g. you@example.org")
        owner_bare = bare_jid(owner)
        if owner_bare == component_jid or owner_bare.endswith("@" + component_domain):
            raise ConfigError(
                "COMPONENT_OWNER must not be inside the component's own domain: "
                + owner_bare
                + " (that would make the bridge talk to itself)"
            )

        targets = tuple(targets)
        if targets and owner_bare not in {t.lower() for t in targets}:
            log.warning(
                "COMPONENT_OWNER is not one of XMPP_TARGET: inbound iMessages go to "
                "XMPP_TARGET but only COMPONENT_OWNER may send",
                event="component_owner_not_target",
                owner=owner_bare,
                targets=",".join(targets),
            )
        if not targets:
            targets = (owner_bare,)
        if owner_bare not in allowed:
            allowed = allowed | {owner_bare}
            log.info(
                "component mode: the owner JID may send iMessages",
                event="component_owner_allowed",
                owner=owner_bare,
            )

        if _bool(e.get("XMPP_OMEMO")):
            raise ConfigError(
                "XMPP_OMEMO=1 is not supported in component mode: an external "
                "component has no OMEMO device identity; component traffic is plaintext"
            )

        return (
            component_jid,
            component_domain,
            component_host,
            component_port,
            component_secret,
            owner_bare,
            targets,
            allowed,
        )

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "Config":
        from .models import bare_jid

        e = dict(os.environ if env is None else env)
        _resolve_secret_files(e)
        mode = (e.get("XMPP_MODE") or "client").strip().lower()
        if mode not in ("client", "component"):
            raise ConfigError(
                "XMPP_MODE must be 'client' or 'component', not " + repr(e.get("XMPP_MODE"))
            )
        if mode == "client":
            missing = [k for k in ("XMPP_JID", "XMPP_PASSWORD") if not (e.get(k) or "").strip()]
            if missing:
                raise ConfigError("missing required environment variable(s): " + ", ".join(missing))

        targets = tuple(split_list(e.get("XMPP_TARGET")))
        muc = (e.get("XMPP_MUC") or "").strip() or None
        contacts = parse_contacts(e.get("XMPP_CONTACTS"))
        allowed = frozenset(j.lower() for j in split_list(e.get("XMPP_ALLOWED")))

        component_jid = ""
        component_domain = ""
        component_host = "127.0.0.1"
        component_port = 5347
        component_secret = ""
        component_owner = ""
        if mode == "component":
            (
                component_jid,
                component_domain,
                component_host,
                component_port,
                component_secret,
                component_owner,
                targets,
                allowed,
            ) = cls._parse_component(
                e, targets=targets, allowed=allowed, bare_jid=bare_jid
            )

        if not targets and not muc and not contacts:
            raise ConfigError(
                "nothing to deliver to: set XMPP_TARGET, XMPP_MUC or XMPP_CONTACTS "
                "(without at least one the bridge has no way to show an iMessage)"
            )

        server = (e.get("XMPP_SERVER") or "").strip() or None
        port = int(e.get("XMPP_PORT") or 5222)
        if server and ":" in server:
            server, _, raw_port = server.rpartition(":")
            if raw_port.isdigit():
                port = int(raw_port)
            else:
                server = server + ":" + raw_port
        if not server:
            server = None

        cfg = cls(
            mode=mode,
            jid=(e.get("XMPP_JID") or "").strip(),
            password=e.get("XMPP_PASSWORD") or "",
            server=server,
            port=port,
            insecure_tls=_bool(e.get("XMPP_INSECURE_TLS")),
            direct_tls=_bool(e.get("XMPP_DIRECT_TLS")),
            targets=targets,
            muc=muc,
            muc_nick=(e.get("XMPP_MUC_NICK") or "imessage").strip() or "imessage",
            contacts=contacts,
            prefix_sender=not _bool(e.get("XMPP_NO_SENDER_PREFIX")),
            default_to=(e.get("IMESSAGE_DEFAULT_TO") or "").strip() or None,
            allowed=allowed,
            api_url=(e.get("IMESSAGE_API_URL") or DEFAULT_API_URL).rstrip("/"),
            request_timeout=float(e.get("REQUEST_TIMEOUT") or 15),
            dedup_size=int(e.get("DEDUP_SIZE") or 4096),
            backoff_initial=float(e.get("BACKOFF_INITIAL") or 1.0),
            backoff_factor=float(e.get("BACKOFF_FACTOR") or 2.0),
            backoff_max=float(e.get("BACKOFF_MAX") or 60.0),
            backoff_jitter=float(e.get("BACKOFF_JITTER") or 0.5),
            healthy_stream_after=float(e.get("HEALTHY_STREAM_AFTER") or 30.0),
            log_level=(e.get("LOG_LEVEL") or "INFO").strip().upper(),
            omemo=_bool(e.get("XMPP_OMEMO")),
            omemo_store=(e.get("XMPP_OMEMO_STORE") or "").strip(),
            omemo_btbv=(e.get("XMPP_OMEMO_BTBV") or "1").strip().lower() in ("1", "true", "yes", "on"),
            component_jid=component_jid,
            component_domain=component_domain,
            component_host=component_host,
            component_port=component_port,
            component_secret=component_secret,
            component_owner=component_owner,
        )
        if not cfg.allowed:
            log.warning(
                "XMPP_ALLOWED is empty: the bridge will DENY EVERY inbound XMPP message. "
                "Set XMPP_ALLOWED to the bare JIDs that may send iMessages.",
                event="allowlist_empty",
            )
        cfg.warn_sanity()
        return cfg

    def warn_sanity(self) -> None:
        if self.mode == "component" and self.muc:
            log.warning(
                "XMPP_MUC is ignored in component mode: group chats are delivered to "
                "COMPONENT_OWNER with the group name in the body",
                event="component_muc_ignored",
                room=self.muc,
            )
        if self.mode == "component" and self.contacts:
            log.info(
                "XMPP_CONTACTS is not used in component mode: every peer has its own JID",
                event="component_contacts_ignored",
            )
        if self.default_to and not self.contacts:
            log.info("XMPP->iMessage will use IMESSAGE_DEFAULT_TO for every allowed sender",
                     event="default_to_only")
        if self.muc and not self.muc_nick:
            raise ConfigError("XMPP_MUC_NICK must not be empty when XMPP_MUC is set")
        if self.omemo and not self.omemo_store:
            raise ConfigError(
                "XMPP_OMEMO_STORE must be set to a writable, persistent file path when "
                "XMPP_OMEMO=1 (the OMEMO identity must survive restarts)"
            )
        if self.omemo and self.muc:
            log.warning(
                "XMPP_OMEMO is on but XMPP_MUC is set: group-chat (MUC) messages are NOT "
                "end-to-end encrypted; OMEMO 2 only covers 1:1 chats.",
                event="omemo_muc_unencrypted",
                room=self.muc,
            )

    def handle_for_jid(self, jid: str) -> Optional[str]:
        """Reverse lookup of XMPP_CONTACTS: a JID -> the iMessage handle it owns."""
        from .models import bare_jid
        target = bare_jid(jid)
        for handle, contact_jid in self.contacts.items():
            if bare_jid(contact_jid) == target:
                return handle
        return None

    def describe(self) -> str:
        """Safe one-line rendering of the effective configuration.

        Never renders the account password or the component shared secret.
        """
        if self.mode == "component":
            return (
                "mode=component"
                + " component_jid=" + self.component_jid
                + " component_domain=" + self.component_domain
                + " component_host=" + self.component_host
                + " component_port=" + str(self.component_port)
                + " owner=" + self.component_owner
                + " targets=" + ",".join(self.targets)
                + " allowed=" + (",".join(sorted(self.allowed)) if self.allowed else "<empty: deny all>")
                + " api_url=" + self.api_url
                + " default_to=" + str(self.default_to)
                + " omemo=off"
            )
        return (
            "mode=client"
            + " jid=" + self.jid
            + " server=" + str(self.server)
            + " port=" + str(self.port)
            + " targets=" + ",".join(self.targets)
            + " muc=" + str(self.muc)
            + " contacts=" + str(len(self.contacts))
            + " allowed=" + (",".join(sorted(self.allowed)) if self.allowed else "<empty: deny all>")
            + " api_url=" + self.api_url
            + " default_to=" + str(self.default_to)
            + " omemo=" + ("on" if self.omemo else "off")
            + (" store=" + self.omemo_store if self.omemo else "")
        )
