# Integration notes (branch integration/0.2)

Four feature sets are merged: FaceTime calls (below), message features (receipts, typing, reactions,
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

Server side (`imessage-server-rustpush`) has no new variables for these features.
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
