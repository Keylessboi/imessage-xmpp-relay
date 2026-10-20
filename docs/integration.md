# Integration notes (branch integration/0.2)

Five feature sets are merged: attachments (XEP-0066), FaceTime calls (below), message features (receipts, typing, reactions,
replies, edits, unsend), group chats as XEP-0045 rooms, and OMEMO in component
mode. Everything is additive; the SSE `message` event and the HTTP API shape are
unchanged. Nothing here has been run against real Apple or a real XMPP server or
client; see the READMEs for the exact untested list.

## Feature environment variables (bridge)

| Variable | Default | Effect |
| --- | --- | --- |
| `XMPP_MODE` | `client` | `component` enables everything below except where noted |
| `RELAY_SEND_READ_RECEIPTS` | on | XEP-0333 `displayed` from the owner becomes an iMessage read receipt |
| `RELAY_SEND_TYPING` | on | XEP-0085 `composing` from the owner becomes an iMessage typing indicator |
| `XMPP_GROUPS_MUC` | `1` | iMessage groups as rooms in the component domain (component mode); `0` = legacy `[group] Name (handle):` lines |
| `XMPP_OMEMO` | off | encrypt 1:1 chat (client and component mode) |
| `XMPP_OMEMO_STORE` | none | identity file; required with `XMPP_OMEMO=1` |
| `XMPP_OMEMO_BTBV` | `1` | auto-trust new owner devices |
| `XMPP_OMEMO_POLICY` | `required` | `required` or `opportunistic` (component mode only) |

Attachments (bridge):

| Variable | Default | Effect |
| --- | --- | --- |
| `PUBLIC_FILE_BASE_URL` | unset | public origin of the server's `/dl` listener; unset = inbound `attachment` events are dropped (text placeholder still goes out) |
| `OUTBOUND_MAX_BYTES` | `104857600` | largest file fetched for an owner-sent OOB URL |
| `OUTBOUND_FETCH_TIMEOUT` | `120` | seconds allowed for that fetch |

Attachments (server, `imessage-server-rustpush`; the whole feature is off without the key file):

| Variable | Default | Effect |
| --- | --- | --- |
| `IMESSAGE_FILE_HMAC_KEY_FILE` | unset | file with the link-signing key (>= 16 bytes, 32+ random recommended); unset = no downloads, no `/dl` listener |
| `IMESSAGE_FILES_LISTEN` | `0.0.0.0:8791` | read-only `/dl/<id>?exp=&sig=` listener (the only thing served there) |
| `IMESSAGE_ATTACH_DIR` | `<data dir>/attachments` | where downloaded files are kept |
| `IMESSAGE_ATTACH_MAX_BYTES` | `104857600` | per-file cap (inbound and `POST /im/attachment`) |
| `IMESSAGE_ATTACH_QUOTA_BYTES` | `2147483648` | total store quota |
| `IMESSAGE_ATTACH_MAX_AGE_SECS` | `604800` | files older than this are removed |

See `docs/k8s-attachments.md` for the cluster wiring.

Server side otherwise has no new variables for the other features.
FaceTime calls have no variables either: they are on whenever `XMPP_MODE=component`
(owner only, 2 requests per minute, nothing to configure).
The base variables (`COMPONENT_*`, `XMPP_*`, `IMESSAGE_*`, `DEDUP_SIZE`,
`LOG_LEVEL`) are unchanged; see the bridge README.

## Interaction policy

* **OMEMO and rooms.** Rooms are plaintext XEP-0045 stanzas. With `XMPP_OMEMO=1`
  the group service is not started (a warning is logged), so group messages use
  the legacy per-peer 1:1 lines, which are encrypted. Fail closed over
  convenience; XEP-0384 does not define OMEMO for rooms here.
* **OMEMO and the new stanzas.** Any stanza with a body is encrypted. Ids-only
  metadata (reply, correction, retraction, chat markers, receipt requests, the
  stanza id) stays on the outer stanza in clear. A XEP-0444 `<reactions/>`
  element and its fallback marker are never sent in clear: only the encrypted
  fallback body (`Loved "..."`) reaches the client, so some clients will not show
  a native reaction under OMEMO. Body-less chat states, receipts and markers go
  unencrypted (they carry no content).
* **Attachments and OMEMO.** Inbound: the download link is the body, so it is
  encrypted like any body; the `jabber:x:oob` element (which would repeat the
  bearer URL) is stripped from the outer stanza under OMEMO, so the file shows
  as an encrypted link, not a native file card. Outbound (owner posts a URL):
  the OOB handler runs after decryption, but the outer `<x xmlns='jabber:x:oob'>`
  is unauthenticated plaintext, so under OMEMO it is honoured only when the same
  URL is also in the decrypted body (the XEP-0066 convention); otherwise it is
  ignored and the message goes as text. A plaintext-allowed opportunistic
  fallback keeps the element. The fetch stays SSRF-guarded and owner-only.
* **Attachments and groups.** With rooms on, a group attachment is a
  `groupchat` message from the sender's occupant (`room/<handle>`) with the link
  as body plus an OOB element (rooms are plaintext). An owner OOB message in a
  joined room is fetched (same guards) and sent to the group with
  `POST /im/attachment?chat_guid=`; in a draft room it is refused. With rooms
  off (or `XMPP_OMEMO=1`) a group attachment goes from the component JID over
  the 1:1 path, as the legacy group text does. Not tested against real Apple.
* **Disco.** The component advertises `jabber:x:oob` together with the Jingle
  features (`calls.DISCO_FEATURES`) and registers it with slixmpp xep_0030.
* **Groups vs reactions/receipts/typing.** Per-peer reactions, receipts and
  typing for group chats remain labelled text notices (`[chat] sender: ...`),
  not room stanzas.
* **Rooms vs XEP-0461 replies.** A room message uses the iMessage guid as its
  stanza id, and the `> quoted` fallback is prepended to the room text, so an
  owner reply to it resolves to the right guid. Inbound group messages are
  indexed for that purpose.
* **FaceTime calls and OMEMO.** An XMPP call the owner starts to a contact (XEP-0353
  `<propose/>`) is declined with a Jingle `<reject/>` (body-less, so it is sent
  unencrypted, like other body-less stanzas) and the bridge asks the server for a
  FaceTime call (`POST /im/facetime-call`, falling back to `/im/facetime-link`). The
  `FaceTime: <url>` message goes through the normal `send_message` path, so it is
  OMEMO-encrypted when `XMPP_OMEMO=1`. The link is posted to the owner only and is
  never sent over iMessage or logged. The typed `facetime` command is matched on the
  plaintext body before OMEMO decryption, so under OMEMO use the call button, not the
  typed command (an encrypted `facetime` message is relayed as ordinary text).
  Untested against real Apple and real XMPP clients; see `bridge/README.md` (Calls)
  and the rustpush server README (FaceTime calls).
