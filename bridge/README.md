# bridge: XMPP <-> iMessage

A thin, testable adapter between the frozen **imessage-server** HTTP API and an
XMPP server you already run (Prosody, ejabberd, ...). It exists so SMS/iMessages
addressed to your number show up in your XMPP client without the OpenBubbles app
running: the separate `imessage-server` process is the actual iMessage client,
this service only moves messages.

```
 iMessage/SMS                bridge (this service)                 XMPP
 ────────────                ────────────────────                 ────
 imessage-server  ──SSE──▶  subscriber ─▶ dedup ─▶ router ──▶ chat to pipe JID
   :8790                      /im/events               ├────▶ per-contact JID
                                                       └────▶ MUC room (group)
 imessage-server  ◀──POST── allowlist ─▶ target resolve ◀── chat/groupchat
     /im/send
```

## Run it

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export XMPP_JID=bridge@example.org XMPP_PASSWORD=... \
       XMPP_TARGET=me@example.org \
       XMPP_ALLOWED=me@example.org \
       IMESSAGE_API_URL=http://imessage-server:8790
python -m relay
```

Component mode (see below) replaces the account with a XEP-0114 component:

```sh
export XMPP_MODE=component \
       COMPONENT_JID=imessage.example.org \
       COMPONENT_SECRET=... \
       COMPONENT_OWNER=me@example.org \
       IMESSAGE_API_URL=http://127.0.0.1:8790
python -m relay
```

Container: `docker build -t openbubbles-bridge bridge/` then run with the same
environment. The image is multi-stage (wheels built with compilers, runtime image
has none) and runs as UID/GID 10001.

## Configuration

Everything comes from environment variables. The only file the bridge reads is
the OMEMO store named by `XMPP_OMEMO_STORE` (and only when `XMPP_OMEMO` is
on); neither the account password nor the component shared secret is ever
logged, and the startup log line renders the effective config through
`Config.describe()`, which omits both.

| Variable | Default | Meaning |
|---|---|---|
| `XMPP_MODE` | `client` | `client` (logged-in bot account) or `component` (XEP-0114 external component) |
| `XMPP_JID` | **required in client mode** | Bot account, e.g. `bridge@example.org` |
| `XMPP_PASSWORD` | **required in client mode** | Password for that account (never logged) |
| `XMPP_SERVER` | JID domain / DNS SRV | Connect override, `host` or `host:port` |
| `XMPP_PORT` | `5222` | Used with `XMPP_SERVER` |
| `COMPONENT_JID` | **required in component mode** | Bare domain the component owns, e.g. `imessage.example.org` |
| `COMPONENT_SECRET` | **required in component mode** | XEP-0114 shared secret (same as the server listener password; never logged) |
| `COMPONENT_OWNER` | first `XMPP_TARGET`/`XMPP_ALLOWED` | The **only** JID allowed to send iMessages |
| `COMPONENT_DOMAIN` | `COMPONENT_JID` | Domain the peer JIDs live in |
| `COMPONENT_HOST` | `127.0.0.1` | Server's component listener |
| `COMPONENT_PORT` | `5347` | Server's component listener port |
| `XMPP_TARGET` | `COMPONENT_OWNER` in component mode | Comma-separated "pipe" JIDs that receive 1:1 chats |
| `XMPP_MUC` | — | Group-chat room (bare JID, e.g. `im@conference.example.org`); client mode only |
| `XMPP_GROUPS_MUC` | `1` | Component mode: host each iMessage group as a XEP-0045 room; `0` keeps the old `[group]` chat lines |
| `XMPP_MUC_NICK` | `imessage` | Nickname the bridge joins the room with |
| `XMPP_CONTACTS` | — | `handle=jid` pairs, comma/semicolon separated |
| `XMPP_ALLOWED` | empty | Bare JIDs allowed to send iMessages (**empty = deny all**; component mode adds `COMPONENT_OWNER`) |
| `XMPP_NO_SENDER_PREFIX` | off | `1` drops the `Name (handle): ` prefix on pipe messages |
| `XMPP_INSECURE_TLS` | off | `1` disables certificate verification (self-signed servers) |
| `XMPP_DIRECT_TLS` | off | `1` uses implicit TLS (xmpps) instead of STARTTLS |
| `XMPP_OMEMO` | off | `1` enables OMEMO 2 (`urn:xmpp:omemo:2`) for 1:1 chats |
| `XMPP_OMEMO_STORE` | **required** with `XMPP_OMEMO` | File holding the OMEMO identity + sessions (mode 0600, never logged) |
| `XMPP_OMEMO_BTBV` | on | `0` disables blind trust: unknown devices are refused instead of trusted |
| `IMESSAGE_API_URL` | `http://imessage-server:8790` | Base URL of the imessage-server |
| `IMESSAGE_DEFAULT_TO` | — | Fallback destination handle for inbound XMPP messages |
| `REQUEST_TIMEOUT` | `15` | Seconds for /health, /im/status, /im/send |
| `DEDUP_SIZE` | `4096` | Entries in the guid LRU |
| `BACKOFF_INITIAL` / `BACKOFF_FACTOR` / `BACKOFF_MAX` / `BACKOFF_JITTER` | `1` / `2` / `60` / `0.5` | SSE reconnect backoff (seconds) |
| `HEALTHY_STREAM_AFTER` | `30` | A stream that lasted this long resets the backoff |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

At least one of `XMPP_TARGET`, `XMPP_MUC`, `XMPP_CONTACTS` (client mode) or
`COMPONENT_OWNER` (component mode) must be set, or the bridge has nowhere to
deliver and exits with a configuration error.

### Component mode (XEP-0114)

`XMPP_MODE=component` turns the bridge into an external component instead of a
logged-in account. The component owns one domain (say
`imessage.example.org`) and gives **every iMessage/SMS peer its own JID**:

```
+15557654321        ->  +15557654321@imessage.example.org
John.Doe@iCloud.com ->  john.doe-40icloud.com@imessage.example.org
+(any odd handle)   ->  ...@imessage.example.org   (lossless -xx escapes)
```

Phone handles keep their `+digits` form as the localpart (legal in a JID);
anything else is sanitised byte by byte: every character outside
`[a-z0-9._]` becomes `-xx` (two lowercase hex digits), so the mapping is
reversible and an outbound message to that JID resolves back to the Apple ID.
A plain word localpart such as `alice@domain` is rejected, not guessed.

**Addressing**

* **iMessage -> XMPP**: each `/im/events` message becomes a `chat` stanza
  **from** the peer JID **to** every `XMPP_TARGET` (default: the owner). The
  body is the message text with no `Name (handle): ` prefix, because the From
  JID identifies the person. A handle that cannot be encoded falls back to the
  component JID and keeps the `Name (handle): ` prefix.
* **XMPP -> iMessage**: a `chat` stanza addressed to `<handle>@COMPONENT_DOMAIN`
  becomes `POST /im/send` with that number/email as `to`. The explicit
  `+1555...: ` body prefix, `XMPP_CONTACTS` and `IMESSAGE_DEFAULT_TO` are not
  consulted in this mode; the recipient JID is the destination.
* **Deny by default**: the transport drops every stanza whose bare `from` is
  not `COMPONENT_OWNER`, and the core allowlist (which component mode seeds
  with the owner) checks it again. A federated server therefore cannot inject
  iMessages even though the component is reachable.
* **Presence**: `presence_subscribe` from the owner is answered with
  `subscribed` followed by `available`, both **from the peer JID**;
  `presence_probe` is answered with `available`. The peer therefore looks like
  an ordinary presence-capable contact in the client.
* **Robustness**: non-`chat`/`normal` types, missing bodies (chat states,
  receipts, carbons), unknown or malformed localparts, and messages from the
  component's own domain are ignored and counted/logged, never fatal. A
  message is never sent to the component itself.
* **No OMEMO** in component mode (`XMPP_OMEMO=1` is rejected): an external
  component has no OMEMO device identity.
* **Group chats** are XEP-0045 rooms hosted by the component itself, see
  [Group chats as rooms](#group-chats-as-rooms-component-mode). With
  `XMPP_GROUPS_MUC=0` they fall back to the old `[group] Name (handle): ` lines
  from the sender's peer JID.

**ejabberd side (what this implementation assumes)**

```yaml
listen:
  -
    port: 5347
    ip: "127.0.0.1"          # plaintext XEP-0114: keep it off the network
    module: ejabberd_service
    access: all
    shaper: fast
    hosts:
      "imessage.example.org":     # == COMPONENT_JID / COMPONENT_DOMAIN
        password: "<COMPONENT_SECRET>"   # same value as the environment
```

* The listener is XEP-0114 (plaintext on 5347) and authenticates only with the
  SHA-1 handshake built from that shared secret, so bind it to loopback (or an
  internal interface) and firewall it. The secret must be the same string on
  both sides; the bridge never logs it.
* `hosts` registers the component domain; the server must route stanzas
  addressed to `<anything>@imessage.example.org` to this component and must
  accept component stanzas whose `from` is `<peer>@imessage.example.org`.
  If `COMPONENT_DOMAIN` differs from `COMPONENT_JID`, list both domains.
* The owner stays on its own federated server. For
  `you@example.org` to reach
  `+15557654321@imessage.example.org`, the **component domain** must be
  reachable over normal s2s (DNS A/SRV for `imessage.example.org` and a
  certificate valid for it on the ejabberd that hosts the component). The
  component listener itself still only needs to be reachable by ejabberd.
* No roster/ACL is needed in ejabberd for the owner's subscription: the
  component answers subscription requests itself.
* The component domain must not be the owner's domain; config refuses a
  `COMPONENT_OWNER` inside `COMPONENT_DOMAIN` (that would loop).

### Message features (component mode only)

Standard XEPs only. Everything below is additive: plain text relaying is
unchanged, and in client mode (no per-peer JIDs, no stanza extras) these events
are ignored and attachments still become readable text.

| iMessage | XMPP | XEP |
|---|---|---|
| inbound message | stanza `id` = iMessage guid, plus `<request/>` and `<markable/>`, `<store/>` hint | 0184, 0333, 0334 |
| delivered / read (for a message the owner sent) | `<received id=/>` / `<displayed id=/>` from the peer JID, only if the owner's stanza asked | 0184, 0333 |
| typing on / off | `<composing/>` / `<active/>` from the peer JID (1:1 only) | 0085 |
| tapback (add/remove) | `<reactions id=>` carrying the peer's full emoji set for that message, with a `Loved "..."` fallback body | 0444, 0428 |
| reply to a known message | `<reply id= to=>` with a `> quoted` fallback range | 0461, 0428 |
| edit | `<replace id=>` correction (part 0) | 0308 |
| unsend | `<retract id=>` with fallback body | 0424, 0428 |
| sticker / attachment | text placeholder `[sticker: name]` / `[attachment: name (mime)]`; bytes are not fetched | - |

| XMPP (owner) | iMessage | XEP |
|---|---|---|
| `<displayed id=>` for a relayed message | `POST /im/read` (once per message) | 0333 |
| `<composing/>` / paused, active, ... (no body) | `POST /im/typing` (debounced) | 0085 |
| `<reactions id=>` | one `POST /im/react` per added/removed emoji; the six tapback emoji map to tapbacks, anything else to an emoji tapback | 0444 |
| `<reply id=>` | `reply_to` = the referenced guid (fallback text stripped) | 0461, 0428 |
| `<replace id=>` on a message the bridge sent | `POST /im/edit` | 0308 |
| `<retract id=>` on a message the bridge sent | `POST /im/unsend` | 0424 |

Config: `RELAY_SEND_READ_RECEIPTS` (default on) and `RELAY_SEND_TYPING`
(default on) switch the two privacy-relevant outbound signals off.

Limits worth knowing: the guid <-> XMPP id map is in memory and bounded, so
after a bridge restart a reaction/reply to an older inbound message still works
(its id is the guid) but an edit/unsend/receipt for an older message the owner
sent does not (the owner is told); group conversations get labelled text
notices instead of per-peer reactions/receipts/typing; an iMessage edit of a
non-first part or an unsend of a non-first part is a text notice; XEP-0424
legacy `urn:xmpp:message-retract:0` and XEP-0461 replies to messages the bridge
never saw are not specially handled. The server publishes a `message_meta`
event (reply target, attachment list) just before the `message` it describes;
the bridge needs a server that does so for reply/placeholder detail and
degrades to plain text without it.

### Group chats as rooms (component mode)

Standard XEPs only: 0045 (MUC), 0030 (disco), 0249 (direct invite), 0059 (RSM),
0203 (delayed delivery). No custom namespace. Code: [relay/muc.py](relay/muc.py).
`XMPP_GROUPS_MUC` (default `1`) turns it off.

**Where the rooms live.** In the component's own domain, not a `muc.` subdomain:
`grp-u<32 hex>@imessage.example.org` (an uppercase-UUID group id, the usual
case) or `grp-x<hex of the id>@...` (any other id). The component *is* the MUC
service, so the ejabberd side needs no `mod_muc`, no second `hosts` entry, no
extra DNS record or certificate, and the localparts can never collide with a
peer JID (`-u`/`-x` is not a hex escape, so `localpart_to_handle` rejects
them). A subdomain would have needed a second domain on the listener; this
repository does not hold the ejabberd config, so it was not assumed.

| iMessage | XMPP |
| --- | --- |
| group message from a member | `groupchat` from `room/<handle>` (the nick is the phone number or Apple ID) |
| a group appears, or messages arrive while the owner is not in the room | XEP-0249 direct invite from the room (re-sent at most every 10 min); messages are kept as room history |
| group renamed | room subject change |
| member added / removed / left | occupant presence (`affiliation=member`, real JID shown) plus a `groupchat` status line from the room |
| group icon changed | status line only (icons are not mirrored: there is no attachment support yet) |
| owner types in the room | `POST /im/send {"chat_guid"}`; the message is reflected to every joined session as MUC requires |
| owner changes the subject, or submits the `muc#roomconfig_roomname` form | `POST /im/group/rename` |
| owner invites a contact (`<invite to='+1555...@domain'/>`) or sets `affiliation=member` | `POST /im/group/members {add}` |
| owner kicks (`role=none`) or sets `affiliation=none` / `outcast` | `POST /im/group/members {remove}` |
| owner **destroys** the room (`muc#owner` `<destroy/>`) | `POST /im/group/leave`: the only way to leave the iMessage group. Just exiting the room leaves it on the XMPP side only |
| `disco#items` on the component | the groups, paged with XEP-0059 (`max` / `after`) |
| joining a room | `<history/>` honoured (`maxstanzas`, `maxchars`, `seconds`, `since`); replayed messages carry XEP-0203 `<delay/>` |

**Creating a group from XMPP.** Join a room name that does not exist, e.g.
`weekend@imessage.example.org`. That makes a *draft* room (status 201, offered in
`disco#items`). Invite at least two contacts (mediated invites to their peer
JIDs). The first message creates the iMessage group (`POST /im/group`: an
iMessage group is created by sending its first message, with the name and member
list attached) and moves you: the draft is destroyed with an alternate venue
(XEP-0045 10.9) and a direct invite to the real `grp-...` room follows. Fewer
than two invited contacts is refused with `not-acceptable`: two members is a
plain 1:1 chat in iMessage.

**Behaviour you should know.** Only `COMPONENT_OWNER` can do anything; every
other JID is dropped. The owner has one nick per room (all of their sessions
share it) and it cannot equal an occupant's handle. Private messages to
occupants are refused. History is in memory (last 100 messages per room) and
starts when the bridge starts; it is not persisted. Group names and member lists
are loaded from `GET /im/groups` on every SSE `ready`.

**Untested.** The tests cover the pure mapping, every handler against a fake
component, and real slixmpp stanza parsing/dispatch (no socket). Not run: a real
ejabberd with this room traffic, any real XMPP client (Conversations, Dino,
Gajim: how they render component-hosted rooms, the direct invite and the
draft-to-group handoff), and every group operation against real Apple (see the
server README).

### OMEMO 2 end-to-end encryption

`XMPP_OMEMO=1` encrypts **1:1** XMPP messages with OMEMO in the
`urn:xmpp:omemo:2` namespace (XEP-0384: X3DH + Double Ratchet). The crypto and
the wire `<encrypted>` element come from `python-omemo` with the
`python-twomemo` backend; the XEP-0420 Stanza Content Encryption envelope and
the PEP device-list/bundle handling live in `relay/omemo2.py`. This is not the
legacy `eu.siacs.conversations.axolotl` (OMEMO 0.3).

* **Fail closed.** If the target publishes no OMEMO 2 devices, if PEP cannot be
  reached, if any recipient device cannot be encrypted for, or if a stanza
  cannot be decrypted, the message is dropped and logged. Plaintext is *never*
  substituted; there is no silent downgrade.
* **Inbound.** While enabled, an unencrypted 1:1 message is refused rather than
  forwarded to iMessage.
* **Group chat is out of scope.** MUC/group messages stay in plaintext (the
  bridge logs a warning when `XMPP_OMEMO` and `XMPP_MUC` are both set).
  `XMPP_OMEMO=1` therefore protects 1:1 chats only.
* **Identity.** The long-term key material is one JSON file at
  `XMPP_OMEMO_STORE`, written atomically with mode 0600. Deleting it mints a
  new device; the file must be on a persistent volume. Its contents are never
  logged.
* **Trust.** Blind Trust Before Verification is on by default, so a newly
  learned device is trusted automatically (like most desktop clients). With
  `XMPP_OMEMO_BTBV=0` an unknown device makes encryption fail instead.

### Routing rules

**iMessage -> XMPP**

| Event | Destination |
|---|---|
| 1:1 from a handle in `XMPP_CONTACTS` | that contact's JID, body unchanged |
| 1:1 from any other handle | every `XMPP_TARGET`, body prefixed `Name (handle): ` |
| group chat, `XMPP_MUC` set | the MUC room as `groupchat`, body prefixed with the sender |
| group chat, no `XMPP_MUC` | every `XMPP_TARGET`, body prefixed `[chat-guid] Name (handle): ` |

**XMPP -> iMessage** (allowlisted senders only) — the destination handle is
resolved in this order:

1. an explicit target at the start of the body: `+15551234567: hello` or
   `+15551234567> hello` (also email-style Apple IDs);
2. the reverse of `XMPP_CONTACTS` for the sender's JID;
3. in a MUC, the occupant nick when it looks like a handle;
4. `IMESSAGE_DEFAULT_TO`.

If none applies, the bridge replies over XMPP with a short hint instead of
guessing. When the reply belongs to a message the bridge previously delivered,
that message's `guid` is sent as `reply_to`.

## Behaviour

* **SSE subscriber** reconnects with exponential backoff plus jitter (capped,
  never a hot loop) on HTTP errors, dropped connections, malformed `data:`
  lines and even handler bugs. `Last-Event-ID` is forwarded when the server
  sends one.
* **Dedup**: a bounded LRU of guids means a replayed SSE event is delivered
  once. It is separately exercised through the real stream in the tests.
* **XMPP reconnects**: slixmpp retries the initial connect itself; on top of
  that the transport watches the `disconnected` event and reconnects with the
  same jittered backoff.
* **Shutdown**: SIGTERM/SIGINT set a stop event; the SSE reader is abandoned
  immediately (it does not wait for the next upstream byte), then the XMPP
  connection and HTTP session are closed.
* **Logging**: one `key=value` line per record (`time=... level=... logger=...
  msg=... event=...`), so it is greppable without a JSON shipper.

## Tests

```sh
cd bridge
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

No real XMPP server, no external network: an injected fake implements the
`XmppTransport` protocol, and a small aiohttp server (`tests/mock_upstream.py`)
implements the frozen upstream contract on a random localhost port. Proven:

* a `message` SSE event becomes the expected XMPP stanza (pipe, per-contact and
  MUC/group routes, sender prefix, unrouted events counted);
* an inbound XMPP message becomes `POST /im/send` with the right `to`/`text`/
  `reply_to`, including the reply-threading and default-target paths;
* a duplicate guid is dropped (unit level and over a live mocked SSE stream);
* HTTP 500s on `/im/events` are retried and the subscriber keeps working;
  malformed JSON and an exception thrown by the event handler do not kill it;
* the allowlist denies unknown senders and an empty allowlist denies everyone;
* a failing `/im/send` produces a notice back to the sender instead of a crash;
* `Config` parsing, the empty-allowlist warning, and that the password never
  appears in `repr()` or the startup config line;
* the service wires everything together and shuts down cleanly on the stop
  event, and SIGTERM is translated into that stop event;
* **Component mode** (tests/test_component.py): handle->JID mapping (phone and
  Apple-ID round trips) and JID validity; `XMPP_MODE=component` parsing, the
  required secret/owner, owner-in-own-domain rejection, OMEMO rejection and
  that the secret never appears in `repr`, `describe()` or logs; the transport
  against a fake slixmpp component (owner accepted, non-owner denied, unknown
  localparts / bodiless / non-chat / malformed stanzas ignored, own-domain
  echo dropped, From on outbound stanzas, presence `subscribed`+`available`,
  probes, connect host/port, rejected-handshake retry); and routing through the
  bridge (peer JID -> owner, Apple-ID reply threading, owner-only core
  allowlist, group fallback, dedup, `/im/send` failure notice, and the
  component transport chosen by `main.make_transport`).
* **OMEMO 2** (tests/test_omemo2.py): the XEP-0420 envelope round-trips and
  rejects mismatched affixes; two real python-omemo sessions exchange messages
  in both directions through an in-memory PEP store; the store file stays mode
  0600 and never contains the plaintext; the identity survives a restart; and
  every failure path (no devices, unreachable PEP, not started, foreign element,
  plaintext on the wire) fails closed instead of sending cleartext.

The suite collects **106 tests**; all pass offline, with no real XMPP server and
no network.

### Live verification (2026-10-19)

Component mode was run end to end against a real **ejabberd 26.9.0**:

* the XEP-0114 handshake was accepted;
* the owner's presence subscription was answered with `subscribed` followed by
  `available`, and a probe was answered;
* an XMPP `chat` message addressed to a peer JID became a **delivered iMessage**
  through the live `imessage-server-rustpush`;
* a message from a JID other than `COMPONENT_OWNER` was **denied**.

See [../crates/imessage-server-rustpush/README.md](../crates/imessage-server-rustpush/README.md)
for the iMessage side of that run.

### Manual smoke run

`smoke_real_transport.py` starts the mock upstream and the **real**
`SlixmppTransport` pointed at a closed port, pushes an event, and asserts the
service neither dies nor hot-loops before a clean shutdown. It needs the test
dependencies and prints its own log:

```sh
timeout 40 .venv/bin/python smoke_real_transport.py   # prints SMOKE_OK
```

## What is NOT proven

* **Message features (receipts, typing, reactions, replies, edits, retractions,
  placeholders) are covered by unit tests with a fake transport and a mock
  upstream only.** No stanza was sent to a real XMPP server or client, and no
  such event was received from real Apple. Which of Conversations/Dino/Gajim/
  Monal render each XEP is unverified, and the Rust side was compile- and
  unit-tested on the NAS but never run against Apple (rustpush send paths for
  read/typing/react/edit/unsend are unexercised).

* **Client mode was not run against a live server.** No stanza was sent from a
  logged-in bot account, so login, STARTTLS, roster, `xep_0045`
  `join_muc_wait` and the reconnect path are still unverified against Prosody or
  ejabberd. Component mode was verified live (above).
* **s2s and a real client UI are still unproven in component mode.** The
  ejabberd proof ran locally; reachability of the component domain from a
  different federated server, and how Conversations/Dino/Gajim render the peer
  JIDs and the owner's subscription, were not exercised.
* `tests/test_service.py` SIGTERM test sends the signal to the test process
  itself; the same handler is installed by `run_service`.
* **Long-lived SSE against the real server is unproven.** The component test used
  the live `imessage-server-rustpush`, but nobody has watched the bridge's SSE
  subscriber through a long idle or reconnect window, or confirmed whether that
  server sends `id:` lines.
* Nothing was run in Docker (no image was built on the development host).
* **OMEMO 2 was never used against a live server or a real client.** The full
  session and SCE envelope are exercised offline between two in-process
  accounts, but relay/omemo2_slixmpp.py (the pubsub device-list/bundle publish
  and fetch) and interop with Conversations/Dino/Gajim are unverified. The SCE
  profile namespace is urn:xmpp:sce:1 per XEP-0420 v0.5.0 / XEP-0384 v0.9.1.
* **Inbound iMessage -> XMPP has never been observed.** No message has arrived at
  the relay's number yet, so the receive side of the bridge is untested against
  real traffic.
* Group-chat delivery uses a single MUC occupant; the sender is carried in the
  message body, not as a separate occupant. The `muc_nick` argument exists for
  API compatibility and is only logged.
