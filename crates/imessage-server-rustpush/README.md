# imessage-server-rustpush

The real Apple IDS backend for `imessage-server`, on top of
`imessage-lab/rustpush`. Same frozen HTTP API; see
[`../imessage-server/README.md`](../imessage-server/README.md) for config and
endpoints.

## Status

**2026-10-19: the number is registered and outbound works; inbound is unproven.**

| Piece | State |
| --- | --- |
| `cargo build` (this crate) | **green**, verified on this machine |
| APS connection + APNs token | **verified live**: Apple returns a real 32-byte token |
| Anisette (X-Apple-I-MD / -MD-M) | **verified live**: 9 headers returned by the configured backend |
| **Phone registration** | **verified live 2026-10-19**: `P:+15551234567` is a registered IDS user, with **no Apple ID login** |
| **Send** | **verified live**: `/im/send` to +15557654321 was delivered (Apple's APS receipt) |
| **FaceTime link** | **verified live**: `POST /im/facetime-link` returns a real join link from the phone-only identity |
| **Group reply** | **implemented, not live-verified**: needs a real inbound group message |
| **Receive / SSE** | **written, not exercised**: no inbound message has arrived at the number |
| SMS 2FA code read off the handset over adb | **verified on-device**: 6/6 unit tests + a live adb read; the working deployment does not log in to an Apple ID |
| Apple ID login | **not used by the deployment**; the two bugs that blocked it are fixed (below) |

The registration survives a restart with the phone unplugged: the backend logs
`reusing saved IDS state (no Apple ID login needed)`, publishes
`handles=["tel:+15551234567"]`, and `/im/status` reports `registered:true,
login_error:null`. The phone is needed again only if Apple invalidates the
registration. The device identity is an iPhone-class `RelayConfig` built from
`REGISTRATION_RELAY_URL` + `REGISTRATION_RELAY_CODE`; see the root
[`README.md`](../../README.md) for the registration path and why a synthetic Mac
profile fails at `id-register` with `6004`.

The first time the relay answers `get-version-info`, the backend writes the
result (the relay's version tuple plus the device UDID) to
`<data_dir>/relay-identity.json`, atomically and with mode 0600. On later boots,
if `REGISTRATION_RELAY_URL`/`CODE` are unset or the relay does not answer, that
file is used instead and the log says `device identity: cached (registration
relay unavailable)`, so the relay and the phone are a one-time dependency. The
file is not a secret credential (it holds no relay code or Apple tokens) but it
lives in the data dir, so keep it with the rest of the state. Deleting it only
means the relay is needed again at the next boot; with neither relay nor cache
the backend falls back to the Mac identity from `hwconfig.plist`.

The sections below are the 2026-10-13/14 evidence trail. Where they say the
Apple ID login is blocked, read that as **history**: `-80045` was a client-info /
OS mismatch and the SMS 2FA submission was a header-set mismatch, both fixed.
The working deployment avoids the Apple ID path entirely.

## History: the `-80045` login failure (2026-10-13; solved)

> **Solved 2026-10-13.** It was a **client-info / OS mismatch**, not a permanent
> block: public anisette-v3 servers build the ADI machine ID with Apple's Android
> library, so the client-info must claim Android. With an Android client-info
> string the SRP init reaches the account check; Mac/Windows/iPhone strings
> returned `-80045`. This section is the evidence trail from when it looked
> Apple-side. It is kept because it documents why anisette was not at fault.

The long-standing failure is:

```
ERROR imessage_server_rustpush::backend: Apple ID / IDS registration did not complete:
  Apple ID login failed: AppleAccount::login: This action cannot be completed at this time. (-80045)
```

The original hypothesis was that the anisette backend was broken (an early probe
of `https://ani.sidestore.io/v3/get_headers` with an **empty body** returned HTTP
502). That is **wrong**, and the evidence is direct:

1. **Anisette works.** `POST /v3/get_headers` with the real body the client sends
   (`{"identifier": <b64 16B>, "adi_pb": <b64>}`) returns HTTP 200 with real
   attestation values. The 502 was an artifact of the empty-body probe; the
   `-45061 not provisioned` reply that a fresh identifier gets is the *normal*
   first response, which the client handles by provisioning (and
   `data/anisette/state.plist` already holds a provisioned `adi_pb`).
   ```
   {"result":"Headers",
    "X-Apple-I-MD":"AAAABQAAABD3iiOGz3zlcBYrh/9WAwuCAAAABA==",
    "X-Apple-I-MD-M":"/QnhSvqpCQvKAAAjJlhFhgS7MOi6TAxeZd8CizaNyxwkwRmvx28gLSw1OBtA2yYMZVHbWzhUh2G0zb7c",
    "X-Apple-I-MD-RINFO":"17106176"}
   ```
2. **The running server agrees.** A live boot logs the provider succeeding and
   then GSA rejecting 234 ms later:
   ```
   INFO  anisette backend: https://ani.sidestore.io (override with ANISETTE_URL / OMNISETTE_URL_V3)
   INFO  anisette headers OK: 9 headers (X-Apple-I-MD 40 chars, X-Apple-I-MD-M 80 chars)
   ERROR Apple ID login failed: AppleAccount::login: This action cannot be completed at this time. (-80045)
   ```
3. **It is not credential-related.** The error is returned by GSA's own plist
   reply at the SRP **init** POST — `check_error` in
   `icloud-auth/src/client.rs` reads `ec=-80045`, `em="This action cannot be
   completed at this time."` — *before* any password proof is sent. A run with a
   made-up Apple ID and a random password hash gets the identical `-80045` at the
   identical point.
4. **It is ecosystem-wide and started ~2026-10-12/13.**
   * `Dadoum/anisette-v3-server#61`: Apple GrandSlam rejecting requests from six
     different public anisette servers; local XADI provisioning still succeeds.
   * `altstoreio/AltStore#1828`: **official AltServer 1.8.1 on macOS 27.0.1, on a
     real Mac with local AOSKit anisette, gets the same `-80045`**, as do
     Sideloadly 0.70.1 and Plumelmpactor — i.e. it is not specific to
     `anisette-v3-server`. Clearing `anisetteID`, another network, other
     accounts and unlocked accounts all made no difference.
   * `xtool-org/xtool#329`: same error for a fake account; early client-info
     changes did not help. The fix that worked was specifically an **Android**
     client-info string, matching the Android-derived anisette machine ID.

**Consequence at the time:** the failure was non-fatal (see degraded mode).
It was later fixed client-side with an Android client-info string, and the login
then advanced past 2FA. `clearadi`
(`third_party/apple-private-apis/clearadi`) remains a **stub** (`todo!()` in
every method) and is not a usable alternative.

## Anisette configuration

`rustpush::default_provider` hardcodes the anisette-v3 URL. This crate therefore
constructs the provider itself in `src/backend.rs` (`make_anisette_provider`)
and takes the base URL from **`ANISETTE_URL`**, falling back to
**`OMNISETTE_URL_V3`** and then `https://ani.sidestore.io`. Trailing slashes are
tolerated. The provider type, the on-disk `<data_dir>/anisette/state.plist` and
the `AppleAccount::login` call are identical to `default_provider`; the crate
adds an `omnisette` path dependency only to name
`AnisetteClient`/`RemoteAnisetteProviderV3`, which rustpush does not re-export.

`login_apple_id` also **pre-flights** the provider and logs the outcome (header
count and the two attestation-value lengths only, never the values), so an
anisette fault is reported as itself instead of hiding inside a generic GSA
error:

```
anisette headers OK: 9 headers (X-Apple-I-MD 40 chars, X-Apple-I-MD-M 80 chars)
# or, pointed at an unreachable backend:
anisette backend failed - the Apple ID login will fail for that reason: Request Error ... Connection refused
```

## What changed (degraded mode)

The Apple ID step and the phone-number step are independent:
`rustpush/src/auth.rs:578 authenticate_phone(number, AuthPhone { push_token, sigs }, os_config)`
posts to Apple's IDS with only the device config and the APNs push token plus
the carrier signature. No GSA account is involved, so a failed or absent Apple
ID login must not stop the server.

`src/backend.rs`:

* connects APS, publishes the APNs token, and installs the backend as
  **running with no `IMClient`** (degraded: `registered:false`, `handles:[]`)
  before the (possibly slow, possibly failing) Apple ID login;
* returns `Ok` from `Backend::start` even when the login fails — only genuinely
  fatal setup errors (bad data dir, unparsable `hwconfig.plist`, no NGM
  identity) still fail;
* serves `GET /apns-token` from the live APS connection whenever the token
  exists, regardless of login;
* lets `POST /im/register-phone` run with **no prior login**. On success the
  `P:<number>` `IDSUser` (with its `com.apple.madrid` registration) is persisted
  into `state.plist.backend_state.users` and its handles appear on
  `GET /im/status`. Failure returns Apple's own error text
  (`400`/`502`), never a generic 500;
* retries the Apple ID login on a **slow bounded backoff** instead of dying at
  boot (`IMESSAGE_LOGIN_RETRY_SECS`, default `1800` = 30 min, doubling to a
  3600 s cap; `0` disables). One attempt at a time, exactly one log line per
  attempt, and the task stops as soon as an `IMClient` exists. Apple's `-80045`
  block is ecosystem-wide and expected to lift on its own, so retrying faster
  only adds load to Apple and noise to the log;
* runs the login inside its own `tokio::spawn`, so even an upstream panic in the
  anisette provider becomes a `JoinError` and cannot abort `start_inner`.

The only HTTP-layer change is in `imessage-server`: a new default-false
`Backend::phone_registration_available()` lets the real backend answer
`/im/register-phone` before `registered` is true. The stub keeps the old
`503 {"error":"not registered"}` behaviour, and every pre-existing response
field and shape is unchanged.

### APS state is persisted on every boot, degraded or not

`APSState` (the APNs keypair + token) used to be written into
`state.plist.backend_state` only by `install_registered` - i.e. only once an
IDS registration succeeded. A degraded boot never gets there, so the state was
never saved, and the next boot's `APSConnectionResource::new(config, None)`
ran `activate()` again and minted a **new** APNs token (`aps.rs` `do_connect`
only skips `activate()` when `state.keypair` is already set). That matters
because a `REG-RESP` signature is bound to the token carried in its
`REG-REQ`: replaying a signature captured on one boot against another boot's
token is what Apple answers with the cert-less
`Plist parsing error: missing field cert` instead of the expected `6004`.

`src/backend.rs` now saves whatever the connection produced as soon as it is
available:

* `persist_aps_state()` runs in `start_inner` immediately after the APS
  connection is up and **before** the Apple ID login is attempted, and again
  from `spawn_apns_token_watch` when the token only appears later (a reconnect);
* the write goes through a new, additive `StateStore::update_backend_state()`
  read-modify-write (`crates/imessage-server/src/state.rs`) that runs the whole
  read-modify-write, including the save, under the state write lock. The push
  state is saved without dropping the IDS users or the NGM identity stored in
  the same blob, and a registration in flight cannot drop the push state either.

Verified live 2026-10-13 with `IMESSAGE_LOGIN_RETRY_SECS=0` in the repo root
against `IMESSAGE_DATA_DIR=./data`: three consecutive boots returned the
**identical** 64-hex token from `GET /apns-token`, and
`state.plist.backend_state.push` held a keypair plus a 32-byte token whose
first eight hex characters matched it. Login still failed non-fatally with
`-80045` (the Apple-side block above), and `/health`, `/im/status`,
`/apns-token` and `/im/register-phone` all answered exactly as before.
`cargo build` + `cargo test` are green in both crates.

### `GET /im/status` login diagnostics

Two **additive** fields tell the owner at a glance whether Apple has unblocked us,
without scraping the log:

```json
{"registered":false,"handles":[],"apple_id":"<the configured account>",
 "login_error":"Apple ID login failed: AppleAccount::login: This action cannot be completed at this time. (-80045)",
 "last_login_attempt":1791891401}
```

* `login_error` is the last Apple ID / IDS failure as a string, or `null` after
  a success or before the first attempt.
* `last_login_attempt` is Unix seconds of the most recent attempt, or `null`.

The backend reports them through a defaulted `Backend::login_status()` on the
existing trait, so the stub (and any other backend) keeps the old behaviour with
`null`/`null`. The fields are set by `set_login_attempt()` in `src/backend.rs`
on every boot/retry attempt and cleared on a full success. Verified live
2026-10-13: `login_error` carried the verbatim `-80045` text and
`last_login_attempt` was the boot-attempt time.

### Historical live run (2026-10-13, before the client-info fix)

```
$ cd /path/to/imessage-xmpp-relay
$ IMESSAGE_DATA_DIR=./data LISTEN=127.0.0.1:8790 IMESSAGE_LOGIN_RETRY_SECS=0 \
    RUST_LOG=info,rustpush::util=warn \
    ./crates/imessage-server-rustpush/target/debug/imessage-server-rustpush
```

Relevant log lines from the most recent verified boot (token redacted):

```
INFO imessage_server: listening on http://127.0.0.1:8791
INFO imessage_server_rustpush::backend: APNs token available (32 bytes)
INFO imessage_server_rustpush::backend: anisette backend: https://ani.sidestore.io (override with ANISETTE_URL / OMNISETTE_URL_V3)
INFO imessage_server_rustpush::backend: anisette headers OK: 9 headers (X-Apple-I-MD 40 chars, X-Apple-I-MD-M 80 chars)
ERROR imessage_server_rustpush::backend: Apple ID / IDS registration did not complete: Apple ID login failed: AppleAccount::login: This action cannot be completed at this time. (-80045)
WARN  imessage_server_rustpush::backend: running DEGRADED (registered=false): ...
WARN  imessage_server_rustpush::backend: Apple ID login retry is disabled (IMESSAGE_LOGIN_RETRY_SECS=0)
```

Endpoint probes against that running process:

```
$ curl -s localhost:8791/health
{"ok":true,"registered":false}
$ curl -s localhost:8790/im/status
{"apple_id":"<the configured account>","handles":[],"registered":false,
 "login_error":"Apple ID login failed: AppleAccount::login: This action cannot be completed at this time. (-80045)",
 "last_login_attempt":1791891401}
$ curl -s localhost:8791/apns-token      # value redacted: 64 hex chars = 32 bytes
{"token":"<64 lowercase hex chars>"}
$ curl -s -X POST localhost:8791/im/send -H 'content-type: application/json' \
    -d '{"to":"+15551234567","text":"hi"}'
{"error":"not registered"}
$ curl -s -X POST localhost:8791/im/register-phone -H 'content-type: application/json' \
    -d '{"number":"+15551234567","signature":"aabbccdd"}'
{"error":"authenticate_phone failed: Plist parsing error: Serde(\"missing field `cert`\")"}   # HTTP 400
```

The `register-phone` call reached Apple **without any login**; the error is
Apple's actual response (it rejected the dummy signature and the cert field is
absent), returned as `400` with Apple's text rather than a generic 500. A real
`REG-RESP` signature is what is still unproven.

## FaceTime calls (`POST /im/facetime-call`, new, UNTESTED against Apple)

`facetime_call.rs` keeps one long-lived `FTClient` (also used by `/im/facetime-link`),
feeds it the APS push stream (`FTClient::handle`, needed for QuickRelay allocations),
sets a local interface, then `create_session(guid, our handle, [contact], video)` and
`get_session_link(guid)`; the returned link is the owner's way into the ringing call.
A let-me-in request (the owner opening the link) is auto-approved for the session
started last, and for nothing else. On any failure (60 s cap) the endpoint falls back
to `get_link_for_usage` and answers `rang:false` with the reason. It compiles (see the
branch report) but has never run: whether Apple accepts `create_session` / an
`Invitation` from the phone-only synthetic identity, and whether the contact's phone
rings, is unknown. The link is never logged.

## FaceTime links and group replies (2026-10-19)

Two additive capabilities, both wired into the same rustpush identity the relay
already holds (a phone-number-only `P:+1...` IDS user, **no Apple ID**).

### `POST /im/facetime-link` - a real `https://facetime.apple.com/join#...` link

```json
$ curl -s -X POST localhost:8790/im/facetime-link -H 'content-type: application/json' -d '{"handle":"+15557654321"}'
{"ok":true,"url":"https://facetime.apple.com/join#v=1&p=<pseudonym>&k=<EC public key, redacted>"}
```

`handle` is context only (a FaceTime link is not tied to one recipient). The link
is minted by `FTClient::get_link_for_usage` (`rustpush/src/facetime.rs:628`),
which provisions an IDS pseudonym (`id-provision-alias`, feature `Gondola`, on
`com.apple.private.alloy.facetime.multi`) for the relay's **own** handle and builds the
join URL from that pseudonym plus a fresh P-256 key
(`FTLink::get_link`, `facetime.rs:101`).

Proof it works with a phone-number-only identity: Apple answered the alias
provision with `pseud:+<pseudonym-redacted>` (status 0) for
`handle tel:+15551234567`, and the URL above came back. There is no Apple ID
anywhere in that path, so the documented "FaceTime needs an Apple ID" worry
does not apply to link creation.

State: `SavedBackend.facetime` in `state.plist.backend_state` (rustpush `FTState`,
serialised), written by the `update_state` callback. One stable usage string
(`relay-link`) means repeat calls return the **same** link instead of minting a
new one; verified by calling it twice and again after a service restart (the
pseudonym re-validated from the persisted key cache), with exactly one link
ever created.

### Group replies: `POST /im/send {"chat_guid": ...}`

`/im/send` gained an optional `chat_guid`; `to` is now optional *only* when
`chat_guid` is present, so a caller that passes only `to` behaves exactly as
before (same validation, same response).

```json
$ curl -s -X POST localhost:8790/im/send -H 'content-type: application/json' \
    -d '{"chat_guid":"iMessage;+;<gid from an inbound event>","text":"hi"}'
{"ok":true,"guid":"..."}
```

Group sends need conversation state, and rustpush has exactly one source for
it: the inbound wire message. `receive_loop` now records, per `chat_guid` (the
wire `gid` that `/im/events` already publishes), the member list and the group
id (`remember_chat` in `src/backend.rs`), persists them in
`state.plist.backend_state.chats`, and a reply replays them as
`ConversationData { participants, sender_guid }` - the same two fields
`prepare_send`/`to_raw` put on the wire (`RawIMessage.p` and `.gid`). Reusing
the recorded `gid` is what makes the reply land in the existing thread rather
than starting a new one; without it the message would arrive as a new group.

Unknown `chat_guid` returns `503` with an explicit message instead of guessing.
**Not yet verified against a live group**: no inbound group message has been
seen by this relay, so there is no real `gid` to reply into. An inbound group
text to the relay's number is the acceptance test.

### Group management: `/im/group*` and the `group` event (2026-10-20)

Additive; the frozen endpoints and the `message` event are untouched. An iMessage
group has no server-side object: it is a member list plus a group id (`gid`, our
`chat_guid`), and every change is just another message carrying that id. So
`src/groups.rs` holds pure state transitions over the saved chat map
(`SavedChat` gained `name` and `group_version`), and `Backend::group_op`
(`src/backend.rs`, section "group chats") sends one message then updates the copy:

| Op | Wire message (rustpush) |
| --- | --- |
| create | the first `Message::Message` (or, with no text, a `RenameMessage`) with a fresh uppercase-UUID `sender_guid`, the member list and `cv_name` |
| rename | `Message::RenameMessage` |
| add / remove members | `Message::ChangeParticipants` with `group_version` + 1 |
| leave | `Message::ChangeParticipants` without our handles (how iMessage leaves) |

Inbound `RenameMessage`, `ChangeParticipants` and `IconChange` update the saved
chat and publish a `group` SSE event. Replies into a group now also carry the
recorded `cv_name`.

**Verified:** the pure functions (`cargo test`: member add/remove/leave rules,
the three-member floor, inbound event derivation). **Not verified:** any of it on
the wire against Apple, from either direction. Whether Apple accepts a
rename-only create, and how other devices render a `group_version` we chose
ourselves, are unknown. Outbound icon change is not implemented (no attachment
support).

## Enabling / building

```sh
cd crates/imessage-server-rustpush
cargo build
```

**This crate is not buildable from a bare clone.** Every dependency on the
rustpush tree is a path dependency into `../../../imessage-lab/rustpush`
(three levels up from this crate, so a sibling of the repository root). Clone
and prepare that checkout first:

```sh
git clone https://github.com/JJTech0130/rustpush ../imessage-lab/rustpush
cd ../imessage-lab/rustpush
git submodule update --init --recursive   # six submodules plus one nested
```

If the rustpush checkout lives somewhere else, edit the four `path = ...`
entries in `Cargo.toml` (the `[patch.crates-io] quinn` entry plus the
`keystore`, `omnisette` and `rustpush` dependencies) to point at it.
The crate is deliberately **not** a workspace member (see the root `Cargo.toml`).

The `[patch.crates-io] quinn` entry in `Cargo.toml` is **required**: rustpush
re-declares the same patch, but a `[patch]` is only honoured from the workspace
root, and rustpush is a path dependency here. Without it `h3-quinn` links
against the crates.io `quinn` while rustpush uses the fork, and
`rustpush/src/ids/link.rs` fails with two incompatible `quinn::Connection`
types. Do not remove it. `build.rs` adds `-latomic` for QEMU/unicorn; keep it.

## First boot flow

1. Read `.env` + the config directory; parse `hwconfig.plist` into `MacOSConfig`
   and `gsa.plist` into an Apple ID + SHA-256 password.
2. Load `state.plist`, connect APS, publish the APNs token, install the backend
   as running (degraded). `/health` and `/apns-token` now answer.
3. With saved IDS users: skip to step 5 — no login, no 2FA. This also applies to
   a `P:<number>` user saved by an earlier `/im/register-phone`: a restart
   rebuilds the `IMClient` from it without any Apple ID.
4. Otherwise log in (`AppleAccount::login`, SRP; may ask for 2FA), then
   `login_apple_delegates` -> `authenticate_apple`. Failure here is non-fatal:
   log the error, stay degraded, retry on the timer.
5. `register(...)` the IDS users, build the `IMClient`, mark the server
   registered (emits `{"type":"ready"}`), start the push loop.
6. `POST /im/register-phone` mints the `P:<number>` user from an SMS
   `REG-RESP` signature: `authenticate_phone(number, AuthPhone { push_token,
   sigs: [sig] }, config)`, then re-`register`s and rebuilds the `IMClient`.

## Two-factor authentication: SMS, not trusted device (history)

The trusted-device notification flow is not the one used here. **The SMS flow is
the one this code implements**, and it is what the upstream state machine picks:

`icloud-auth/src/client.rs` `login_with_anisette` maps
`LoginState::NeedsSMS2FA` / `NeedsDevice2FA` (GSA's `secondaryAuth` /
`trustedDeviceSecondaryAuth`) to
`send_2fa_to_devices()` followed by `send_sms_2fa_to_devices(1)`, which does
`PUT https://gsa.apple.com/auth/verify/phone` with `mode:"sms"` and returns
`NeedsSMS2FAVerification(body)`; that is then answered by `verify_sms_2fa` ->
`POST /auth/verify/phone/securitycode`. `verify_2fa` / `/auth/verify/trusteddevice`
is only reachable from `Needs2FAVerification`, which the login loop never
produces, so the trusted-device prompt is effectively dead code here.

The synchronous code callback needs no human at all: the code is read **off the
phone over adb**. The Apple ID's SIM is in a handset attached to this host
(serial `<adb-serial>`; override with `IMESSAGE_ADB_SERIAL`). `aka-oracle`
(`com.openbubbles.aka`, debuggable) appends every inbound text to its private
`files/sms.txt`:

```
[<iso>] src=SMS_RECEIVED from=<addr> time=<millis> body=<body>
```

which `run-as` reads without root. `src/sms2fa.rs` snapshots that file *before*
the login asks Apple for anything - the login loop requests the SMS **before** it
calls this closure, so a snapshot taken inside the closure would already contain
the new message and look stale - then polls for the first line that was *not in
the snapshot* and carries a standalone 6-digit code. Freshness is a set
difference, not a clock comparison, so an old code from a previous attempt can
never be reused. An Apple-looking body wins over another sender (a Google
verification SMS with six digits also lands in this file). Every `adb` call is
wrapped in `timeout 10`, so an unplugged handset cannot wedge the login thread.

Precedence when the callback fires:

1. `IMESSAGE_2FA_CODE` (operator override);
2. a code an operator left in `<data_dir>/2fa.txt`;
3. the SMS, read over adb. The code is then written back to `<data_dir>/2fa.txt`
   as an artifact and logged as device-read, not typed:
```
INFO imessage_server_rustpush::backend: 2FA code read from the handset over adb - from=<addr> \
     device_time=Some(<ms>) (read off the device, NOT typed); writing <data_dir>/2fa.txt for the record
```
A stale `2fa.txt` left by a previous attempt is deleted when the closure is
built, so a dead code is never sent to Apple.

Verified:

* `cargo test` - 6/6 unit tests (code extraction, freshness by set difference,
  Apple-sender preference, DATA_SMS rejection).
* Live against the real handset:
  `SMS2FA_LIVE=1 cargo test live_reads_a_new_code -- --nocapture` appends a
  synthetic SMS to the app's mirror over adb and proves `wait()` reads the new
  code back; passed in 0.25 s.
* The full exchange against Apple was later exercised: after the `-80045` and
  header-set fixes, Apple answered the code POST with **HTTP 200** and
  `x-apple-ak-auth-type: hsa2`, `x-apple-ak-action: complete`. The working
  deployment no longer uses this path.

Known limitation, upstream and **not patched here**: `send_sms_2fa_to_devices`
used a hardcoded `phone_id = 1` (`icloud-auth/src/client.rs`), so it relied on
the account's first trusted phone being id 1. The fork's `icloud-auth` now reads
`IMESSAGE_2FA_PHONE_ID` (default 1); this account needed id 2. That override
lives outside this crate (`imessage-lab`), not in the server code.

## Upstream bug: omnisette panics on anisette errors (patched in the fork)

`third_party/apple-private-apis/omnisette/src/remote_anisette_v3.rs` used to do:

```rust
if matches!(err, AnisetteError::AnisetteNotProvisioned) {
    state.adi_pb = None;
    client.provision(state).await?;
    plist::to_file_xml(config_path, state)?;
    client.get_headers(&state).await?
} else { panic!() }
```

Any anisette error other than `AnisetteNotProvisioned` panicked inside
`AnisetteProvider::get_anisette_headers` instead of returning the error, taking
down whatever task called it. The fork's working tree now returns the error and
logs it:

```rust
} else {
    log::error!("omnisette: get_headers failed: {err}");
    return Err(err);
}
```

(`matches!(err, ...)` also became `matches!(&err, ...)` so `err` is still owned
for the `return`.) The fork also honours `OMNISETTE_URL_V3` in
`default_provider` via a new `anisette_url_v3()`, to match the crate-side
override. Both hunks were present in the working tree before this crate's build and are
**not** committed. A `git submodule update --force --checkout` on
`imessage-lab/rustpush` (or any other re-checkout / re-init) will **silently
discard them**, and the next anisette error will again `panic!()` the login
task instead of returning `Err`. `scripts/sync-vendor.sh` only refreshes
`vendor/open-absinthe`, so it does not touch omnisette — but re-verify after
*any* submodule operation:

```sh
grep -n 'return Err(err)' imessage-lab/rustpush/third_party/apple-private-apis/omnisette/src/remote_anisette_v3.rs
grep -n 'anisette_url_v3' imessage-lab/rustpush/third_party/apple-private-apis/omnisette/src/lib.rs
```
Verified: with `ANISETTE_URL=http://127.0.0.1:9` the login now logs
`Failed getting anisette data ... Connection refused` and stays degraded instead
## Known unknowns

**Current (2026-10-19):**

* **Inbound has never been observed.** No message has arrived at +15551234567,
  so `receive_loop`, `/im/events` and the bridge's XMPP delivery are unproven
  against real traffic.
* **Group reply is unproven live.** No inbound group message has produced a real
  `gid` to reply into.
* **The Apple ID login is not used.** `-80045` (client-info mismatch) and the
  SMS 2FA header set are fixed, but the last attempt still hit
  `ICLOUD_UNSUPPORTED_DEVICE` at iCloud setup with the synthetic hardware
  identity. The phone-number path avoids the Apple ID entirely.
* **Registration is one-time and stateful.** Deleting `data/state.plist` (or
  losing the registration-relay secret) means registering the number again with
  the phone.
* An account whose trusted phone is not id 1 may not receive the SMS; the fork
  now takes `IMESSAGE_2FA_PHONE_ID`, but that code is outside this crate.
* The `keys_updated` callback is synchronous, so it writes the state file with
  blocking IO (small plist; deliberate).
* No attachment/MMS support and no SIM/entitlement (`authenticate_smsless`)
  path. `authenticate_smsless` exists upstream and is the natural next route if
  the carrier signature route is unavailable.
* To hold a *different* number, that number's SIM must answer its own
  `REG-REQ`; the relay cannot port one.

**Historical:** the old captured `REG-RESP` signature
(`s=<redacted>`) is stale — it predates APS-state persistence and the current
APNs token. The 2026-10-19 registration used a fresh signature captured against
the persisted token. Minting one means exactly one `REG-REQ` to the `22223333`
shortcode; do not spam it.
